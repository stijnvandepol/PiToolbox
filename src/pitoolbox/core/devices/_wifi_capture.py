"""Pcap-captures met airodump-ng op de Alfa (zie `wifi` voor de hardware en de guards)."""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pitoolbox.core.devices._wifi_iface import SYSFS_NET, read_mode, require_driver
from pitoolbox.core.errors import InvalidState
from pitoolbox.core.proc import (
    CommandRunner,
    ProcHandle,
    stop_process,
    validate_iface,
)
from pitoolbox.core.scope import normalize_mac

if TYPE_CHECKING:
    from pitoolbox.config import Settings

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_CONCURRENT_CAPTURES = 2


class _TimerLike(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[[], None]], _TimerLike]


def _default_timer_factory(interval: float, fn: Callable[[], None]) -> _TimerLike:
    t = threading.Timer(interval, fn)
    t.daemon = True
    return t


@dataclass(frozen=True)
class _CaptureRequest:
    """Gevalideerde invoer van `CaptureManager.start`."""

    bssid: str
    channel: int
    duration_s: int


def _validate_request(bssid: str, channel: int, duration_s: int) -> _CaptureRequest:
    try:
        mac = normalize_mac(bssid)
    except ValueError as exc:
        raise InvalidState(str(exc)) from exc
    if isinstance(channel, bool) or not 1 <= int(channel) <= 196:
        raise InvalidState(f"ongeldig kanaal {channel!r}")
    if not 10 <= int(duration_s) <= 3600:
        raise InvalidState("duur moet tussen 10 en 3600 seconden liggen")
    return _CaptureRequest(mac, int(channel), int(duration_s))


@dataclass
class _Capture:
    capture_id: str
    bssid: str
    channel: int
    directory: Path
    prefix: Path
    handle: ProcHandle
    timer: _TimerLike
    started: datetime
    stops_at: datetime
    started_mono: float

    def info(self, now_mono: float) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "pid": self.handle.pid,
            "bssid": self.bssid,
            "channel": self.channel,
            "dir": str(self.directory),
            "files_prefix": str(self.prefix),
            "started": self.started.isoformat(),
            "stops_at": self.stops_at.isoformat(),
            "elapsed_s": round(now_mono - self.started_mono, 1),
        }


