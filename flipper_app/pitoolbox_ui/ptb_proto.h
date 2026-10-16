/**
 * Protocol FAP <-> Pi, versie 1 (docs/fase4-protocol.md).
 *
 * Spiegel van src/pitoolbox/frontends/flipper_ui/protocol.py. Puur C zonder furi, zodat
 * het ook op de host te testen is tegen de vectoren van de Python-codec.
 *
 * De FAP decodeert alleen Pi->FAP-berichten en codeert alleen FAP->Pi-berichten. Hij stuurt
 * nooit tekst, toolnamen of argumenten: alleen ids, handles en refs.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PTB_MAGIC          0x50
#define PTB_PROTO_VERSION  1
#define PTB_MAX_MSG        512
#define PTB_HEADER_SIZE    5
#define PTB_MAX_FAP_MSG    16 /* grootste FAP->Pi-bericht (HELLO) is 14 bytes */
#define PTB_MAX_SUMMARY    200
#define PTB_MAX_LABEL      24
#define PTB_MAX_PRESETS    12
#define PTB_MAX_PROGRESS   64
#define PTB_MAX_ERROR_TEXT 120
#define PTB_MAX_UPLINK     32
#define PTB_MAX_TOOL_NAME  24
#define PTB_MAX_CHUNK      500
#define PTB_MAX_PARTS      8
#define PTB_MAX_RESULT     (PTB_MAX_CHUNK * PTB_MAX_PARTS)
#define PTB_MAX_TTL_S      120
#define PTB_AGE_UNKNOWN    0xFFFF

typedef enum {
    /* FAP -> Pi */
    PtbMsgHello = 0x01,
    PtbMsgStatusRequest = 0x02,
    PtbMsgPresetsRequest = 0x03,
    PtbMsgAction = 0x04,
    PtbMsgConfirmAnswer = 0x05,
    PtbMsgCancel = 0x06,
    /* Pi -> FAP */
    PtbMsgWelcome = 0x81,
    PtbMsgStatus = 0x82,
    PtbMsgPresets = 0x83,
    PtbMsgConfirmPrompt = 0x84,
    PtbMsgConfirmClosed = 0x85,
    PtbMsgProgress = 0x86,
    PtbMsgResult = 0x87,
    PtbMsgError = 0x88,
} PtbMsgType;

typedef enum {
    PtbActionQuick = 1,
    PtbActionPreset = 2,
} PtbActionKind;

typedef enum {
    PtbQuickStatus = 1,
    PtbQuickWifiScan = 2,
    PtbQuickStopCaptures = 3,
    PtbQuickEmergencyStop = 4,
} PtbQuickAction;

#define PTB_WELCOME_CONFIRM_ON_FLIPPER 0x01
#define PTB_WELCOME_AGENT_AVAILABLE    0x02

#define PTB_STATUS_UPLINK_UP             0x01
#define PTB_STATUS_UNDERVOLTAGE_NOW      0x02
#define PTB_STATUS_UNDERVOLTAGE_OCCURRED 0x04
#define PTB_STATUS_POWER_UNKNOWN         0x08

typedef enum {
    PtbLinkUnknown = 0,
    PtbLinkUsb = 1,
    PtbLinkBle = 2,
} PtbLink;

typedef enum {
    PtbLastNone = 0,
    PtbLastOk = 1,
    PtbLastFailed = 2,
} PtbLastResult;

typedef enum {
    PtbCloseExecuted = 1,
    PtbCloseRejected = 2,
    PtbCloseExpired = 3,
    PtbCloseRevoked = 4,
    PtbCloseFailed = 5,
} PtbCloseReason;

typedef struct {
    uint8_t proto;
    uint32_t instance;
    uint8_t flags;
} PtbWelcome;

typedef struct {
    uint8_t flags;
    uint8_t link;
    uint8_t captures;
    uint8_t pending;
    uint8_t last_result;
    uint16_t last_age_s;
    char uplink[PTB_MAX_UPLINK + 1];
    char last_tool[PTB_MAX_TOOL_NAME + 1];
} PtbStatus;

typedef struct {
    uint8_t count;
    uint8_t id[PTB_MAX_PRESETS];
    char label[PTB_MAX_PRESETS][PTB_MAX_LABEL + 1];
} PtbPresets;

typedef struct {
    uint32_t handle;
    uint8_t ttl_s;
    char summary[PTB_MAX_SUMMARY + 1];
} PtbConfirmPrompt;

typedef struct {
    uint32_t handle;
    uint8_t reason;
} PtbConfirmClosed;

typedef struct {
    uint16_t ref;
    char text[PTB_MAX_PROGRESS + 1];
} PtbProgress;

typedef struct {
    uint16_t ref;
    uint8_t part;
    uint8_t total;
    const char* text; /* wijst in de ontvangen buffer; niet nul-afgesloten */
    size_t len;
} PtbResultChunk;

typedef struct {
    uint16_t ref;
    uint8_t code;
    char text[PTB_MAX_ERROR_TEXT + 1];
} PtbError;

typedef struct {
    uint8_t type;
    uint16_t seq;
    union {
        PtbWelcome welcome;
        PtbStatus status;
        PtbPresets presets;
        PtbConfirmPrompt prompt;
        PtbConfirmClosed closed;
        PtbProgress progress;
        PtbResultChunk result;
        PtbError error;
    };
} PtbMsg;

/** Decodeer een Pi->FAP-bericht. false bij elke afwijking (strikt, zoals de Python-codec). */
bool ptb_decode_pi(const uint8_t* data, size_t len, PtbMsg* out);

/* Encoders: buf moet minstens PTB_MAX_FAP_MSG bytes zijn; geven de lengte terug. */
size_t ptb_encode_hello(
    uint8_t* buf,
    uint16_t seq,
    uint8_t app_major,
    uint8_t app_minor,
    uint32_t instance);
size_t ptb_encode_empty(uint8_t* buf, PtbMsgType type, uint16_t seq);
size_t ptb_encode_action(uint8_t* buf, uint16_t seq, PtbActionKind kind, uint8_t action_id);
size_t ptb_encode_confirm_answer(uint8_t* buf, uint16_t seq, uint32_t handle, bool approve);
size_t ptb_encode_cancel(uint8_t* buf, uint16_t seq, uint16_t ref);

/** Volgende seq: 1..65535, 0 wordt overgeslagen. */
uint16_t ptb_seq_next(uint16_t seq);

/** true als seq nieuwer is dan last (serieel rekenen, RFC 1982, modulo 65535). */
bool ptb_seq_newer(uint16_t seq, uint16_t last);
