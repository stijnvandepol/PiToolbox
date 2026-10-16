"""Kop en codering van protocol v1 (§2): `encode` en de strikte decoders per richting."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from pitoolbox.frontends.flipper_ui.protocol_messages import (
    Action,
    Cancel,
    ConfirmAnswer,
    ConfirmClosed,
    ConfirmPrompt,
    Error,
    FapMessage,
    Hello,
    Message,
    PiMessage,
    Presets,
    PresetsRequest,
    Progress,
    ResultChunk,
    Status,
    StatusRequest,
    Welcome,
)
from pitoolbox.frontends.flipper_ui.protocol_spec import (
    _HEADER,
    HEADER_SIZE,
    MAGIC,
    MAX_MSG,
    PROTO_VERSION,
    SUPPORTED_VERSIONS,
    ErrorCode,
    MsgType,
    ProtocolError,
)
from pitoolbox.frontends.flipper_ui.protocol_wire import _Reader

_FAP_TYPES: dict[MsgType, type[Message]] = {
    cls.TYPE: cls for cls in (Hello, StatusRequest, PresetsRequest, Action, ConfirmAnswer, Cancel)
}
_PI_TYPES: dict[MsgType, type[Message]] = {
    cls.TYPE: cls
    for cls in (
        Welcome,
        Status,
        Presets,
        ConfirmPrompt,
        ConfirmClosed,
        Progress,
        ResultChunk,
        Error,
    )
}


# --- encode / decode --------------------------------------------------------------------------


def encode(msg: Message) -> bytes:
    """Kop + body. Het model is al gevalideerd; de grootte wordt nog één keer gecontroleerd."""
    data = _HEADER.pack(MAGIC, PROTO_VERSION, msg.TYPE, msg.seq) + msg.body()
    if len(data) > MAX_MSG:
        raise ProtocolError(f"bericht van {len(data)} bytes > {MAX_MSG}", seq=msg.seq)
    return data


def _decode(data: bytes, table: Mapping[MsgType, type[Message]]) -> Message:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtocolError(f"bytes verwacht, niet {type(data).__name__}")
    data = bytes(data)
    if len(data) > MAX_MSG:
        raise ProtocolError(f"bericht van {len(data)} bytes > {MAX_MSG}")
    if len(data) < HEADER_SIZE:
        raise ProtocolError(f"bericht van {len(data)} bytes is korter dan de kop")
    magic, version, raw_type, seq = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ProtocolError(f"verkeerde magic {magic:#04x}")
    if seq == 0:
        raise ProtocolError("seq 0 is ongeldig")
    if version not in SUPPORTED_VERSIONS:
        raise ProtocolError(
            f"protocolversie {version} niet ondersteund",
            code=ErrorCode.UNSUPPORTED_VERSION,
            seq=seq,
        )
    try:
        msg_type = MsgType(raw_type)
    except ValueError:
        raise ProtocolError(f"onbekend berichttype {raw_type:#04x}", seq=seq) from None
    cls = table.get(msg_type)
    if cls is None:
        raise ProtocolError(f"berichttype {msg_type.name} hoort niet in deze richting", seq=seq)
    reader = _Reader(data[HEADER_SIZE:])
    try:
        msg = cls.parse_body(reader, seq)
        reader.done()
    except ProtocolError as exc:
        exc.seq = seq
        raise
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(p) for p in first["loc"]) or msg_type.name
        raise ProtocolError(f"{msg_type.name}: {where}: {first['msg']}", seq=seq) from None
    return msg


def decode_from_fap(data: bytes) -> FapMessage:
    """Decodeer een bericht van de FAP (Pi-kant). Alleen FAP->Pi-types; alles strikt."""
    msg = _decode(data, _FAP_TYPES)
    assert isinstance(msg, FapMessage)  # noqa: S101 - tabel en union zijn gelijk
    return msg


def decode_from_pi(data: bytes) -> PiMessage:
    """Decodeer een bericht van de Pi (voor tests en een nagebootste FAP)."""
    msg = _decode(data, _PI_TYPES)
    assert isinstance(msg, PiMessage)  # noqa: S101
    return msg
