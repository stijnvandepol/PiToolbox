"""Framing: varint en nanopb-delimited frames, incrementeel gedecodeerd."""

from __future__ import annotations

import pytest

from pitoolbox.core.devices._flipper_rpc.framing import (
    FrameDecoder,
    FrameError,
    encode_frame,
    encode_varint,
)
from pitoolbox.core.devices._flipper_rpc.pb import flipper_pb2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
        (16384, b"\x80\x80\x01"),
    ],
)
def test_encode_varint_known_values(value: int, expected: bytes) -> None:
    assert encode_varint(value) == expected


def test_encode_varint_rejects_negative() -> None:
    with pytest.raises(ValueError):
        encode_varint(-1)


@pytest.mark.parametrize("size", [1, 2, 127, 128, 129, 16383, 16384, 20000])
def test_roundtrip_various_sizes(size: int) -> None:
    payload = bytes(i % 251 for i in range(size))
    assert FrameDecoder().feed(encode_frame(payload)) == [payload]


def test_partial_feed_byte_by_byte() -> None:
    payload = b"x" * 200  # lengteprefix van twee bytes
    wire = encode_frame(payload)
    decoder = FrameDecoder()
    out: list[bytes] = []
    for i, byte in enumerate(wire):
        frames = decoder.feed(bytes([byte]))
        if i < len(wire) - 1:
            assert frames == []
        out.extend(frames)
    assert out == [payload]
    assert decoder.buffered == 0


def test_multiple_frames_and_trailing_partial_in_one_chunk() -> None:
    a, b, c = b"aaa", b"b" * 130, b"cccc"
    wire = encode_frame(a) + encode_frame(b) + encode_frame(c)
    cut = len(wire) - 2
    decoder = FrameDecoder()
    assert decoder.feed(wire[:cut]) == [a, b]
    assert decoder.buffered > 0
    assert decoder.feed(wire[cut:]) == [c]


def test_roundtrip_real_main_message() -> None:
    main = flipper_pb2.Main()
    main.command_id = 7
    main.system_ping_request.data = b"hallo"
    [payload] = FrameDecoder().feed(encode_frame(main.SerializeToString()))
    parsed = flipper_pb2.Main()
    parsed.ParseFromString(payload)
    assert parsed.command_id == 7
    assert parsed.system_ping_request.data == b"hallo"


def test_max_frame_size_guard_clears_buffer() -> None:
    decoder = FrameDecoder(max_frame_size=100)
    with pytest.raises(FrameError, match="limiet"):
        decoder.feed(encode_varint(101) + b"zzz")
    assert decoder.buffered == 0
    # decoder is na de fout weer bruikbaar
    assert decoder.feed(encode_frame(b"ok")) == [b"ok"]


def test_frame_at_exact_limit_is_accepted() -> None:
    payload = b"q" * 100
    assert FrameDecoder(max_frame_size=100).feed(encode_frame(payload)) == [payload]


def test_overlong_varint_is_error() -> None:
    with pytest.raises(FrameError, match="varint"):
        FrameDecoder().feed(b"\xff\xff\xff\xff\xff\x01")


def test_zero_length_frame_is_error() -> None:
    with pytest.raises(FrameError, match="lengte 0"):
        FrameDecoder().feed(b"\x00")


def test_cli_text_is_detected_as_desync() -> None:
    # Prompt-tekst als RPC lezen: '\r' = lengte 13, '>' ... wordt een enorm of onleesbaar frame.
    decoder = FrameDecoder(max_frame_size=8)
    with pytest.raises(FrameError):
        decoder.feed(b"\r\n>: ")


def test_invalid_max_frame_size() -> None:
    with pytest.raises(ValueError):
        FrameDecoder(max_frame_size=0)
