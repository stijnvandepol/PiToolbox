"""Berichtprotocol tussen de Flipper-app (`flipper_app/pitoolbox_ui`) en de Pi, versie 1.

Volledige spec met byte-layouts en de reden voor elke keuze: `docs/fase4-protocol.md`.
De C-kant (`flipper_app/pitoolbox_ui/ptb_proto.c`) volgt dezelfde layouts; bij een wijziging
hier moet die mee.

Kort:

* Elk bericht is één `app_data_exchange_request` (DataExchange, protobuf 0.25) van hoogstens
  `MAX_MSG` = 512 bytes. Gemeten veilig over BLE (fase4-spike.md, test 3).
* Binair: 5 bytes kop (magic 0x50, versie, type, seq u16 LE) plus een vaste body per type.
  Getallen little-endian, tekst als u8-lengte + printbare ASCII. Geen JSON: de FAP zou een
  eigen JSON-parser in C nodig hebben (API 87.1 exporteert er geen), en vaste velden zijn
  aan beide kanten met een paar grenscontroles volledig te valideren.
* De FAP is een **onbetrouwbaar invoerapparaat**. Hij stuurt alleen ids (snelle actie,
  preset, bevestigings-handle), nooit toolnamen, argumenten of vrije tekst. De Pi koppelt
  ids aan vaste tool-calls (`QUICK_ACTIONS`, presets uit de Pi-config) en valideert alles
  hier, voordat de bridge iets doet.

Dit module bevat alleen de codec en kleine bouwstenen voor de bridge (volgnummers,
rate limiting, reassembly, bevestigings-handles). De koppeling aan de runner en de agent-loop
staat in `bridge.py`.
"""

from __future__ import annotations

import secrets
import struct
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Generic, Self, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

# --- constanten (zie de spec, hoofdstuk "Grenzen") --------------------------------------------

MAGIC = 0x50  # 'P'
PROTO_VERSION = 1
SUPPORTED_VERSIONS = frozenset({1})
MAX_MSG = 512
MIN_RX = 64  # kleinste max_rx die een FAP mag melden
_HEADER = struct.Struct("<BBBH")  # magic, versie, type, seq
HEADER_SIZE = _HEADER.size  # 5

MAX_SUMMARY = 200
MAX_LABEL = 24
MAX_PRESETS = 12
MAX_PROGRESS = 64
MAX_ERROR_TEXT = 120
MAX_UPLINK = 32
MAX_TOOL_NAME = 24
_RESULT_OVERHEAD = HEADER_SIZE + 4  # ref u16, part u8, total u8
MAX_CHUNK = 500  # tekstbytes per RESULT-deel; 5 + 4 + 500 = 509 <= 512
MAX_PARTS = 8
MAX_RESULT = MAX_CHUNK * MAX_PARTS  # 4000 bytes: ook de buffer in de FAP
MAX_TTL_S = 120
AGE_UNKNOWN = 0xFFFF

# Rate limit per richting (spike: boven ~10 frames/s loopt er een achterstand op over BLE).
RATE_PER_S = 5.0
RATE_BURST = 5

_U16_MAX = 0xFFFF
_U32_MAX = 0xFFFF_FFFF


class MsgType(IntEnum):
    # FAP -> Pi
    HELLO = 0x01
    STATUS_REQUEST = 0x02
    PRESETS_REQUEST = 0x03
    ACTION = 0x04
    CONFIRM_ANSWER = 0x05
    CANCEL = 0x06
    # Pi -> FAP
    WELCOME = 0x81
    STATUS = 0x82
    PRESETS = 0x83
    CONFIRM_PROMPT = 0x84
    CONFIRM_CLOSED = 0x85
    PROGRESS = 0x86
    RESULT = 0x87
    ERROR = 0x88


class ActionKind(IntEnum):
    QUICK = 1
    PRESET = 2


class QuickAction(IntEnum):
    STATUS = 1
    WIFI_SCAN = 2
    STOP_CAPTURES = 3
    EMERGENCY_STOP = 4


