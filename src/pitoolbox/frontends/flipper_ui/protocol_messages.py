"""De berichten van protocol v1 als strikte modellen, met hun body-layout
(docs/fase4-protocol.md §3). Kop en tabellen per richting staan in `protocol_codec.py`."""

from __future__ import annotations

import struct
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pitoolbox.frontends.flipper_ui.protocol_spec import (
    _U16_MAX,
    _U32_MAX,
    AGE_UNKNOWN,
    MAX_MSG,
    MAX_PARTS,
    MAX_PRESETS,
    MAX_TTL_S,
    MIN_RX,
    ActionKind,
    CloseReason,
    ErrorCode,
    LastResult,
    Link,
    MsgType,
    ProtocolError,
)
from pitoolbox.frontends.flipper_ui.protocol_text import (
    ChunkText,
    ErrorText,
    Label,
    ProgressText,
    Summary,
    ToolText,
    UplinkText,
)
from pitoolbox.frontends.flipper_ui.protocol_wire import _enum, _Reader, _str8

# Vlagbits (byte) in WELCOME en STATUS.
_WF_CONFIRM_ON_FLIPPER = 0x01
_WF_AGENT_AVAILABLE = 0x02
_WF_ALL = _WF_CONFIRM_ON_FLIPPER | _WF_AGENT_AVAILABLE
_SF_UPLINK_UP = 0x01
_SF_UNDERVOLTAGE_NOW = 0x02
_SF_UNDERVOLTAGE_OCCURRED = 0x04
_SF_POWER_UNKNOWN = 0x08
_SF_ALL = _SF_UPLINK_UP | _SF_UNDERVOLTAGE_NOW | _SF_UNDERVOLTAGE_OCCURRED | _SF_POWER_UNKNOWN

Seq = Annotated[int, Field(ge=1, le=_U16_MAX)]
Ref = Annotated[int, Field(ge=0, le=_U16_MAX)]  # 0 = geen antwoord op een FAP-bericht
Handle = Annotated[int, Field(ge=1, le=_U32_MAX)]
U8 = Annotated[int, Field(ge=0, le=0xFF)]


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
