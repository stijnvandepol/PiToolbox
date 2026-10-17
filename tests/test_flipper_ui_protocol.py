"""Protocol FAP <-> Pi (docs/fase4-protocol.md): codec, grenzen, reassembly, handles."""

from __future__ import annotations

import random
import struct
from typing import Any

import pytest

from pitoolbox.core.tools import build_registry
from pitoolbox.frontends.flipper_ui import protocol as p

# --- hulpjes ----------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _rng(seed: int) -> random.Random:
    # Vaste seed: reproduceerbare fuzz, geen cryptografie.
    return random.Random(seed)  # noqa: S311


def _hdr(msg_type: int, seq: int = 1, *, magic: int = p.MAGIC, version: int = 1) -> bytes:
    return struct.pack("<BBBH", magic, version, msg_type, seq)


FAP_SAMPLES: list[p.Message] = [
    p.Hello(seq=1, app_major=1, app_minor=2, proto_max=1, instance=0xDEADBEEF, max_rx=512),
    p.StatusRequest(seq=2),
    p.PresetsRequest(seq=3),
    p.Action(seq=4, kind=p.ActionKind.QUICK, action_id=2),
    p.Action(seq=5, kind=p.ActionKind.PRESET, action_id=255),
    p.ConfirmAnswer(seq=6, handle=1, approve=True),
    p.ConfirmAnswer(seq=7, handle=0xFFFFFFFF, approve=False),
    p.Cancel(seq=65535, ref=4),
]

PI_SAMPLES: list[p.Message] = [
    p.Welcome(seq=1, proto=1, instance=7, confirm_on_flipper=True, agent_available=False),
    p.Status(
        seq=2,
        uplink_up=True,
        undervoltage_now=False,
        undervoltage_occurred=True,
        power_unknown=False,
        link=p.Link.BLE,
        captures=2,
        pending=1,
        last_result=p.LastResult.OK,
        last_age_s=12,
        uplink="wlan0 192.168.1.20",
        last_tool="wifi_scan",
    ),
    p.Status(
        seq=3,
        uplink_up=False,
        undervoltage_now=False,
        undervoltage_occurred=False,
        power_unknown=True,
        link=p.Link.UNKNOWN,
        captures=0,
        pending=0,
        last_result=p.LastResult.NONE,
        last_age_s=None,
        uplink="",
        last_tool="",
    ),
    p.Presets(seq=4, items=(p.Preset(id=1, label="Wat zie je?"), p.Preset(id=9, label="x"))),
    p.Presets(seq=5, items=()),
    p.ConfirmPrompt(seq=6, handle=42, ttl_s=60, summary="IR 'Power' naar tv\nwoonkamer"),
    p.ConfirmClosed(seq=7, handle=42, reason=p.CloseReason.EXPIRED),
    p.Progress(seq=8, ref=4, text="scannen..."),
    p.ResultChunk(seq=9, ref=4, part=0, total=2, text="regel 1\nregel 2"),
    p.ResultChunk(seq=10, ref=0, part=0, total=1, text=""),
    p.Error(seq=11, ref=4, code=p.ErrorCode.BUSY, text="bezig met vorige actie"),
]


# --- roundtrip en vaste bytes -----------------------------------------------------------------


@pytest.mark.parametrize("msg", FAP_SAMPLES, ids=lambda m: type(m).__name__)
def test_fap_roundtrip(msg: p.Message) -> None:
    data = p.encode(msg)
    assert len(data) <= p.MAX_MSG
    assert p.decode_from_fap(data) == msg


@pytest.mark.parametrize("msg", PI_SAMPLES, ids=lambda m: type(m).__name__)
def test_pi_roundtrip(msg: p.Message) -> None:
    data = p.encode(msg)
    assert len(data) <= p.MAX_MSG
    assert p.decode_from_pi(data) == msg