class Link(IntEnum):
    UNKNOWN = 0
    USB = 1
    BLE = 2


class LastResult(IntEnum):
    NONE = 0
    OK = 1
    FAILED = 2


class CloseReason(IntEnum):
    EXECUTED = 1  # bevestigd en uitgevoerd
    REJECTED = 2  # mens zei nee
    EXPIRED = 3  # TTL verstreken
    REVOKED = 4  # elders afgehandeld of gewist (bijv. emergency_stop)
    FAILED = 5  # bevestigd, maar de uitvoering faalde of de scope weigerde


class ErrorCode(IntEnum):
    BUSY = 1
    RATE_LIMITED = 2
    UNKNOWN_ACTION = 3
    INVALID_MESSAGE = 4
    UNSUPPORTED_VERSION = 5
    TOOL_FAILED = 6
    AGENT_UNAVAILABLE = 7
    CANCELLED = 8
    TIMEOUT = 9
    CONFIRM_INVALID = 10
    NOT_READY = 11  # bericht vóór HELLO


# Vlagbits (byte) in WELCOME en STATUS.
_WF_CONFIRM_ON_FLIPPER = 0x01
_WF_AGENT_AVAILABLE = 0x02
_WF_ALL = _WF_CONFIRM_ON_FLIPPER | _WF_AGENT_AVAILABLE
_SF_UPLINK_UP = 0x01
_SF_UNDERVOLTAGE_NOW = 0x02
_SF_UNDERVOLTAGE_OCCURRED = 0x04
_SF_POWER_UNKNOWN = 0x08
_SF_ALL = _SF_UPLINK_UP | _SF_UNDERVOLTAGE_NOW | _SF_UNDERVOLTAGE_OCCURRED | _SF_POWER_UNKNOWN


class ProtocolError(ValueError):
    """Ongeldig bericht. `seq` is gezet als de kop nog leesbaar was (voor een ERROR-antwoord)."""

    def __init__(
        self, message: str, *, code: ErrorCode = ErrorCode.INVALID_MESSAGE, seq: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.seq = seq


# --- tekst ------------------------------------------------------------------------------------

# Het Flipper-font kent alleen ASCII. De Pi zet tekst om vóór het versturen; de decoder
# weigert alles daarbuiten. Newline alleen in samenvatting en resultaat.
_PRINTABLE = frozenset(range(0x20, 0x7F))
_PRINTABLE_NL = _PRINTABLE | {0x0A}


def _check_text(value: str, *, max_len: int, min_len: int = 0, multiline: bool = False) -> str:
    allowed = _PRINTABLE_NL if multiline else _PRINTABLE
    if any(ord(c) not in allowed for c in value):
        raise ValueError("alleen printbare ASCII toegestaan" + (" (en \\n)" if multiline else ""))
    if not min_len <= len(value) <= max_len:
        raise ValueError(f"lengte {len(value)} buiten {min_len}..{max_len}")
    return value


def _v_label(v: str) -> str:
    return _check_text(v, max_len=MAX_LABEL, min_len=1)


def _v_summary(v: str) -> str:
    return _check_text(v, max_len=MAX_SUMMARY, min_len=1, multiline=True)


def _v_progress(v: str) -> str:
    return _check_text(v, max_len=MAX_PROGRESS)


def _v_chunk(v: str) -> str:
    return _check_text(v, max_len=MAX_CHUNK, multiline=True)


def _v_error(v: str) -> str:
    return _check_text(v, max_len=MAX_ERROR_TEXT)


def _v_uplink(v: str) -> str:
    return _check_text(v, max_len=MAX_UPLINK)


def _v_tool(v: str) -> str:
    return _check_text(v, max_len=MAX_TOOL_NAME)


Label = Annotated[str, AfterValidator(_v_label)]
Summary = Annotated[str, AfterValidator(_v_summary)]
ProgressText = Annotated[str, AfterValidator(_v_progress)]
ChunkText = Annotated[str, AfterValidator(_v_chunk)]
ErrorText = Annotated[str, AfterValidator(_v_error)]
UplinkText = Annotated[str, AfterValidator(_v_uplink)]
ToolText = Annotated[str, AfterValidator(_v_tool)]

Seq = Annotated[int, Field(ge=1, le=_U16_MAX)]
Ref = Annotated[int, Field(ge=0, le=_U16_MAX)]  # 0 = geen antwoord op een FAP-bericht
Handle = Annotated[int, Field(ge=1, le=_U32_MAX)]
U8 = Annotated[int, Field(ge=0, le=0xFF)]

_TYPOGRAPHY = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "″": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "…": "...",
        "•": "*",
        "·": "*",
        "→": "->",
        "←": "<-",
        "×": "x",
        "€": "EUR",
        "°": " gr",
        " ": " ",
        "\t": " ",
    }
)


