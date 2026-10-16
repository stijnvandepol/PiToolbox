"""CaptureManager: argv, sessiemappen, limieten, auto-stop (met nep-timer) en stop-escalatie."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from wifi_fakes import FakeRunner, FakeTimer

from pitoolbox.config import Settings
from pitoolbox.core.devices.wifi import CaptureManager
from pitoolbox.core.errors import InvalidState

BSSID = "AA:BB:CC:DD:EE:01"
OTHER = "AA:BB:CC:DD:EE:02"


class Env:
    def __init__(self, tmp_path: Path, mode: str = "monitor") -> None:
        self.runner = FakeRunner(mode)
        self.timers: list[FakeTimer] = []
        self.t = 100.0
        self.captures_dir = tmp_path / "captures"

        def factory(interval: float, fn: Any) -> FakeTimer:
            timer = FakeTimer(interval, fn)
            self.timers.append(timer)
            return timer

        self.mgr = CaptureManager(
            "wlan1",
            "wlan0",
            self.captures_dir,
            self.runner,
            timer_factory=factory,
            clock=lambda: self.t,
            now=lambda: datetime(2025, 1, 10, 12, 0, 0, tzinfo=UTC),
        )

    def touch_capture_files(self, cid: str) -> None:
        d = next(self.captures_dir.iterdir())
        (d / f"cap-{cid}-01.cap").write_bytes(b"x" * 123)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_from_settings(tmp_path: Path) -> None:
    s = Settings(captures_dir=tmp_path / "c")
    assert isinstance(CaptureManager.from_settings(s, FakeRunner()), CaptureManager)


def test_start_argv_and_result(env: Env) -> None:
    res = env.mgr.start(BSSID, 6, 300)
    argv = env.runner.spawned[0].argv
    prefix = argv[argv.index("--write") + 1]
    assert argv == [
        "airodump-ng",
        "--bssid",
        BSSID,
        "--channel",
        "6",
        "--output-format",
        "pcap",
        "--write",
        prefix,
        "wlan1",
    ]
    assert re.fullmatch(r"[0-9a-f]{6}", res["capture_id"])
    assert Path(prefix).name == f"cap-{res['capture_id']}"
    assert res["files_prefix"] == prefix
    assert res["pid"] == env.runner.spawned[0].pid
    assert res["started"].startswith("2025-01-10T12:00:00")
    assert res["stops_at"].startswith("2025-01-10T12:05:00")
    assert Path(res["dir"]).is_dir()
    assert env.runner.spawn_kwargs[0]["cwd"] == Path(res["dir"])
    assert env.timers[0].interval == 300
    assert env.timers[0].started


def test_default_session_dir_is_lazy_and_shared(env: Env) -> None:
    assert not env.captures_dir.exists()
    a = env.mgr.start(BSSID, 6, 60)
    env.mgr.stop(a["capture_id"])
    b = env.mgr.start(BSSID, 6, 60)
    assert a["dir"] == b["dir"]  # één sessiemap per proces
    assert re.fullmatch(r"20250110-120000-[0-9a-f]{6}", Path(a["dir"]).name)


def test_named_session(env: Env) -> None:
    res = env.mgr.start(BSSID, 6, 60, "pentest-lab_1")
    assert Path(res["dir"]) == env.captures_dir / "pentest-lab_1"


@pytest.mark.parametrize("slug", ["../x", "A", "-a", "a b", "x" * 33, "a/b", "", "ok\n"])
def test_bad_session_names_refused(env: Env, slug: str) -> None:
    with pytest.raises(InvalidState):
        env.mgr.start(BSSID, 6, 60, slug)
    assert env.runner.spawned == []


def test_requires_monitor_mode(tmp_path: Path) -> None:
    e = Env(tmp_path, mode="managed")
    with pytest.raises(InvalidState, match="monitor"):
        e.mgr.start(BSSID, 6, 60)
    assert e.runner.spawned == []


def test_refuses_uplink_and_bad_args(tmp_path: Path) -> None:
    runner = FakeRunner("monitor")
    mgr = CaptureManager("wlan0", "wlan0", tmp_path, runner)
    with pytest.raises(InvalidState, match="uplink"):
        mgr.start(BSSID, 6, 60)
    ok = CaptureManager("wlan1", "wlan0", tmp_path, runner)
    for kwargs in [
        {"bssid": "nonsense", "channel": 6, "duration_s": 60},
        {"bssid": BSSID, "channel": 0, "duration_s": 60},
        {"bssid": BSSID, "channel": 6, "duration_s": 5},
        {"bssid": BSSID, "channel": 6, "duration_s": 99999},
    ]:
        with pytest.raises(InvalidState):
            ok.start(**kwargs)
    assert runner.spawned == []


def test_max_two_concurrent_and_same_channel(env: Env) -> None:
    env.mgr.start(BSSID, 6, 60)
    with pytest.raises(InvalidState, match="kanaal"):
        env.mgr.start(OTHER, 11, 60)
    with pytest.raises(InvalidState, match="al een capture"):
        env.mgr.start(BSSID, 6, 60)
    env.mgr.start(OTHER, 6, 60)
    with pytest.raises(InvalidState, match="maximaal 2"):
        env.mgr.start("AA:BB:CC:DD:EE:03", 6, 60)
    assert len(env.mgr.list_active()) == 2


def test_stop_returns_files_and_sends_sigint(env: Env) -> None:
    res = env.mgr.start(BSSID, 6, 60)
    cid = res["capture_id"]
    env.touch_capture_files(cid)
    env.t = 130.0
    out = env.mgr.stop(cid)
    handle = env.runner.spawned[0]
    assert handle.events[0] == "interrupt"
    assert handle.events[1] == "wait(5.0)"
    assert "kill" not in handle.events
    assert out["files"] == [{"name": f"cap-{cid}-01.cap", "size": 123}]
    assert out["duration_s"] == 30.0
    assert out["returncode"] == 0
    assert env.timers[0].cancelled
    assert env.mgr.list_active() == []


def test_stop_kills_after_5s(env: Env) -> None:
    res = env.mgr.start(BSSID, 6, 60)
    handle = env.runner.spawned[0]
    handle.ignore_interrupt = True
    out = env.mgr.stop(res["capture_id"])
    assert handle.events == ["interrupt", "wait(5.0)", "kill", "wait(5.0)"]
    assert out["returncode"] == -9


def test_stop_unknown_and_repeat(env: Env) -> None:
    with pytest.raises(InvalidState):
        env.mgr.stop("abcdef")
    cid = env.mgr.start(BSSID, 6, 60)["capture_id"]
    env.mgr.stop(cid)
    again = env.mgr.stop(cid)  # tweede stop (bijv. na auto-stop) geeft het eerdere resultaat
    assert again["already_stopped"] is True


def test_auto_stop_timer(env: Env) -> None:
    cid = env.mgr.start(BSSID, 6, 90)["capture_id"]
    env.timers[0].fire()  # timer verloopt: geen echte sleep
    assert env.runner.spawned[0].events[0] == "interrupt"
    assert env.mgr.list_active() == []
    assert env.mgr.stop(cid)["already_stopped"] is True


def test_dead_process_is_reaped(env: Env) -> None:
    env.mgr.start(BSSID, 6, 60)
    env.runner.spawned[0].returncode = 2  # airodump crashte
    assert env.mgr.list_active() == []
    env.mgr.start(BSSID, 6, 60)  # slot is weer vrij


def test_stop_all_continues_after_failure(env: Env) -> None:
    a = env.mgr.start(BSSID, 6, 60)["capture_id"]
    b = env.mgr.start(OTHER, 6, 60)["capture_id"]
    real_stop = env.mgr.stop

    def flaky(cid: str) -> dict[str, Any]:
        if cid == a:
            raise RuntimeError("boem")
        return real_stop(cid)

    env.mgr.stop = flaky  # type: ignore[method-assign]
    results = env.mgr.stop_all()
    assert {r["capture_id"] for r in results} == {a, b}
    assert "error" in next(r for r in results if r["capture_id"] == a)
    assert "files" in next(r for r in results if r["capture_id"] == b)


def test_list_active_shape(env: Env) -> None:
    res = env.mgr.start(BSSID, 6, 60)
    env.t = 110.0
    (item,) = env.mgr.list_active()
    assert item["capture_id"] == res["capture_id"]
    assert item["bssid"] == BSSID
    assert item["channel"] == 6
    assert item["elapsed_s"] == 10.0