# Vaste vectoren: dezelfde bytes staan in docs/fase4-protocol.md en zijn de referentie voor
# ptb_proto.c. Verandert er hier iets, dan is het protocol veranderd.
@pytest.mark.parametrize(
    ("msg", "hexstr"),
    [
        (
            p.Hello(seq=1, app_major=1, app_minor=0, proto_max=1, instance=0x11223344, max_rx=512),
            "5001010100" + "010001" + "44332211" + "0002",
        ),
        (p.Action(seq=0x0102, kind=p.ActionKind.QUICK, action_id=4), "5001040201" + "0104"),
        (p.ConfirmAnswer(seq=3, handle=0xA1B2C3D4, approve=True), "5001050300" + "d4c3b2a1" + "01"),
        (
            p.ConfirmPrompt(seq=2, handle=0xA1B2C3D4, ttl_s=60, summary="IR tv"),
            "5001840200" + "d4c3b2a1" + "3c" + "05" + b"IR tv".hex(),
        ),
        (
            p.ResultChunk(seq=5, ref=7, part=1, total=3, text="ok"),
            "5001870500" + "0700" + "01" + "03" + b"ok".hex(),
        ),
        (
            p.Welcome(seq=1, proto=1, instance=0x11223344, confirm_on_flipper=True),
            "5001810100" + "01" + "44332211" + "01",
        ),
    ],
)
def test_known_vectors(msg: p.Message, hexstr: str) -> None:
    assert p.encode(msg).hex() == hexstr
    decoded = p.decode_from_fap(bytes.fromhex(hexstr)) if msg.TYPE < 0x80 else None
    if decoded is None:
        decoded = p.decode_from_pi(bytes.fromhex(hexstr))
    assert decoded == msg


def test_largest_messages_fit() -> None:
    biggest: list[p.Message] = [
        p.Presets(
            seq=1, items=tuple(p.Preset(id=i, label="L" * p.MAX_LABEL) for i in range(1, 13))
        ),
        p.ConfirmPrompt(seq=1, handle=1, ttl_s=p.MAX_TTL_S, summary="s" * p.MAX_SUMMARY),
        p.ResultChunk(seq=1, ref=1, part=7, total=8, text="r" * p.MAX_CHUNK),
        p.Error(seq=1, ref=1, code=p.ErrorCode.TIMEOUT, text="e" * p.MAX_ERROR_TEXT),
        p.Progress(seq=1, ref=1, text="p" * p.MAX_PROGRESS),
        p.Status(
            seq=1,
            uplink_up=True,
            undervoltage_now=True,
            undervoltage_occurred=True,
            power_unknown=True,
            link=p.Link.USB,
            captures=255,
            pending=255,
            last_result=p.LastResult.FAILED,
            last_age_s=65534,
            uplink="u" * p.MAX_UPLINK,
            last_tool="t" * p.MAX_TOOL_NAME,
        ),
    ]
    for msg in biggest:
        assert len(p.encode(msg)) <= p.MAX_MSG, type(msg).__name__
    assert len(p.encode(biggest[2])) == 509


# --- modelvalidatie (Pi-kant bouwt alleen geldige berichten) ----------------------------------

_HELLO: dict[str, Any] = {
    "seq": 1,
    "app_major": 1,
    "app_minor": 0,
    "proto_max": 1,
    "instance": 1,
    "max_rx": 512,
}