def to_display_text(text: str, *, max_len: int, multiline: bool = False) -> str:
    """Maak willekeurige tekst (toolresultaat, modelantwoord) geschikt voor het Flipper-scherm.

    Typografische tekens worden ASCII, accenten verdwijnen (`ë` -> `e`), al het andere buiten
    printbare ASCII wordt `?`. Te lang wordt afgekapt met `...`. Het resultaat komt altijd
    door de validatie van de berichtvelden met dezelfde `max_len`/`multiline`.
    """
    if max_len < 0:
        raise ValueError("max_len moet >= 0 zijn")
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_TYPOGRAPHY)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    allowed = _PRINTABLE_NL if multiline else _PRINTABLE
    out = "".join(c if ord(c) in allowed else (" " if c == "\n" else "?") for c in text)
    if len(out) > max_len:
        out = out[: max_len - 3] + "..." if max_len >= 3 else out[:max_len]
    return out


# --- lezen ------------------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def _take(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise ProtocolError("bericht te kort")
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return int(struct.unpack("<H", self._take(2))[0])

    def u32(self) -> int:
        return int(struct.unpack("<I", self._take(4))[0])

    def str8(self) -> str:
        return self._ascii(self._take(self.u8()))

    def rest(self) -> str:
        return self._ascii(self._take(len(self._data) - self._pos))

    @staticmethod
    def _ascii(raw: bytes) -> str:
        # Tekens buiten ASCII worden in de modelvalidatie geweigerd; latin-1 decodeert alles.
        return raw.decode("latin-1")

    def done(self) -> None:
        if self._pos != len(self._data):
            raise ProtocolError(f"{len(self._data) - self._pos} bytes over na de body")


def _str8(value: str) -> bytes:
    raw = value.encode("ascii")
    if len(raw) > 0xFF:  # door de modelgrenzen onmogelijk; puur defensief
        raise ProtocolError("tekstveld langer dan 255 bytes")
    return bytes([len(raw)]) + raw


_E = TypeVar("_E", bound=IntEnum)


def _enum(cls: type[_E], value: int, what: str) -> _E:
    try:
        return cls(value)
    except ValueError:
        raise ProtocolError(f"onbekende {what}: {value}") from None


# --- berichten --------------------------------------------------------------------------------


