"""Tests voor de audit-log: één JSON-regel per call, inkorten en thread-safety."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pitoolbox.core.audit import MAX_ITEMS, MAX_STR, AuditLog


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_creates_parent_dir_and_writes_one_line_per_call(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "dir" / "audit.jsonl"
    log = AuditLog(path)
    assert log.path == path
    log.write(frontend="mcp", tool="a", decision="allow", ok=True)
    log.write(frontend="mcp", tool="b", decision="deny", ok=False, error="denied")
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    records = _lines(path)
    assert [r["tool"] for r in records] == ["a", "b"]
    assert records[1]["error"] == "denied"
    assert records[0]["ts"].endswith("+00:00")


def test_write_returns_record_and_keeps_unicode(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    record = log.write(tool="x", result="é – ünïcode")
    assert record["tool"] == "x"
    assert "é – ünïcode" in (tmp_path / "a.jsonl").read_text(encoding="utf-8")


def test_appends_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    AuditLog(path).write(tool="1")
    AuditLog(path).write(tool="2")
    assert [r["tool"] for r in _lines(path)] == ["1", "2"]


def test_long_string_is_truncated(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    log.write(tool="t", args={"data": "x" * (MAX_STR + 100)}, short="y" * MAX_STR)
    rec = _lines(tmp_path / "a.jsonl")[0]
    assert rec["args"]["data"] == "x" * MAX_STR + "…(+100)"
    assert rec["short"] == "y" * MAX_STR  # precies op de grens blijft heel


def test_bytes_are_summarized(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    log.write(tool="t", args={"blob": b"\x00" * 1234})
    assert _lines(tmp_path / "a.jsonl")[0]["args"]["blob"] == "<1234 bytes>"


def test_long_list_and_dict_are_capped(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    log.write(
        tool="t",
        args={
            "items": list(range(MAX_ITEMS + 7)),
            "tuple": tuple(range(3)),
            "dict": {f"k{i}": i for i in range(MAX_ITEMS + 5)},
        },
    )
    args = _lines(tmp_path / "a.jsonl")[0]["args"]
    assert args["items"][:MAX_ITEMS] == list(range(MAX_ITEMS))
    assert args["items"][-1] == "…(+7)"
    assert len(args["items"]) == MAX_ITEMS + 1
    assert args["tuple"] == [0, 1, 2]
    assert len(args["dict"]) == MAX_ITEMS


def test_nested_truncation_and_depth_limit(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    deep: Any = "leaf"
    for _ in range(10):
        deep = {"n": deep}
    log.write(tool="t", args={"list": [{"s": "z" * (MAX_STR + 1)}]}, deep=deep)
    rec = _lines(tmp_path / "a.jsonl")[0]
    assert rec["args"]["list"][0]["s"].endswith("…(+1)")
    node = rec["deep"]
    for _ in range(20):
        if not isinstance(node, dict):
            break
        node = node["n"]
    assert node == "…"


def test_unknown_objects_become_strings(tmp_path: Path) -> None:
    class Odd:
        def __str__(self) -> str:
            return "odd-object"

    log = AuditLog(tmp_path / "a.jsonl")
    log.write(tool="t", thing=Odd(), none=None, flag=True, num=1.5)
    rec = _lines(tmp_path / "a.jsonl")[0]
    assert rec["thing"] == "odd-object"
    assert rec["none"] is None and rec["flag"] is True and rec["num"] == 1.5


def test_concurrent_writes_produce_valid_lines(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    log = AuditLog(path)
    threads_n, per_thread = 8, 60
    start = threading.Barrier(threads_n)

    def worker(n: int) -> None:
        start.wait()
        for i in range(per_thread):
            log.write(tool=f"t{n}", seq=i, payload="p" * 200)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = _lines(path)  # elke regel moet geldige JSON zijn
    assert len(records) == threads_n * per_thread
    seen = {(r["tool"], r["seq"]) for r in records}
    assert len(seen) == threads_n * per_thread