@pytest.mark.parametrize(
    ("cls", "kwargs"),
    [
        (p.StatusRequest, {"seq": 0}),
        (p.StatusRequest, {"seq": 65536}),
        (p.StatusRequest, {"seq": 1, "extra": 1}),
        (p.Hello, {**_HELLO, "instance": 0}),
        (p.Hello, {**_HELLO, "max_rx": 513}),
        (p.Hello, {**_HELLO, "proto_max": 0}),
        (p.Action, {"seq": 1, "kind": p.ActionKind.QUICK, "action_id": 0}),
        (p.Action, {"seq": 1, "kind": 1, "action_id": 1}),  # strict: enum, geen int
        (p.ConfirmAnswer, {"seq": 1, "handle": 1, "approve": 1}),  # strict: bool
        (p.Cancel, {"seq": 1, "ref": 0}),
        (p.ConfirmPrompt, {"seq": 1, "handle": 1, "ttl_s": 0, "summary": "x"}),
        (p.ConfirmPrompt, {"seq": 1, "handle": 1, "ttl_s": 121, "summary": "x"}),
        (p.ConfirmPrompt, {"seq": 1, "handle": 1, "ttl_s": 60, "summary": ""}),
        (p.ConfirmPrompt, {"seq": 1, "handle": 1, "ttl_s": 60, "summary": "x" * 201}),
        (p.ConfirmPrompt, {"seq": 1, "handle": 1, "ttl_s": 60, "summary": "zenden naar tv ë"}),
        (p.ConfirmPrompt, {"seq": 1, "handle": 1, "ttl_s": 60, "summary": "a\tb"}),
        (p.Progress, {"seq": 1, "ref": 1, "text": "twee\nregels"}),
        (p.ResultChunk, {"seq": 1, "ref": 1, "part": 2, "total": 2, "text": ""}),
        (p.ResultChunk, {"seq": 1, "ref": 1, "part": 0, "total": 9, "text": ""}),
        (p.ResultChunk, {"seq": 1, "ref": 1, "part": 0, "total": 1, "text": "x" * 501}),
        (p.Error, {"seq": 1, "ref": 1, "code": p.ErrorCode.BUSY, "text": "\x1b[31m"}),
        (p.Preset, {"id": 1, "label": ""}),
        (p.Preset, {"id": 0, "label": "x"}),
        (p.Preset, {"id": 1, "label": "x" * 25}),
    ],
)
def test_model_rejects(cls: type[Any], kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        cls(**kwargs)


def test_presets_limits() -> None:
    with pytest.raises(ValueError, match="uniek"):
        p.Presets(seq=1, items=(p.Preset(id=1, label="a"), p.Preset(id=1, label="b")))
    with pytest.raises(ValueError):
        p.Presets(seq=1, items=tuple(p.Preset(id=i, label="a") for i in range(1, 14)))


def test_messages_are_frozen() -> None:
    msg = p.Action(seq=1, kind=p.ActionKind.QUICK, action_id=1)
    with pytest.raises(ValueError):
        msg.action_id = 2  # type: ignore[misc]


# --- decoder: strikt en zonder verrassingen ---------------------------------------------------


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"", "korter dan de kop"),
        (b"\x50\x01\x02\x01", "korter dan de kop"),
        (_hdr(0x02, magic=0x51), "magic"),
        (_hdr(0x02, seq=0), "seq 0"),
        (_hdr(0x7F), "onbekend berichttype"),
        (_hdr(0x81) + b"\x01" + b"\x07\x00\x00\x00" + b"\x00", "deze richting"),  # WELCOME
        (_hdr(0x02) + b"\x00", "bytes over"),
        (_hdr(0x04) + b"\x01", "te kort"),
        (_hdr(0x04) + b"\x03\x01", "actiesoort"),
        (_hdr(0x04) + b"\x01\x00", "action_id"),
        (_hdr(0x05) + b"\x01\x00\x00\x00\x02", "0 of 1"),
        (_hdr(0x05) + b"\x00\x00\x00\x00\x01", "handle"),
        (_hdr(0x06) + b"\x00\x00", "ref"),
        (_hdr(0x01) + bytes([1, 0, 1]) + b"\x01\x00\x00\x00" + b"\x3f\x00", "max_rx"),
        (_hdr(0x01) + bytes([1, 0, 0]) + b"\x01\x00\x00\x00" + b"\x00\x02", "proto_max"),
        (_hdr(0x02) + b"\x00" * 600, "> 512"),
    ],
)
def test_decode_from_fap_rejects(data: bytes, fragment: str) -> None:
    with pytest.raises(p.ProtocolError, match=fragment):
        p.decode_from_fap(data)


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (_hdr(0x04) + b"\x01\x01", "deze richting"),  # ACTION van de Pi naar de FAP? nee
        (_hdr(0x81) + b"\x01\x07\x00\x00\x00\x04", "WELCOME-vlaggen"),
        (_hdr(0x82) + b"\x10" + bytes(6) + b"\x00\x00", "STATUS-vlaggen"),
        (_hdr(0x82) + b"\x00\x03" + bytes(5) + b"\x00\x00", "link"),
        (_hdr(0x82) + b"\x00\x00\x00\x00\x03\x00\x00" + b"\x00\x00", "last_result"),
        (_hdr(0x82) + b"\x00\x00\x00\x00\x00\x00\x00" + b"\x05ab", "te kort"),
        (_hdr(0x83) + b"\x0d" + b"\x01\x01a" * 13, "te veel presets"),
        (_hdr(0x83) + b"\x02\x01\x01a\x01\x01b", "uniek"),
        (_hdr(0x83) + b"\x01\x01\x00", "label"),
        (_hdr(0x84) + b"\x01\x00\x00\x00\x3c\x02\xc3\xab", "ASCII"),
        (_hdr(0x84) + b"\x01\x00\x00\x00\x00\x01x", "ttl_s"),
        (_hdr(0x85) + b"\x01\x00\x00\x00\x06", "reden"),
        (_hdr(0x86) + b"\x01\x00\x02a\n", "ASCII"),
        (_hdr(0x87) + b"\x01\x00\x02\x02", "part 2 >= total 2"),
        (_hdr(0x87) + b"\x01\x00\x00\x00", "total"),
        (_hdr(0x87) + b"\x01\x00\x00\x01" + b"\x00", "ASCII"),
        (_hdr(0x87) + b"\x01\x00\x00\x01" + b"x" * 504, "> 512"),
        (_hdr(0x88) + b"\x01\x00\x00\x00", "foutcode"),
    ],
)
def test_decode_from_pi_rejects(data: bytes, fragment: str) -> None:
    with pytest.raises(p.ProtocolError, match=fragment):
        p.decode_from_pi(data)


