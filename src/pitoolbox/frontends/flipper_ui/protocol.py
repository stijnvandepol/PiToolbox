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

Dit module is de publieke ingang: de codec en kleine bouwstenen voor de bridge (volgnummers,
rate limiting, reassembly, bevestigings-handles). De koppeling aan de runner en de agent-loop
staat in `bridge.py`. De code staat per onderdeel in:

* `protocol_spec.py`: grenzen, berichttypes, codes en `ProtocolError`;
* `protocol_text.py`: tekstvelden (printbare ASCII), `to_display_text` en het afbreken op
  woordgrenzen voor de TextBox van de FAP (`to_text_box`);
* `protocol_wire.py` en `protocol_messages.py`: body-layout en de berichtmodellen;
* `protocol_codec.py`: kop, `encode` en de decoders per richting;
* `protocol_flow.py`: volgnummers en rate limiting;
* `protocol_chunks.py`: RESULT in delen en weer samenvoegen;
* `protocol_handles.py`: bevestigings-handles;
* `protocol_quick_actions.py`: vaste koppeling snelle actie -> tool-call.
"""

from __future__ import annotations

from pitoolbox.frontends.flipper_ui.protocol_chunks import Reassembler, chunk_result
from pitoolbox.frontends.flipper_ui.protocol_codec import decode_from_fap, decode_from_pi, encode
from pitoolbox.frontends.flipper_ui.protocol_flow import RateLimiter, SeqCounter, SeqTracker
from pitoolbox.frontends.flipper_ui.protocol_handles import ConfirmHandles
from pitoolbox.frontends.flipper_ui.protocol_messages import (
    U8,
    Action,
    Cancel,
    ConfirmAnswer,
    ConfirmClosed,
    ConfirmPrompt,
    Error,
    FapMessage,
    Handle,
    Hello,
    Message,
    PiMessage,
    Preset,
    Presets,
    PresetsRequest,
    Progress,
    Ref,
    ResultChunk,
    Seq,
    Status,
    StatusRequest,
    Welcome,
)
from pitoolbox.frontends.flipper_ui.protocol_quick_actions import (
    QUICK_ACTIONS,
    QuickActionPlan,
    quick_action_plan,
)
from pitoolbox.frontends.flipper_ui.protocol_spec import (
    AGE_UNKNOWN,
    HEADER_SIZE,
    MAGIC,
    MAX_CHUNK,
    MAX_ERROR_TEXT,
    MAX_LABEL,
    MAX_MSG,
    MAX_PARTS,
    MAX_PRESETS,
    MAX_PROGRESS,
    MAX_RESULT,
    MAX_SUMMARY,
    MAX_TOOL_NAME,
    MAX_TTL_S,
    MAX_UPLINK,
    MIN_RX,
    PROTO_VERSION,
    RATE_BURST,
    RATE_PER_S,
    SUPPORTED_VERSIONS,
    ActionKind,
    CloseReason,
    ErrorCode,
    LastResult,
    Link,
    MsgType,
    ProtocolError,
    QuickAction,
)
from pitoolbox.frontends.flipper_ui.protocol_text import (
    ChunkText,
    ErrorText,
    Label,
    ProgressText,
    Summary,
    ToolText,
    UplinkText,
    text_width,
    to_display_text,
    to_text_box,
    wrap_lines,
)

__all__ = [
    "AGE_UNKNOWN",
    "HEADER_SIZE",
    "MAGIC",
    "MAX_CHUNK",
    "MAX_ERROR_TEXT",
    "MAX_LABEL",
    "MAX_MSG",
    "MAX_PARTS",
    "MAX_PRESETS",
    "MAX_PROGRESS",
    "MAX_RESULT",
    "MAX_SUMMARY",
    "MAX_TOOL_NAME",
    "MAX_TTL_S",
    "MAX_UPLINK",
    "MIN_RX",
    "PROTO_VERSION",
    "QUICK_ACTIONS",
    "RATE_BURST",
    "RATE_PER_S",
    "SUPPORTED_VERSIONS",
    "U8",
    "Action",
    "ActionKind",
    "Cancel",
    "ChunkText",
    "CloseReason",
    "ConfirmAnswer",
    "ConfirmClosed",
    "ConfirmHandles",
    "ConfirmPrompt",
    "Error",
    "ErrorCode",
    "ErrorText",
    "FapMessage",
    "Handle",
    "Hello",
    "Label",
    "LastResult",
    "Link",
    "Message",
    "MsgType",
    "PiMessage",
    "Preset",
    "Presets",
    "PresetsRequest",
    "Progress",
    "ProgressText",
    "ProtocolError",
    "QuickAction",
    "QuickActionPlan",
    "RateLimiter",
    "Reassembler",
    "Ref",
    "ResultChunk",
    "Seq",
    "SeqCounter",
    "SeqTracker",
    "Status",
    "StatusRequest",
    "Summary",
    "ToolText",
    "UplinkText",
    "Welcome",
    "chunk_result",
    "decode_from_fap",
    "decode_from_pi",
    "encode",
    "quick_action_plan",
    "text_width",
    "to_display_text",
    "to_text_box",
    "wrap_lines",
]