class Message(BaseModel):
    """Basis: elk bericht heeft een volgnummer van de afzender (1..65535, 0 nooit)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    TYPE: ClassVar[MsgType]
    seq: Seq

    def body(self) -> bytes:
        return b""

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        return cls(seq=seq)


# FAP -> Pi ------------------------------------------------------------------------------------


class Hello(Message):
    """Eerste bericht van de FAP na `rpc_system_app_send_started`.

    `instance` is een willekeurig getal per FAP-start: de Pi bindt handles daaraan en wist
    alles van een eerdere instance zodra er een nieuwe groet.
    """

    TYPE = MsgType.HELLO
    app_major: U8
    app_minor: U8
    proto_max: Annotated[int, Field(ge=1, le=0xFF)]
    instance: Handle
    max_rx: Annotated[int, Field(ge=MIN_RX, le=MAX_MSG)]

    def body(self) -> bytes:
        return struct.pack(
            "<BBBIH", self.app_major, self.app_minor, self.proto_max, self.instance, self.max_rx
        )

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        return cls(
            seq=seq,
            app_major=r.u8(),
            app_minor=r.u8(),
            proto_max=r.u8(),
            instance=r.u32(),
            max_rx=r.u16(),
        )


class StatusRequest(Message):
    TYPE = MsgType.STATUS_REQUEST


class PresetsRequest(Message):
    TYPE = MsgType.PRESETS_REQUEST


class Action(Message):
    """Snelle actie of preset, alleen als id. Het antwoord draagt `ref = seq` van dit bericht."""

    TYPE = MsgType.ACTION
    kind: ActionKind
    action_id: Annotated[int, Field(ge=1, le=0xFF)]

    def body(self) -> bytes:
        return bytes([self.kind, self.action_id])

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        kind = _enum(ActionKind, r.u8(), "actiesoort")
        return cls(seq=seq, kind=kind, action_id=r.u8())


class ConfirmAnswer(Message):
    TYPE = MsgType.CONFIRM_ANSWER
    handle: Handle
    approve: bool

    def body(self) -> bytes:
        return struct.pack("<IB", self.handle, 1 if self.approve else 0)

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        handle = r.u32()
        answer = r.u8()
        if answer not in (0, 1):
            raise ProtocolError(f"antwoord moet 0 of 1 zijn, niet {answer}")
        return cls(seq=seq, handle=handle, approve=answer == 1)


class Cancel(Message):
    """De mens drukte Back tijdens een lopende actie; de Pi breekt af als dat kan."""

    TYPE = MsgType.CANCEL
    ref: Annotated[int, Field(ge=1, le=_U16_MAX)]

    def body(self) -> bytes:
        return struct.pack("<H", self.ref)

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        return cls(seq=seq, ref=r.u16())


# Pi -> FAP ------------------------------------------------------------------------------------


class Welcome(Message):
    TYPE = MsgType.WELCOME
    proto: Annotated[int, Field(ge=1, le=0xFF)]
    instance: Handle
    confirm_on_flipper: bool = False
    agent_available: bool = False

    def body(self) -> bytes:
        flags = (_WF_CONFIRM_ON_FLIPPER if self.confirm_on_flipper else 0) | (
            _WF_AGENT_AVAILABLE if self.agent_available else 0
        )
        return struct.pack("<BIB", self.proto, self.instance, flags)

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        proto, instance, flags = r.u8(), r.u32(), r.u8()
        if flags & ~_WF_ALL:
            raise ProtocolError(f"onbekende WELCOME-vlaggen: {flags:#04x}")
        return cls(
            seq=seq,
            proto=proto,
            instance=instance,
            confirm_on_flipper=bool(flags & _WF_CONFIRM_ON_FLIPPER),
            agent_available=bool(flags & _WF_AGENT_AVAILABLE),
        )


class Status(Message):
    """Statusscherm. Vlaggen in één byte; `last_age_s=None` betekent onbekend (0xFFFF)."""

    TYPE = MsgType.STATUS
    uplink_up: bool
    undervoltage_now: bool
    undervoltage_occurred: bool
    power_unknown: bool
    link: Link
    captures: U8
    pending: U8
    last_result: LastResult
    last_age_s: Annotated[int, Field(ge=0, le=AGE_UNKNOWN - 1)] | None
    uplink: UplinkText
    last_tool: ToolText

    def body(self) -> bytes:
        flags = (
            (_SF_UPLINK_UP if self.uplink_up else 0)
            | (_SF_UNDERVOLTAGE_NOW if self.undervoltage_now else 0)
            | (_SF_UNDERVOLTAGE_OCCURRED if self.undervoltage_occurred else 0)
            | (_SF_POWER_UNKNOWN if self.power_unknown else 0)
        )
        age = AGE_UNKNOWN if self.last_age_s is None else self.last_age_s
        head = struct.pack(
            "<BBBBBH", flags, self.link, self.captures, self.pending, self.last_result, age
        )
        return head + _str8(self.uplink) + _str8(self.last_tool)

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        flags = r.u8()
        if flags & ~_SF_ALL:
            raise ProtocolError(f"onbekende STATUS-vlaggen: {flags:#04x}")
        link = _enum(Link, r.u8(), "link")
        captures, pending = r.u8(), r.u8()
        last_result = _enum(LastResult, r.u8(), "last_result")
        age = r.u16()
        return cls(
            seq=seq,
            uplink_up=bool(flags & _SF_UPLINK_UP),
            undervoltage_now=bool(flags & _SF_UNDERVOLTAGE_NOW),
            undervoltage_occurred=bool(flags & _SF_UNDERVOLTAGE_OCCURRED),
            power_unknown=bool(flags & _SF_POWER_UNKNOWN),
            link=link,
            captures=captures,
            pending=pending,
            last_result=last_result,
            last_age_s=None if age == AGE_UNKNOWN else age,
            uplink=r.str8(),
            last_tool=r.str8(),
        )


class Preset(BaseModel):
    """Eén preset zoals de FAP hem ziet: id + label. De prompttekst blijft op de Pi."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: Annotated[int, Field(ge=1, le=0xFF)]
    label: Label