def test_unknown_version_keeps_seq_for_error_reply() -> None:
    with pytest.raises(p.ProtocolError) as info:
        p.decode_from_fap(_hdr(0x01, seq=77, version=2) + bytes(9))
    assert info.value.code is p.ErrorCode.UNSUPPORTED_VERSION
    assert info.value.seq == 77


def test_body_error_keeps_seq() -> None:
    with pytest.raises(p.ProtocolError) as info:
        p.decode_from_fap(_hdr(0x04, seq=9) + b"\x09\x01")
    assert info.value.code is p.ErrorCode.INVALID_MESSAGE
    assert info.value.seq == 9


def test_decode_accepts_bytearray_and_memoryview() -> None:
    data = p.encode(p.StatusRequest(seq=3))
    assert p.decode_from_fap(bytearray(data)) == p.StatusRequest(seq=3)
    assert p.decode_from_fap(memoryview(data)) == p.StatusRequest(seq=3)


def test_decode_rejects_non_bytes() -> None:
    with pytest.raises(p.ProtocolError, match="bytes verwacht"):
        p.decode_from_fap("5001020100")  # type: ignore[arg-type]


def test_fuzz_random_bytes_only_protocol_errors() -> None:
    rng = _rng(1234)
    for _ in range(5000):
        n = rng.choice([0, 1, 4, 5, 6, 9, 12, 40, 300, 512, 513])
        data = bytes(rng.getrandbits(8) for _ in range(n))
        if rng.random() < 0.7 and n >= 5:  # vaker een geldige kop, dan komt de body aan bod
            data = bytes([p.MAGIC, 1, rng.choice(list(p.MsgType)), 1, 0]) + data[5:]
        for fn in (p.decode_from_fap, p.decode_from_pi):
            try:
                msg = fn(data)
            except p.ProtocolError:
                continue
            assert p.encode(msg) == data  # wat geaccepteerd wordt, is canoniek


@pytest.mark.parametrize("msg", FAP_SAMPLES + PI_SAMPLES, ids=lambda m: type(m).__name__)
def test_fuzz_mutations_and_truncations(msg: p.Message) -> None:
    rng = _rng(msg.seq)
    data = p.encode(msg)
    decode = p.decode_from_fap if msg.TYPE < 0x80 else p.decode_from_pi
    for cut in range(len(data)):
        try:
            got = decode(data[:cut])
        except p.ProtocolError:
            continue
        # Alleen RESULT (tekst = rest van het bericht) mag ingekort nog geldig zijn.
        assert isinstance(got, p.ResultChunk)
    for _ in range(300):
        mutated = bytearray(data)
        for _ in range(rng.randint(1, 3)):
            mutated[rng.randrange(len(mutated))] = rng.getrandbits(8)
        try:
            got = decode(bytes(mutated))
        except p.ProtocolError:
            continue
        assert p.encode(got) == bytes(mutated)


