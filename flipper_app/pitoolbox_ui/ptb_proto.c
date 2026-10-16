#include "ptb_proto.h"

#include <string.h>

/* --- lezen ----------------------------------------------------------------------------- */

typedef struct {
    const uint8_t* p;
    size_t left;
    bool ok;
} PtbReader;

static uint8_t rd_u8(PtbReader* r) {
    if(!r->ok || r->left < 1) {
        r->ok = false;
        return 0;
    }
    r->left--;
    return *r->p++;
}

static uint16_t rd_u16(PtbReader* r) {
    uint16_t lo = rd_u8(r);
    uint16_t hi = rd_u8(r);
    return (uint16_t)(lo | (hi << 8));
}

static uint32_t rd_u32(PtbReader* r) {
    uint32_t v = rd_u16(r);
    return v | ((uint32_t)rd_u16(r) << 16);
}

static bool text_ok(const uint8_t* s, size_t len, bool multiline) {
    for(size_t i = 0; i < len; i++) {
        uint8_t c = s[i];
        if(c == '\n' && multiline) continue;
        if(c < 0x20 || c > 0x7E) return false;
    }
    return true;
}

/* u8-lengte + printbare ASCII, naar dst (nul-afgesloten). */
static void rd_str8(PtbReader* r, char* dst, size_t min_len, size_t max_len, bool multiline) {
    size_t len = rd_u8(r);
    if(!r->ok || len < min_len || len > max_len || len > r->left ||
       !text_ok(r->p, len, multiline)) {
        r->ok = false;
        dst[0] = '\0';
        return;
    }
    memcpy(dst, r->p, len);
    dst[len] = '\0';
    r->p += len;
    r->left -= len;
}

bool ptb_decode_pi(const uint8_t* data, size_t len, PtbMsg* out) {
    if(!data || !out || len < PTB_HEADER_SIZE || len > PTB_MAX_MSG) return false;
    if(data[0] != PTB_MAGIC || data[1] != PTB_PROTO_VERSION) return false;

    PtbReader r = {.p = data + 2, .left = len - 2, .ok = true};
    memset(out, 0, sizeof(*out));
    out->type = rd_u8(&r);
    out->seq = rd_u16(&r);
    if(out->seq == 0) return false;

    switch(out->type) {
    case PtbMsgWelcome:
        out->welcome.proto = rd_u8(&r);
        out->welcome.instance = rd_u32(&r);
        out->welcome.flags = rd_u8(&r);
        if(out->welcome.proto == 0 || out->welcome.instance == 0) return false;
        if(out->welcome.flags &
           ~(PTB_WELCOME_CONFIRM_ON_FLIPPER | PTB_WELCOME_AGENT_AVAILABLE))
            return false;
        break;
    case PtbMsgStatus: {
        PtbStatus* s = &out->status;
        s->flags = rd_u8(&r);
        s->link = rd_u8(&r);
        s->captures = rd_u8(&r);
        s->pending = rd_u8(&r);
        s->last_result = rd_u8(&r);
        s->last_age_s = rd_u16(&r);
        rd_str8(&r, s->uplink, 0, PTB_MAX_UPLINK, false);
        rd_str8(&r, s->last_tool, 0, PTB_MAX_TOOL_NAME, false);
        if(s->flags & ~0x0F) return false;
        if(s->link > PtbLinkBle || s->last_result > PtbLastFailed) return false;
        break;
    }
    case PtbMsgPresets: {
        PtbPresets* ps = &out->presets;
        ps->count = rd_u8(&r);
        if(ps->count > PTB_MAX_PRESETS) return false;
        for(uint8_t i = 0; i < ps->count && r.ok; i++) {
            ps->id[i] = rd_u8(&r);
            rd_str8(&r, ps->label[i], 1, PTB_MAX_LABEL, false);
            if(ps->id[i] == 0) return false;
            for(uint8_t j = 0; j < i; j++) {
                if(ps->id[j] == ps->id[i]) return false;
            }
        }
        break;
    }
    case PtbMsgConfirmPrompt:
        out->prompt.handle = rd_u32(&r);
        out->prompt.ttl_s = rd_u8(&r);
        rd_str8(&r, out->prompt.summary, 1, PTB_MAX_SUMMARY, true);
        if(out->prompt.handle == 0) return false;
        if(out->prompt.ttl_s == 0 || out->prompt.ttl_s > PTB_MAX_TTL_S) return false;
        break;
    case PtbMsgConfirmClosed:
        out->closed.handle = rd_u32(&r);
        out->closed.reason = rd_u8(&r);
        if(out->closed.handle == 0) return false;
        if(out->closed.reason < PtbCloseExecuted || out->closed.reason > PtbCloseFailed)
            return false;
        break;
    case PtbMsgProgress:
        out->progress.ref = rd_u16(&r);
        rd_str8(&r, out->progress.text, 0, PTB_MAX_PROGRESS, false);
        break;
    case PtbMsgResult:
        out->result.ref = rd_u16(&r);
        out->result.part = rd_u8(&r);
        out->result.total = rd_u8(&r);
        if(!r.ok) return false;
        if(out->result.total == 0 || out->result.total > PTB_MAX_PARTS) return false;
        if(out->result.part >= out->result.total) return false;
        if(r.left > PTB_MAX_CHUNK || !text_ok(r.p, r.left, true)) return false;
        out->result.text = (const char*)r.p;
        out->result.len = r.left;
        r.p += r.left;
        r.left = 0;
        break;
    case PtbMsgError:
        out->error.ref = rd_u16(&r);
        out->error.code = rd_u8(&r);
        rd_str8(&r, out->error.text, 0, PTB_MAX_ERROR_TEXT, false);
        if(out->error.code == 0 || out->error.code > 11) return false;
        break;
    default:
        return false; /* onbekend, of een FAP->Pi-type */
    }
    return r.ok && r.left == 0;
}