class Presets(Message):
    TYPE = MsgType.PRESETS
    items: tuple[Preset, ...] = Field(max_length=MAX_PRESETS)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [p.id for p in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("preset-ids moeten uniek zijn")
        return self

    def body(self) -> bytes:
        return bytes([len(self.items)]) + b"".join(
            bytes([p.id]) + _str8(p.label) for p in self.items
        )

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        count = r.u8()
        if count > MAX_PRESETS:
            raise ProtocolError(f"te veel presets: {count} > {MAX_PRESETS}")
        items = tuple(Preset(id=r.u8(), label=r.str8()) for _ in range(count))
        return cls(seq=seq, items=items)


class ConfirmPrompt(Message):
    """Vraag de mens om een transmit te bevestigen. `handle` is willekeurig en van de Pi."""

    TYPE = MsgType.CONFIRM_PROMPT
    handle: Handle
    ttl_s: Annotated[int, Field(ge=1, le=MAX_TTL_S)]
    summary: Summary

    def body(self) -> bytes:
        return struct.pack("<IB", self.handle, self.ttl_s) + _str8(self.summary)

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        return cls(seq=seq, handle=r.u32(), ttl_s=r.u8(), summary=r.str8())


class ConfirmClosed(Message):
    TYPE = MsgType.CONFIRM_CLOSED
    handle: Handle
    reason: CloseReason

    def body(self) -> bytes:
        return struct.pack("<IB", self.handle, self.reason)

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        handle = r.u32()
        return cls(seq=seq, handle=handle, reason=_enum(CloseReason, r.u8(), "reden"))


class Progress(Message):
    TYPE = MsgType.PROGRESS
    ref: Ref
    text: ProgressText

    def body(self) -> bytes:
        return struct.pack("<H", self.ref) + _str8(self.text)

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        return cls(seq=seq, ref=r.u16(), text=r.str8())


class ResultChunk(Message):
    """Deel `part` (0-based) van `total` van een antwoord; de tekst is de rest van het bericht."""

    TYPE = MsgType.RESULT
    ref: Ref
    part: Annotated[int, Field(ge=0, le=MAX_PARTS - 1)]
    total: Annotated[int, Field(ge=1, le=MAX_PARTS)]
    text: ChunkText

    @model_validator(mode="after")
    def _part_in_range(self) -> Self:
        if self.part >= self.total:
            raise ValueError(f"part {self.part} >= total {self.total}")
        return self

    def body(self) -> bytes:
        return struct.pack("<HBB", self.ref, self.part, self.total) + self.text.encode("ascii")

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        ref, part, total = r.u16(), r.u8(), r.u8()
        return cls(seq=seq, ref=ref, part=part, total=total, text=r.rest())


class Error(Message):
    TYPE = MsgType.ERROR
    ref: Ref
    code: ErrorCode
    text: ErrorText = ""

    def body(self) -> bytes:
        return struct.pack("<HB", self.ref, self.code) + _str8(self.text)

    @classmethod
    def parse_body(cls, r: _Reader, seq: int) -> Self:
        ref = r.u16()
        code = _enum(ErrorCode, r.u8(), "foutcode")
        return cls(seq=seq, ref=ref, code=code, text=r.str8())


FapMessage = Hello | StatusRequest | PresetsRequest | Action | ConfirmAnswer | Cancel
PiMessage = (
    Welcome | Status | Presets | ConfirmPrompt | ConfirmClosed | Progress | ResultChunk | Error
)

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


# --- volgnummers, rate limit ------------------------------------------------------------------

_SEQ_MOD = _U16_MAX  # seq loopt 1..65535; 0 komt niet voor


class SeqCounter:
    """Volgnummers van één afzender: 1, 2, ..., 65535, 1, ..."""

    def __init__(self, start: int = 0) -> None:
        self._n = start

    def next(self) -> int:
        self._n = self._n % _SEQ_MOD + 1
        return self._n


class SeqTracker:
    """Ontvangstkant: herkent duplicaten en oude berichten (serieel rekenen, RFC 1982).

    `accept(seq)` geeft het aantal overgeslagen nummers (0 = precies de volgende), of None
    als het bericht niet nieuwer is dan het laatst geaccepteerde (duplicaat: weggooien).
    """

    def __init__(self) -> None:
        self._last: int | None = None

    def reset(self) -> None:
        self._last = None

    def accept(self, seq: int) -> int | None:
        if not 1 <= seq <= _U16_MAX:
            raise ValueError(f"seq {seq} buiten 1..65535")
        if self._last is None:
            self._last = seq
            return 0
        distance = (seq - self._last) % _SEQ_MOD
        if distance == 0 or distance >= _SEQ_MOD // 2:
            return None
        self._last = seq
        return distance - 1


class RateLimiter:
    """Token bucket: gemiddeld `rate` berichten per seconde, pieken tot `burst`."""

    def __init__(
        self,
        rate: float = RATE_PER_S,
        burst: int = RATE_BURST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate > 0 en burst >= 1 vereist")
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._stamp = clock()
        self.dropped = 0

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._stamp) * self._rate)
        self._stamp = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        self.dropped += 1
        return False