# --- tekst voor het scherm --------------------------------------------------------------------


def test_to_display_text_transliterates() -> None:
    raw = "Café “Ünïcode” – 5 € … • →  klaar\tnu"
    assert p.to_display_text(raw, max_len=100) == 'Cafe "Unicode" - 5 EUR ... * ->  klaar nu'


def test_to_display_text_controls_and_newlines() -> None:
    raw = "a\r\nb\rc\x1b[0m\x00漢"
    assert p.to_display_text(raw, max_len=50, multiline=True) == "a\nb\nc?[0m??"
    assert p.to_display_text(raw, max_len=50) == "a b c?[0m??"


def test_to_display_text_truncates() -> None:
    assert p.to_display_text("abcdefghij", max_len=6) == "abc..."
    assert p.to_display_text("abcdefghij", max_len=2) == "ab"
    assert p.to_display_text("abc", max_len=3) == "abc"
    with pytest.raises(ValueError):
        p.to_display_text("x", max_len=-1)


def test_to_display_text_output_always_valid_for_fields() -> None:
    rng = _rng(5)
    alphabet = "aé\n\t“”€漢\x00\x7f\U0001f600 Zz~"
    for _ in range(300):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))
        summary = p.to_display_text(raw, max_len=p.MAX_SUMMARY, multiline=True) or "?"
        p.ConfirmPrompt(seq=1, handle=1, ttl_s=1, summary=summary)
        p.Progress(seq=1, ref=0, text=p.to_display_text(raw, max_len=p.MAX_PROGRESS))


# --- volgnummers en rate limit ----------------------------------------------------------------


def test_seq_counter_wraps_and_skips_zero() -> None:
    c = p.SeqCounter(start=65534)
    assert [c.next(), c.next(), c.next()] == [65535, 1, 2]


def test_seq_tracker_duplicates_gaps_and_wrap() -> None:
    t = p.SeqTracker()
    assert t.accept(10) == 0  # eerste bericht zet de basis
    assert t.accept(11) == 0
    assert t.accept(11) is None  # duplicaat
    assert t.accept(5) is None  # oud
    assert t.accept(14) == 2  # 12 en 13 ontbreken
    t2 = p.SeqTracker()
    t2.accept(65534)
    assert t2.accept(65535) == 0
    assert t2.accept(1) == 0  # wrap: 0 bestaat niet
    assert t2.accept(65535) is None
    t2.reset()
    assert t2.accept(3) == 0
    with pytest.raises(ValueError):
        t2.accept(0)


def test_seq_tracker_far_jump_is_old() -> None:
    t = p.SeqTracker()
    t.accept(100)
    assert t.accept(100 + 40000) is None  # meer dan een half bereik vooruit = achterlopend


def test_rate_limiter() -> None:
    clock = FakeClock()
    rl = p.RateLimiter(rate=5, burst=5, clock=clock)
    assert [rl.allow() for _ in range(6)] == [True] * 5 + [False]
    assert rl.dropped == 1
    clock.t += 0.2  # één token terug
    assert rl.allow() is True
    assert rl.allow() is False
    clock.t += 10  # nooit meer dan burst
    assert sum(rl.allow() for _ in range(10)) == 5
    with pytest.raises(ValueError):
        p.RateLimiter(rate=0)


# --- chunking en reassembly -------------------------------------------------------------------


def test_chunk_result_roundtrip_via_bytes() -> None:
    seq = p.SeqCounter()
    text = "\n".join(f"AP {i}: Wifi “thuis” – {-40 - i} dBm" for i in range(200))
    chunks = p.chunk_result(9, text, seq)
    assert 1 < len(chunks) <= p.MAX_PARTS
    assert [c.seq for c in chunks] == list(range(1, len(chunks) + 1))
    r = p.Reassembler()
    out = None
    for c in chunks:
        data = p.encode(c)
        assert len(data) <= p.MAX_MSG
        decoded = p.decode_from_pi(data)
        assert isinstance(decoded, p.ResultChunk)
        out = r.feed(decoded)
    expected = p.to_text_box(text, max_len=p.MAX_RESULT)  # afgebroken vóór het opsplitsen
    assert out == expected
    assert out.endswith("...")  # 200 regels passen niet in 4000 bytes


