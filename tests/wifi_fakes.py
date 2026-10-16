"""Hulpmiddelen voor de wifi/system-tests: FakeRunner, FakeHandle, FakeTimer en fixtures."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pitoolbox.core.proc import RunResult

FIXTURES = Path(__file__).parent / "fixtures" / "wifi"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHandle:
    _next_pid = 4000

    def __init__(self, argv: list[str], cwd: Path | None) -> None:
        FakeHandle._next_pid += 1
        self._pid = FakeHandle._next_pid
        self.argv = argv
        self.cwd = cwd
        self.returncode: int | None = None
        self.events: list[str] = []
        # True = reageert niet op interrupt (wait geeft TimeoutError tot kill)
        self.ignore_interrupt = False
        self.exit_code_on_interrupt = 0
        self._stderr_path: Path | None = None

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def stderr_path(self) -> Path | None:
        return self._stderr_path

    def poll(self) -> int | None:
        return self.returncode

    def interrupt(self) -> None:
        self.events.append("interrupt")
        if not self.ignore_interrupt and self.returncode is None:
            self.returncode = self.exit_code_on_interrupt

    def terminate(self) -> None:
        self.events.append("terminate")
        self.returncode = -15

    def kill(self) -> None:
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.events.append(f"wait({timeout})")
        if self.returncode is None:
            raise TimeoutError("nog bezig")
        return self.returncode


class FakeTimer:
    def __init__(self, interval: float, fn: Callable[[], None]) -> None:
        self.interval = interval
        self.fn = fn
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.fn()


class FakeRunner:
    """Neemt argv op en simuleert een interface met een wisselend type (managed/monitor).

    - `iw dev <if> info` geeft de fixture voor het huidige `mode`;
    - `iw dev <if> set type <t>` verandert het mode;
    - `responses` mapt een exacte argv-tuple naar een RunResult (overschrijft de simulatie);
    - `fail_prefixes`: argv die met dit prefix begint geeft returncode 1.
    """

    def __init__(self, mode: str = "managed") -> None:
        self.mode = mode
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.spawned: list[FakeHandle] = []
        self.spawn_kwargs: list[dict[str, Any]] = []
        self.responses: dict[tuple[str, ...], RunResult] = {}
        self.fail_prefixes: list[tuple[str, ...]] = []
        self.spawn_hook: Callable[[FakeHandle], None] | None = None

    def run(self, argv: Sequence[str], timeout: float) -> RunResult:
        a = list(argv)
        self.calls.append(a)
        self.timeouts.append(timeout)
        for prefix in self.fail_prefixes:
            if tuple(a[: len(prefix)]) == prefix:
                return RunResult(1, "", "command failed: Operation not permitted (-1)")
        if tuple(a) in self.responses:
            return self.responses[tuple(a)]
        if a[0] == "iw" and a[1] == "dev" and a[3] == "info":
            name = (
                "iw_dev_info_monitor.txt" if self.mode == "monitor" else "iw_dev_info_managed.txt"
            )
            return RunResult(0, fixture(name), "")
        if a[0] == "iw" and a[1] == "dev" and a[3:5] == ["set", "type"]:
            self.mode = a[5]
            return RunResult(0, "", "")
        if a[0] == "iw" and a[3] == "scan":
            return RunResult(0, fixture("iw_scan.txt"), "")
        if a[0] == "ip" and a[1] == "link":
            return RunResult(0, "", "")
        raise AssertionError(f"onverwachte argv: {a}")

    def spawn(
        self, argv: Sequence[str], cwd: Path | None = None, stderr_name: str | None = None
    ) -> FakeHandle:
        handle = FakeHandle(list(argv), cwd)
        if cwd is not None:
            handle._stderr_path = cwd / (stderr_name or f"{argv[0]}.stderr.log")
        self.spawned.append(handle)
        self.spawn_kwargs.append({"cwd": cwd, "stderr_name": stderr_name})
        self.calls.append(list(argv))
        if self.spawn_hook is not None:
            self.spawn_hook(handle)
        return handle