# --- resultaten in delen ----------------------------------------------------------------------


def chunk_result(
    ref: int,
    text: str,
    seq: SeqCounter,
    *,
    max_msg: int = MAX_MSG,
    max_parts: int = MAX_PARTS,
) -> list[ResultChunk]:
    """Splits een (lang) antwoord in RESULT-delen voor de FAP.

    De tekst wordt eerst schermklaar gemaakt (`to_display_text`) en afgekapt op wat in
    `max_parts` delen past. `max_msg` is de `max_rx` uit de HELLO van de FAP (hoogstens 512).
    """
    if not MIN_RX <= max_msg <= MAX_MSG:
        raise ValueError(f"max_msg buiten {MIN_RX}..{MAX_MSG}")
    if not 1 <= max_parts <= MAX_PARTS:
        raise ValueError(f"max_parts buiten 1..{MAX_PARTS}")
    per_part = min(MAX_CHUNK, max_msg - _RESULT_OVERHEAD)
    clean = to_display_text(text, max_len=per_part * max_parts, multiline=True)
    pieces = [clean[i : i + per_part] for i in range(0, len(clean), per_part)] or [""]
    total = len(pieces)
    return [
        ResultChunk(seq=seq.next(), ref=ref, part=i, total=total, text=piece)
        for i, piece in enumerate(pieces)
    ]