/* --- schrijven ------------------------------------------------------------------------- */

static size_t wr_header(uint8_t* buf, PtbMsgType type, uint16_t seq) {
    buf[0] = PTB_MAGIC;
    buf[1] = PTB_PROTO_VERSION;
    buf[2] = (uint8_t)type;
    buf[3] = (uint8_t)(seq & 0xFF);
    buf[4] = (uint8_t)(seq >> 8);
    return PTB_HEADER_SIZE;
}

static size_t wr_u32(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
    return 4;
}

size_t ptb_encode_hello(
    uint8_t* buf,
    uint16_t seq,
    uint8_t app_major,
    uint8_t app_minor,
    uint32_t instance) {
    size_t n = wr_header(buf, PtbMsgHello, seq);
    buf[n++] = app_major;
    buf[n++] = app_minor;
    buf[n++] = PTB_PROTO_VERSION; /* proto_max */
    n += wr_u32(buf + n, instance);
    buf[n++] = (uint8_t)(PTB_MAX_MSG & 0xFF); /* max_rx */
    buf[n++] = (uint8_t)(PTB_MAX_MSG >> 8);
    return n;
}

size_t ptb_encode_empty(uint8_t* buf, PtbMsgType type, uint16_t seq) {
    return wr_header(buf, type, seq);
}

size_t ptb_encode_action(uint8_t* buf, uint16_t seq, PtbActionKind kind, uint8_t action_id) {
    size_t n = wr_header(buf, PtbMsgAction, seq);
    buf[n++] = (uint8_t)kind;
    buf[n++] = action_id;
    return n;
}

size_t ptb_encode_confirm_answer(uint8_t* buf, uint16_t seq, uint32_t handle, bool approve) {
    size_t n = wr_header(buf, PtbMsgConfirmAnswer, seq);
    n += wr_u32(buf + n, handle);
    buf[n++] = approve ? 1 : 0;
    return n;
}

size_t ptb_encode_cancel(uint8_t* buf, uint16_t seq, uint16_t ref) {
    size_t n = wr_header(buf, PtbMsgCancel, seq);
    buf[n++] = (uint8_t)(ref & 0xFF);
    buf[n++] = (uint8_t)(ref >> 8);
    return n;
}

/* --- volgnummers ----------------------------------------------------------------------- */

uint16_t ptb_seq_next(uint16_t seq) {
    return seq == 0xFFFF ? 1 : (uint16_t)(seq + 1);
}

bool ptb_seq_newer(uint16_t seq, uint16_t last) {
    if(seq == 0) return false;
    if(last == 0) return true;
    uint32_t distance = ((uint32_t)seq + 0xFFFF - last) % 0xFFFF;
    return distance != 0 && distance < 0xFFFF / 2;
}