def test_chunk_result_small_and_empty() -> None:
    seq = p.SeqCounter()
    [only] = p.chunk_result(1, "", seq)
    assert (only.part, only.total, only.text) == (0, 1, "")
    chunks = p.chunk_result(2, "x" * 200, seq, max_msg=64)  # 55 tekstbytes per deel
    assert all(len(p.encode(c)) <= 64 for c in chunks)
    joined = "".join(c.text for c in chunks)
    assert len(chunks) == 4 and joined.replace("\n", "") == "x" * 200
    assert all(p.text_width(line) <= 120 for line in joined.split("\n"))
    chunks = p.chunk_result(3, "x" * 500, seq, max_msg=64)  # past niet in 8 x 55
    assert len(chunks) == 8 and chunks[-1].text.endswith("...")
    with pytest.raises(ValueError):
        p.chunk_result(1, "x", seq, max_msg=63)
    with pytest.raises(ValueError):
        p.chunk_result(1, "x", seq, max_parts=9)


def _parts(ref: int, texts: list[str]) -> list[p.ResultChunk]:
    return [
        p.ResultChunk(seq=i + 1, ref=ref, part=i, total=len(texts), text=t)
        for i, t in enumerate(texts)
    ]


def test_reassembler_out_of_order_and_duplicates() -> None:
    r = p.Reassembler()
    a, b, c = _parts(5, ["aa", "bb", "cc"])
    assert r.feed(c) is None
    assert r.feed(a) is None
    assert r.feed(a) is None  # identiek duplicaat: genegeerd
    assert r.feed(b) == "aabbcc"
    assert r.open_refs == []
    assert r.feed(b) is None  # laat duplicaat na afronden
    [single] = _parts(6, ["los"])
    assert r.feed(single) == "los"


def test_reassembler_conflicting_duplicate() -> None:
    r = p.Reassembler()
    a, _ = _parts(5, ["aa", "bb"])
    r.feed(a)
    with pytest.raises(p.ProtocolError, match="andere tekst"):
        r.feed(p.ResultChunk(seq=9, ref=5, part=0, total=2, text="XX"))
    assert r.open_refs == []


def test_reassembler_total_mismatch() -> None:
    r = p.Reassembler()
    r.feed(p.ResultChunk(seq=1, ref=1, part=0, total=3, text="a"))
    with pytest.raises(p.ProtocolError, match="total wisselt"):
        r.feed(p.ResultChunk(seq=2, ref=1, part=1, total=2, text="b"))
    assert r.open_refs == []


def test_reassembler_missing_part_expires() -> None:
    clock = FakeClock()
    r = p.Reassembler(timeout_s=30, clock=clock)
    a, _, c = _parts(3, ["a", "b", "c"])
    r.feed(a)
    r.feed(c)
    clock.t += 29
    assert r.expire() == []
    clock.t += 2
    assert r.expire() == [3]
    assert r.open_refs == []


def test_reassembler_limits() -> None:
    r = p.Reassembler(max_parts=2, max_bytes=5, max_open=2)
    with pytest.raises(p.ProtocolError, match="delen"):
        r.feed(p.ResultChunk(seq=1, ref=1, part=0, total=3, text="a"))
    r.feed(p.ResultChunk(seq=2, ref=2, part=0, total=2, text="abc"))
    with pytest.raises(p.ProtocolError, match="groter dan 5"):
        r.feed(p.ResultChunk(seq=3, ref=2, part=1, total=2, text="def"))
    r.feed(p.ResultChunk(seq=4, ref=3, part=0, total=2, text="a"))
    r.feed(p.ResultChunk(seq=5, ref=4, part=0, total=2, text="a"))
    with pytest.raises(p.ProtocolError, match="open antwoorden"):
        r.feed(p.ResultChunk(seq=6, ref=5, part=0, total=2, text="a"))
    r.drop(3)
    r.feed(p.ResultChunk(seq=7, ref=5, part=0, total=2, text="a"))  # weer ruimte


