"""Subprocess-laag: de enige plek waar externe programma's gestart worden.

Regels (hard):
- geen shell: nooit `shell=True`, altijd een argv-lijst;
- vaste allowlist van binaries (`ALLOWED_BINARIES`); alles anders -> CommandFailed;
- elk argument is een str zonder NUL-byte; de aanroepers bouwen argumenten uitsluitend uit
  gevalideerde waarden (MAC via `normalize_mac`, interfacenaam via `validate_iface`, getallen
  via int()).

De tools hebben dus geen generieke 'run command'; alleen de device-klassen (wifi/system)
roepen deze runner aan met vaste argv-sjablonen.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from pitoolbox.core.errors import CommandFailed, InvalidState

log = logging.getLogger(__name__)

ALLOWED_BINARIES: frozenset[str] = frozenset({"iw", "ip", "airodump-ng", "vcgencmd"})

# Linux IFNAMSIZ = 16 (incl. NUL): maximaal 15 tekens, geen '/' of ':' of spaties.
# Let op: altijd fullmatch gebruiken; '$' matcht ook vóór een afsluitende newline.
_IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,14}$")


def validate_iface(name: str) -> str:
    """Geeft de interfacenaam terug of gooit InvalidState (voorkomt rare argv-waarden)."""
    if not isinstance(name, str) or not _IFACE_RE.fullmatch(name):
        raise InvalidState(f"ongeldige interfacenaam: {name!r}")
    return name


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class ProcHandle(Protocol):
    """Handvat naar een draaiend achtergrondproces (bijv. airodump-ng)."""

    @property
    def pid(self) -> int: ...

    @property
    def stderr_path(self) -> Path | None: ...

    def poll(self) -> int | None: ...

    def interrupt(self) -> None:
        """Netjes stoppen (SIGINT op POSIX) zodat airodump-ng zijn bestanden afsluit."""

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int:
        """Wacht op het einde; gooit TimeoutError als het proces blijft draaien."""


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], timeout: float) -> RunResult: ...

    def spawn(
        self, argv: Sequence[str], cwd: Path | None = None, stderr_name: str | None = None
    ) -> ProcHandle: ...


def stop_process(handle: ProcHandle, grace_s: float = 5.0) -> int | None:
    """SIGINT, wacht `grace_s`, daarna kill. Geeft de returncode (of None als onbekend)."""
    try:
        handle.interrupt()
    except OSError:
        log.warning("interrupt faalde voor pid %s", handle.pid)
    try:
        return handle.wait(grace_s)
    except TimeoutError:
        log.warning("pid %s reageert niet op interrupt, kill", handle.pid)
    try:
        handle.kill()
    except OSError:
        log.warning("kill faalde voor pid %s", handle.pid)
    try:
        return handle.wait(grace_s)
    except TimeoutError:
        return None


class _PopenHandle:
    def __init__(
        self, proc: subprocess.Popen[bytes], stderr_file: IO[bytes] | None, stderr_path: Path | None
    ) -> None:
        self._proc = proc
        self._stderr_file = stderr_file
        self._stderr_path = stderr_path

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def stderr_path(self) -> Path | None:
        return self._stderr_path

    def _close(self) -> None:
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            finally:
                self._stderr_file = None

    def poll(self) -> int | None:
        rc = self._proc.poll()
        if rc is not None:
            self._close()
        return rc

    def interrupt(self) -> None:
        if self._proc.poll() is not None:
            return
        if sys.platform == "win32":
            # Windows kent geen SIGINT naar een kindproces; alleen voor dev, niet het doelplatform.
            self._proc.terminate()
        else:
            self._proc.send_signal(signal.SIGINT)

    def terminate(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()

    def kill(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()

    def wait(self, timeout: float | None = None) -> int:
        try:
            rc = self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"pid {self._proc.pid} draait nog") from None
        self._close()
        return rc


class SubprocessRunner:
    """Echte uitvoering. Alleen binaries uit ALLOWED_BINARIES, gevonden via shutil.which."""

    def __init__(self, which: Callable[[str], str | None] = shutil.which) -> None:
        self._which = which

    def _resolve(self, argv: Sequence[str]) -> list[str]:
        if not argv:
            raise CommandFailed("lege argv")
        for a in argv:
            if not isinstance(a, str) or "\x00" in a:
                raise CommandFailed("ongeldig argument in argv")
        name = argv[0]
        # Exacte naam, geen pad: '/bin/sh' of '../iw' wordt dus geweigerd.
        if name not in ALLOWED_BINARIES:
            raise CommandFailed(f"binary niet toegestaan: {name!r}")
        path = self._which(name)
        if path is None:
            raise CommandFailed(f"binary {name!r} niet gevonden in PATH")
        return [path, *argv[1:]]

    @staticmethod
    def _env() -> dict[str, str]:
        # Vaste locale zodat de uitvoerformaten (iw/ip/vcgencmd) niet vertaald worden.
        return {**os.environ, "LC_ALL": "C"}

    def run(self, argv: Sequence[str], timeout: float) -> RunResult:
        resolved = self._resolve(argv)
        try:
            cp = subprocess.run(  # noqa: S603 - argv-lijst, allowlist, geen shell
                resolved,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                shell=False,
                check=False,
                env=self._env(),
            )
        except subprocess.TimeoutExpired:
            raise CommandFailed(f"{argv[0]} duurde langer dan {timeout:g}s") from None
        except OSError as exc:
            raise CommandFailed(f"{argv[0]} kon niet starten: {exc}") from exc
        return RunResult(cp.returncode, cp.stdout, cp.stderr)

    def spawn(
        self, argv: Sequence[str], cwd: Path | None = None, stderr_name: str | None = None
    ) -> ProcHandle:
        """Start een achtergrondproces.

        airodump-ng gebruikt curses: stdin en stdout gaan naar DEVNULL. stderr gaat naar een
        bestand in `cwd` (zodat een foutmelding achteraf te lezen is); zonder cwd naar DEVNULL.
        """
        resolved = self._resolve(argv)
        stderr_file: IO[bytes] | None = None
        stderr_path: Path | None = None
        if cwd is not None:
            stderr_path = cwd / (stderr_name or f"{argv[0]}.stderr.log")
            stderr_file = stderr_path.open("wb")
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv-lijst, allowlist, geen shell
                resolved,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file if stderr_file is not None else subprocess.DEVNULL,
                cwd=cwd,
                shell=False,
                env=self._env(),
            )
        except OSError as exc:
            if stderr_file is not None:
                stderr_file.close()
            raise CommandFailed(f"{argv[0]} kon niet starten: {exc}") from exc
        return _PopenHandle(proc, stderr_file, stderr_path)
