"""Vaste waarden van protocol v1 (docs/fase4-protocol.md): grenzen, berichttypes, codes en
de fout die de decoder geeft. De C-kant (`flipper_app/pitoolbox_ui/ptb_proto.h`) gebruikt
dezelfde getallen; zie `protocol.py` voor het overzicht."""

from __future__ import annotations

import struct
from enum import IntEnum

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


class ProtocolError(ValueError):
    """Ongeldig bericht. `seq` is gezet als de kop nog leesbaar was (voor een ERROR-antwoord)."""

    def __init__(
        self, message: str, *, code: ErrorCode = ErrorCode.INVALID_MESSAGE, seq: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.seq = seq