@dataclass
class _Partial:
    total: int
    started: float
    parts: dict[int, str] = field(default_factory=dict)
    size: int = 0


class Reassembler:
    """Zet RESULT-delen weer in elkaar, met harde grenzen op geheugen en tijd.

    De Pi stuurt delen op volgorde (stop-and-wait via de RPC-bevestiging), dus in de praktijk
    komen ze in volgorde binnen. Deze klasse is de referentie voor de FAP-logica en voor een
    nagebootste FAP in tests, en verdraagt daarom ook: andere volgorde, dubbele delen
    (identiek: genegeerd; anders: fout) en ontbrekende delen (verlopen na `timeout_s`).
    """

    def __init__(
        self,
        *,
        max_parts: int = MAX_PARTS,
        max_bytes: int = MAX_RESULT,
        max_open: int = 4,
        timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_parts = max_parts
        self._max_bytes = max_bytes
        self._max_open = max_open
        self._timeout = timeout_s
        self._clock = clock
        self._open: dict[int, _Partial] = {}
        self._done: deque[int] = deque(maxlen=32)

    @property
    def open_refs(self) -> list[int]:
        return list(self._open)

    def drop(self, ref: int) -> None:
        self._open.pop(ref, None)

    def expire(self) -> list[int]:
        """Gooi onvolledige antwoorden weg die te lang open staan; geeft hun refs."""
        now = self._clock()
        old = [ref for ref, p in self._open.items() if now - p.started > self._timeout]
        for ref in old:
            del self._open[ref]
        return old

    def feed(self, chunk: ResultChunk) -> str | None:
        """Voeg een deel toe. Geeft de volledige tekst als het antwoord compleet is.

        Raises:
            ProtocolError: grens overschreden of tegenstrijdige delen; dat antwoord is weg.
        """
        if chunk.ref in self._done:
            return None  # laat duplicaat van een al afgerond antwoord
        if chunk.total > self._max_parts:
            self.drop(chunk.ref)
            raise ProtocolError(f"antwoord van {chunk.total} delen > {self._max_parts}")
        partial = self._open.get(chunk.ref)
        if partial is None:
            self.expire()
            if len(self._open) >= self._max_open:
                raise ProtocolError(f"te veel open antwoorden (> {self._max_open})")
            partial = self._open[chunk.ref] = _Partial(total=chunk.total, started=self._clock())
        if chunk.total != partial.total:
            self.drop(chunk.ref)
            raise ProtocolError(f"ref {chunk.ref}: total wisselt ({partial.total}/{chunk.total})")
        known = partial.parts.get(chunk.part)
        if known is not None:
            if known == chunk.text:
                return None
            self.drop(chunk.ref)
            raise ProtocolError(f"ref {chunk.ref}: deel {chunk.part} twee keer, met andere tekst")
        if partial.size + len(chunk.text) > self._max_bytes:
            self.drop(chunk.ref)
            raise ProtocolError(f"ref {chunk.ref}: antwoord groter dan {self._max_bytes} bytes")
        partial.parts[chunk.part] = chunk.text
        partial.size += len(chunk.text)
        if len(partial.parts) < partial.total:
            return None
        del self._open[chunk.ref]
        self._done.append(chunk.ref)
        return "".join(partial.parts[i] for i in range(partial.total))


# --- bevestigings-handles ---------------------------------------------------------------------

T = TypeVar("T")


@dataclass
class _Issued(Generic[T]):
    token: T
    instance: int
    expires: float


class ConfirmHandles(Generic[T]):
    """Handles voor CONFIRM_PROMPT/CONFIRM_ANSWER.

    Een handle is een willekeurig u32 (nooit 0, nooit hergebruikt binnen dit object) die de
    Pi uitgeeft voor één open bevestiging (`token`, bijv. de `PendingConfirmation` van de
    agent-loop). Een antwoord telt alleen als de handle door deze Pi is uitgegeven, aan
    dezelfde FAP-instance, nog niet gebruikt is en de TTL (Pi-klok) niet verstreken is. De
    FAP ziet nooit de `pending_id` zelf.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_open: int = 8) -> None:
        self._clock = clock
        self._max_open = max_open
        self._open: dict[int, _Issued[T]] = {}
        self._issued: set[int] = set()

    def __len__(self) -> int:
        return len(self._open)

    def issue(self, token: T, *, instance: int, ttl_s: float) -> int:
        if ttl_s <= 0:
            raise ValueError("ttl_s moet > 0 zijn")
        self.expire()
        if len(self._open) >= self._max_open:
            raise ProtocolError("te veel open bevestigingen", code=ErrorCode.BUSY)
        while True:
            handle = secrets.randbelow(_U32_MAX) + 1
            if handle not in self._issued:
                break
        self._issued.add(handle)
        self._open[handle] = _Issued(token, instance, self._clock() + ttl_s)
        return handle

    def take(self, handle: int, *, instance: int) -> T | None:
        """Eenmalig ophalen. None bij onbekend, al gebruikt, verlopen of andere instance.

        Een antwoord van een andere instance laat de handle staan: een oude of vervalste
        FAP kan zo de bevestiging van de huidige niet "opmaken".
        """
        issued = self._open.get(handle)
        if issued is None or issued.instance != instance:
            return None
        del self._open[handle]
        if self._clock() >= issued.expires:
            return None
        return issued.token

    def revoke(self, handle: int) -> T | None:
        issued = self._open.pop(handle, None)
        return None if issued is None else issued.token

    def revoke_all(self) -> list[tuple[int, T]]:
        """Bij sessieverlies of een nieuwe HELLO: alle open handles ongeldig."""
        out = [(h, i.token) for h, i in self._open.items()]
        self._open.clear()
        return out

    def expire(self) -> list[tuple[int, T]]:
        """Verlopen handles weghalen; de bridge stuurt per stuk CONFIRM_CLOSED(EXPIRED)."""
        now = self._clock()
        gone = [(h, i.token) for h, i in self._open.items() if now >= i.expires]
        for h, _ in gone:
            del self._open[h]
        return gone


# --- vaste koppeling id -> tool-call ----------------------------------------------------------


@dataclass(frozen=True)
class QuickActionPlan:
    """Wat de bridge voor een snelle actie via de ToolRunner aanroept (frontend `flipper-ui`).

    `per_active_capture`: roep `tool` aan voor elke capture uit `status.captures`, met
    `{"capture_id": <id>}` (de ids komen van de Pi zelf, nooit van de FAP).
    """

    label: str
    tool: str
    args: Mapping[str, Any]
    per_active_capture: bool = False


def _plan(label: str, tool: str, per_active_capture: bool = False, **args: Any) -> QuickActionPlan:
    return QuickActionPlan(label, tool, MappingProxyType(dict(args)), per_active_capture)


QUICK_ACTIONS: Mapping[QuickAction, QuickActionPlan] = MappingProxyType(
    {
        QuickAction.STATUS: _plan("Status", "status"),
        QuickAction.WIFI_SCAN: _plan("Wifi-scan", "wifi_scan", method="iw", band="abg"),
        QuickAction.STOP_CAPTURES: _plan("Stop captures", "capture_stop", per_active_capture=True),
        QuickAction.EMERGENCY_STOP: _plan("Noodstop", "emergency_stop"),
    }
)


def quick_action_plan(action: Action) -> QuickActionPlan:
    """Plan voor een ACTION met `kind=QUICK`; onbekend id -> ProtocolError(UNKNOWN_ACTION)."""
    if action.kind is not ActionKind.QUICK:
        raise ValueError("geen snelle actie")
    try:
        return QUICK_ACTIONS[QuickAction(action.action_id)]
    except ValueError:
        raise ProtocolError(
            f"onbekende snelle actie {action.action_id}",
            code=ErrorCode.UNKNOWN_ACTION,
            seq=action.seq,
        ) from None