def test_reassembler_expired_slots_are_reused() -> None:
    clock = FakeClock()
    r = p.Reassembler(max_open=1, timeout_s=5, clock=clock)
    r.feed(p.ResultChunk(seq=1, ref=1, part=0, total=2, text="a"))
    clock.t += 6
    r.feed(p.ResultChunk(seq=2, ref=2, part=0, total=2, text="b"))
    assert r.open_refs == [2]


# --- bevestigings-handles ---------------------------------------------------------------------


def test_handles_once_within_ttl_same_instance() -> None:
    clock = FakeClock()
    h: p.ConfirmHandles[str] = p.ConfirmHandles(clock=clock)
    handle = h.issue("pending-a", instance=7, ttl_s=60)
    assert 1 <= handle <= 0xFFFFFFFF
    assert h.take(handle, instance=8) is None  # andere FAP-instance
    assert h.take(handle, instance=7) == "pending-a"
    assert h.take(handle, instance=7) is None  # eenmalig
    assert h.take(12345, instance=7) is None  # nooit uitgegeven


def test_handles_expire() -> None:
    clock = FakeClock()
    h: p.ConfirmHandles[str] = p.ConfirmHandles(clock=clock)
    a = h.issue("a", instance=1, ttl_s=10)
    b = h.issue("b", instance=1, ttl_s=100)
    clock.t += 10
    assert h.take(a, instance=1) is None  # precies op de TTL: verlopen
    assert h.expire() == []
    clock.t += 90
    assert h.expire() == [(b, "b")]
    assert len(h) == 0


def test_handles_revoke_and_uniqueness() -> None:
    h: p.ConfirmHandles[int] = p.ConfirmHandles(max_open=1000)
    handles = {h.issue(i, instance=1, ttl_s=60) for i in range(500)}
    assert len(handles) == 500 and 0 not in handles
    first = next(iter(handles))
    assert h.revoke(first) is not None
    assert h.revoke(first) is None
    assert len(h.revoke_all()) == 499
    assert all(h.take(x, instance=1) is None for x in handles)


def test_handles_limits() -> None:
    h: p.ConfirmHandles[str] = p.ConfirmHandles(max_open=2)
    h.issue("a", instance=1, ttl_s=1)
    h.issue("b", instance=1, ttl_s=1)
    with pytest.raises(p.ProtocolError) as info:
        h.issue("c", instance=1, ttl_s=1)
    assert info.value.code is p.ErrorCode.BUSY
    with pytest.raises(ValueError):
        h.issue("d", instance=1, ttl_s=0)


# --- vaste koppeling snelle acties -> tools ---------------------------------------------------


def test_quick_actions_map_to_real_tools_with_valid_args() -> None:
    registry = build_registry()
    assert set(p.QUICK_ACTIONS) == set(p.QuickAction)
    for action, plan in p.QUICK_ACTIONS.items():
        spec = registry.get(plan.tool)
        assert spec is not None, action
        args = dict(plan.args)
        if plan.per_active_capture:
            args["capture_id"] = "abcdef"
        spec.params.model_validate(args)
        assert len(plan.label) <= p.MAX_LABEL
        with pytest.raises(TypeError):
            plan.args["x"] = 1  # type: ignore[index]


def test_quick_action_plan_lookup() -> None:
    ok = p.Action(seq=3, kind=p.ActionKind.QUICK, action_id=4)
    assert p.quick_action_plan(ok).tool == "emergency_stop"
    with pytest.raises(p.ProtocolError) as info:
        p.quick_action_plan(p.Action(seq=4, kind=p.ActionKind.QUICK, action_id=99))
    assert info.value.code is p.ErrorCode.UNKNOWN_ACTION and info.value.seq == 4
    with pytest.raises(ValueError, match="geen snelle"):
        p.quick_action_plan(p.Action(seq=5, kind=p.ActionKind.PRESET, action_id=1))
