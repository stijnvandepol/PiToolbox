#include "ptb_confirm_view.h"
#include "ptb_proto.h"

#include <furi.h>
#include <gui/elements.h>
#include <stdio.h>

#define TEXT_WIDTH    122 /* 128 min ruimte voor de scrollbalk */
#define LINE_HEIGHT   10
#define BODY_Y        22
#define VISIBLE_LINES 3

struct PtbConfirmView {
    View* view;
    PtbConfirmCallback callback;
    void* context;
};

typedef struct {
    char summary[PTB_MAX_SUMMARY + 1];
    uint8_t remaining;
    uint8_t scroll;
    uint8_t lines; /* gezet tijdens het tekenen */
    uint32_t armed_at; /* tick vanaf wanneer OK telt */
    uint32_t handle; /* van de vraag op het scherm; gaat mee met het antwoord */
} PtbConfirmModel;

/* Woordomloop op pixelbreedte; tekent regels [first, first+count). Geeft het totaal. */
static uint8_t wrap_text(Canvas* canvas, const char* text, uint8_t first, uint8_t count) {
    char line[48];
    uint8_t index = 0;
    const char* p = text;
    while(*p && index < 0xFF) {
        size_t len = 0;
        size_t last_space = 0;
        while(p[len] && p[len] != '\n' && len < sizeof(line) - 1) {
            line[len] = p[len];
            line[len + 1] = '\0';
            if(canvas_string_width(canvas, line) > TEXT_WIDTH) break;
            if(p[len] == ' ') last_space = len;
            len++;
        }
        size_t take = len;
        size_t skip = 0;
        if(p[len] == '\n') {
            skip = 1;
        } else if(p[len] && last_space > 0) {
            take = last_space; /* afbreken op de laatste spatie, die valt weg */
            skip = 1;
        }
        if(take == 0 && skip == 0) take = 1; /* één teken breder dan de regel: toch verder */
        line[take] = '\0';
        if(index >= first && index < first + count) {
            canvas_draw_str(canvas, 0, BODY_Y + (index - first) * LINE_HEIGHT, line);
        }
        index++;
        p += take + skip;
    }
    return index;
}

static void ptb_confirm_draw(Canvas* canvas, void* model) {
    PtbConfirmModel* m = model;
    char header[24];

    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);
    snprintf(header, sizeof(header), "Zenden? %us", (unsigned)m->remaining);
    canvas_draw_str(canvas, 0, 10, header);

    canvas_set_font(canvas, FontSecondary);
    m->lines = wrap_text(canvas, m->summary, m->scroll, VISIBLE_LINES);
    if(m->lines > VISIBLE_LINES) {
        elements_scrollbar(canvas, m->scroll, m->lines - VISIBLE_LINES + 1);
    }

    canvas_draw_str(canvas, 0, 63, "Back=nee");
    if((int32_t)(furi_get_tick() - m->armed_at) >= 0) {
        elements_button_center(canvas, "Ja");
    }
}

static bool ptb_confirm_input(InputEvent* event, void* context) {
    PtbConfirmView* confirm = context;
    if(event->type != InputTypeShort && event->type != InputTypeRepeat) return true;

    bool fire = false;
    bool approve = false;
    uint32_t handle = 0;
    with_view_model(
        confirm->view,
        PtbConfirmModel * m,
        {
            switch(event->key) {
            case InputKeyUp:
                if(m->scroll > 0) m->scroll--;
                break;
            case InputKeyDown:
                if(m->scroll + VISIBLE_LINES < m->lines) m->scroll++;
                break;
            case InputKeyOk:
                if(event->type == InputTypeShort &&
                   (int32_t)(furi_get_tick() - m->armed_at) >= 0) {
                    fire = true;
                    approve = true;
                }
                break;
            case InputKeyBack:
                if(event->type == InputTypeShort) fire = true;
                break;
            default:
                break;
            }
            handle = m->handle; /* nu vastleggen: de vraag kan daarna nog wisselen */
        },
        true);

    if(fire && confirm->callback) confirm->callback(confirm->context, handle, approve);
    return true; /* alles opeten: Back mag hier nooit "terug" betekenen zonder antwoord */
}

PtbConfirmView* ptb_confirm_view_alloc(void) {
    PtbConfirmView* confirm = malloc(sizeof(PtbConfirmView));
    confirm->callback = NULL;
    confirm->context = NULL;
    confirm->view = view_alloc();
    view_allocate_model(confirm->view, ViewModelTypeLocking, sizeof(PtbConfirmModel));
    view_set_context(confirm->view, confirm);
    view_set_draw_callback(confirm->view, ptb_confirm_draw);
    view_set_input_callback(confirm->view, ptb_confirm_input);
    return confirm;
}

void ptb_confirm_view_free(PtbConfirmView* confirm) {
    furi_check(confirm);
    view_free(confirm->view);
    free(confirm);
}

View* ptb_confirm_view_get_view(PtbConfirmView* confirm) {
    furi_check(confirm);
    return confirm->view;
}

void ptb_confirm_view_set_callback(
    PtbConfirmView* confirm,
    PtbConfirmCallback callback,
    void* context) {
    furi_check(confirm);
    confirm->callback = callback;
    confirm->context = context;
}

void ptb_confirm_view_set_prompt(
    PtbConfirmView* confirm,
    const char* summary,
    uint8_t remaining_s,
    uint32_t handle) {
    furi_check(confirm);
    with_view_model(
        confirm->view,
        PtbConfirmModel * m,
        {
            strlcpy(m->summary, summary, sizeof(m->summary));
            m->remaining = remaining_s;
            m->scroll = 0;
            m->lines = 0;
            m->armed_at = furi_get_tick() + furi_ms_to_ticks(PTB_CONFIRM_ARM_MS);
            m->handle = handle;
        },
        true);
}

void ptb_confirm_view_set_remaining(PtbConfirmView* confirm, uint8_t remaining_s) {
    furi_check(confirm);
    with_view_model(confirm->view, PtbConfirmModel * m, { m->remaining = remaining_s; }, true);
}