class CaptureManager:
    """Beheert airodump-ng pcap-captures (max. 2 tegelijk) in captures_dir/<sessie>/."""

    def __init__(
        self,
        iface: str,
        uplink_iface: str,
        captures_dir: Path,
        runner: CommandRunner,
        *,
        timer_factory: TimerFactory = _default_timer_factory,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        expected_driver: str | None = None,
        sysfs_net: Path = SYSFS_NET,
    ) -> None:
        self._iface = iface
        self._uplink = uplink_iface
        self._expected_driver = expected_driver
        self._sysfs_net = sysfs_net
        self._captures_dir = captures_dir
        self._runner = runner
        self._timer_factory = timer_factory
        self._clock = clock
        self._now = now
        self._lock = threading.RLock()
        self._active: dict[str, _Capture] = {}
        self._finished: dict[str, dict[str, Any]] = {}
        self._default_dir: Path | None = None

    @classmethod
    def from_settings(cls, settings: Settings, runner: CommandRunner) -> CaptureManager:
        return cls(
            settings.alfa_iface,
            settings.uplink_iface,
            settings.captures_dir,
            runner,
            expected_driver=settings.alfa_driver or None,
        )

    # -- sessiemap ---------------------------------------------------------------------

    def _session_dir(self, session: str | None) -> Path:
        if session is not None:
            if not _SLUG_RE.fullmatch(session):
                raise InvalidState(f"ongeldige sessienaam {session!r} (a-z, 0-9, _ en -; max 32)")
            d = self._captures_dir / session
        else:
            with self._lock:
                if self._default_dir is None:
                    stamp = self._now().strftime("%Y%m%d-%H%M%S")
                    self._default_dir = self._captures_dir / f"{stamp}-{secrets.token_hex(3)}"
                d = self._default_dir
        # 0700: captures kunnen gevoelige data bevatten.
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    # -- start/stop --------------------------------------------------------------------

    def start(
        self, bssid: str, channel: int, duration_s: int, session: str | None = None
    ) -> dict[str, Any]:
        request = _validate_request(bssid, channel, duration_s)
        iface = self._monitor_iface()
        with self._lock:
            self._reap()
            self._check_room(request)
            directory = self._session_dir(session)
            cap = self._spawn(self._new_capture_id(), request, directory, iface)
            self._active[cap.capture_id] = cap
            cap.timer.start()
            info = cap.info(self._clock())
        return {
            "capture_id": cap.capture_id,
            "pid": cap.handle.pid,
            "dir": str(cap.directory),
            "files_prefix": str(cap.prefix),
            "started": cap.started.isoformat(),
            "stops_at": cap.stops_at.isoformat(),
            "bssid": cap.bssid,
            "channel": cap.channel,
            "elapsed_s": info["elapsed_s"],
        }

    def _monitor_iface(self) -> str:
        """Uplink- en driverguard; daarna moet de Alfa in monitor mode staan."""
        if self._iface == self._uplink:
            raise InvalidState("alfa_iface is gelijk aan uplink_iface; weiger te capturen")
        iface = validate_iface(self._iface)
        require_driver(iface, self._expected_driver, self._sysfs_net)
        mode = read_mode(self._runner, iface)
        if mode != "monitor":
            raise InvalidState(
                f"capture vereist monitor mode, {iface} staat op {mode!r}; "
                "gebruik eerst alfa_set_mode(monitor)"
            )
        return iface

    def _check_room(self, request: _CaptureRequest) -> None:
        """Weigert een capture die niet naast de lopende kan; aanroepen onder `_lock`."""
        if len(self._active) >= MAX_CONCURRENT_CAPTURES:
            raise InvalidState(f"maximaal {MAX_CONCURRENT_CAPTURES} gelijktijdige captures")
        for cap in self._active.values():
            if cap.bssid == request.bssid:
                raise InvalidState(
                    f"er loopt al een capture voor {request.bssid} ({cap.capture_id})"
                )
            if cap.channel != request.channel:
                # Eén adapter kan maar op één kanaal tegelijk luisteren.
                raise InvalidState(
                    f"lopende capture {cap.capture_id} staat op kanaal {cap.channel}; "
                    "één adapter kan niet op twee kanalen tegelijk"
                )

    def _new_capture_id(self) -> str:
        cid = secrets.token_hex(3)
        while cid in self._active or cid in self._finished:
            cid = secrets.token_hex(3)
        return cid

    def _spawn(self, cid: str, request: _CaptureRequest, directory: Path, iface: str) -> _Capture:
        prefix = directory / f"cap-{cid}"
        # airodump-ng: --bssid filtert op AP, --channel zet vaste kanaal (geen hoppen),
        # --output-format pcap schrijft <prefix>-01.cap.
        # https://www.aircrack-ng.org/doku.php?id=airodump-ng
        argv = [
            "airodump-ng",
            "--bssid",
            request.bssid,
            "--channel",
            str(request.channel),
            "--output-format",
            "pcap",
            "--write",
            str(prefix),
            iface,
        ]
        handle = self._runner.spawn(argv, directory, f"cap-{cid}.stderr.log")
        started = self._now()
        stops_at = datetime.fromtimestamp(started.timestamp() + request.duration_s, UTC)
        timer = self._timer_factory(float(request.duration_s), lambda: self._auto_stop(cid))
        return _Capture(
            cid,
            request.bssid,
            request.channel,
            directory,
            prefix,
            handle,
            timer,
            started,
            stops_at,
            self._clock(),
        )

    def _auto_stop(self, capture_id: str) -> None:
        try:
            self.stop(capture_id)
        except InvalidState:
            pass  # al handmatig gestopt
        except Exception:
            log.exception("auto-stop van capture %s faalde", capture_id)

    def stop(self, capture_id: str) -> dict[str, Any]:
        with self._lock:
            cap = self._active.pop(capture_id, None)
            if cap is None:
                prior = self._finished.get(capture_id)
                if prior is not None:
                    return {**prior, "already_stopped": True}
                raise InvalidState(f"onbekende capture_id {capture_id!r}")
        cap.timer.cancel()
        rc = stop_process(cap.handle)  # SIGINT, 5 s wachten, dan kill
        return self._finalize(cap, rc)

    def _finalize(self, cap: _Capture, returncode: int | None) -> dict[str, Any]:
        files = [
            {"name": p.name, "size": p.stat().st_size}
            for p in sorted(cap.directory.glob(f"cap-{cap.capture_id}*"))
            if p.is_file() and not p.name.endswith(".stderr.log")
        ]
        result: dict[str, Any] = {
            "capture_id": cap.capture_id,
            "bssid": cap.bssid,
            "dir": str(cap.directory),
            "files": files,
            "duration_s": round(self._clock() - cap.started_mono, 1),
            "returncode": returncode,
        }
        with self._lock:
            self._finished[cap.capture_id] = result
            while len(self._finished) > 20:
                self._finished.pop(next(iter(self._finished)))
        return result

    def _reap(self) -> None:
        """Ruim captures op waarvan het proces zelf gestopt is (bijv. airodump crashte)."""
        with self._lock:
            for cid in [c for c, cap in self._active.items() if cap.handle.poll() is not None]:
                cap = self._active.pop(cid)
                cap.timer.cancel()
                rc = cap.handle.poll()
                res = self._finalize(cap, rc)
                res["ended"] = "process_exited"
                log.warning("capture %s stopte zelf (rc=%s)", cid, rc)

    def stop_all(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._active)
        results: list[dict[str, Any]] = []
        for cid in ids:
            try:
                results.append(self.stop(cid))
            except Exception as exc:
                log.exception("stop_all: capture %s faalde", cid)
                results.append({"capture_id": cid, "error": f"{type(exc).__name__}: {exc}"})
        return results

    def list_active(self) -> list[dict[str, Any]]:
        with self._lock:
            self._reap()
            now = self._clock()
            return [cap.info(now) for cap in self._active.values()]
