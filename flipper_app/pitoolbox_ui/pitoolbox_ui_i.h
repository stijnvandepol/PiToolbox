/**
 * PiToolbox-bediening op de Flipper (fase 4). Intern: app-struct, gedeelde functies.
 *
 * Draadmodel (zie README.md, "Veiligheid rond sessieverlies"):
 * - App-thread: view_dispatcher-eventloop. Alle GUI, scenes en het verwerken van berichten.
 * - RPC-worker van de sessie: DataExchange/AppExit-callbacks.
 * - Timer-thread: SessionClose-callback (rpc_system_app_free -> rpc_session_thread_pending_callback).
 * De callbacks raken nooit GUI-objecten aan: ze bevestigen, kopiëren naar `inbox` (zonder
 * wachten) en zetten vlaggen. De app-thread leest `inbox` via een event-loop-abonnement.
 */
#pragma once

#include <furi.h>
#include <gui/gui.h>
#include <gui/view_dispatcher.h>
#include <gui/scene_manager.h>
#include <gui/modules/submenu.h>
#include <gui/modules/text_box.h>
#include <gui/modules/popup.h>
#include <notification/notification.h>
#include <rpc/rpc_app.h>

#include "ptb_proto.h"
#include "ptb_confirm_view.h"
#include "ptb_scenes.h"

#define PTB_APP_MAJOR 1
#define PTB_APP_MINOR 0

#define PTB_TX_GAP_MS      200 /* <= 5 berichten/s naar de Pi (fase4-spike.md) */
#define PTB_TICK_MS        250
#define PTB_HELLO_RETRY_MS 3000
#define PTB_HELLO_TRIES    3
#define PTB_INBOX_DEPTH    4

/* Handmatige start: de FAP laat dit bestand achter en sluit; de Pi start hem met "RPC". */
#define PTB_DATA_DIR       EXT_PATH("apps_data/pitoolbox_ui")
#define PTB_START_FILE     PTB_DATA_DIR "/start_request"
#define PTB_START_TMP      PTB_DATA_DIR "/start_request.tmp"

typedef enum {
    PtbViewSubmenu,
    PtbViewTextBox,
    PtbViewPopup,
    PtbViewConfirm,
} PtbView;

typedef enum {
    PtbEventWelcome = 1,
    PtbEventStatus,
    PtbEventPresets,
    PtbEventResult,
    PtbEventConfirmPrompt,
    PtbEventConfirmClosed,
    PtbEventConfirmAnswered, /* antwoord is al verstuurd (bij de knopdruk) */
    PtbEventWaitChanged,
    PtbEventMenuBase = 0x100, /* + index van een submenu-item */
} PtbEvent;

typedef enum {
    PtbWaitConnecting,
    PtbWaitNoAnswer,
    PtbWaitOffline,
    PtbWaitManualSent,
    PtbWaitManualNoLink,
    PtbWaitManualError,
} PtbWaitState;

typedef enum {
    PtbItemData,
    PtbItemSessionClosed,
    PtbItemAppExit,
} PtbItemKind;

typedef struct {
    uint8_t kind;
    uint16_t len;
    uint8_t data[PTB_MAX_MSG];
} PtbInboxItem;

typedef struct {
    Gui* gui;
    NotificationApp* notifications;
    ViewDispatcher* view_dispatcher;
    SceneManager* scene_manager;
    Submenu* submenu;
    TextBox* text_box;
    Popup* popup;
    PtbConfirmView* confirm_view;

    /* --- RPC: alleen onder rpc_mutex lezen of schrijven --- */
    FuriMutex* rpc_mutex;
    RpcAppSystem* rpc; /* NULL zodra de sessie weg is of de app ontkoppelt */
    bool rpc_gone; /* sessie gesloten (callback) of ontkoppeld (afsluiten) */
    uint32_t callbacks_active; /* atomair: callbacks die nu in onze code lopen */
    PtbInboxItem cb_item; /* buffer van de callbacks (die lopen nooit tegelijk) */
    FuriMessageQueue* inbox;

    /* --- vanaf hier alleen de app-thread --- */
    PtbInboxItem rx_item;
    PtbMsg msg; /* ~400 bytes: niet op de stack */
    bool rpc_mode; /* gestart door de Pi met "RPC <ptr>" */
    bool offline_handled;
    PtbWaitState wait_state;
    uint32_t wait_deadline; /* tick waarop het wachtscherm de app sluit; 0 = nooit */
    uint32_t instance;
    uint16_t tx_seq;
    uint32_t last_tx_tick;
    bool rx_seq_valid;
    uint16_t rx_seq_last;
    uint8_t hello_tries;
    uint32_t hello_tick;

    bool connected;
    uint8_t welcome_flags;
    bool status_valid;
    PtbStatus status;
    bool presets_valid;
    PtbPresets presets;

    /* lopende actie en het laatste antwoord */
    bool busy;
    uint16_t active_ref;
    uint8_t next_part;
    uint8_t total_parts;
    FuriString* result;
    FuriString* progress;

    /* open bevestiging (hoogstens één, de Pi zet de rest in de rij) */
    bool confirm_active;
    uint32_t confirm_handle;
    uint32_t confirm_deadline;
    char confirm_summary[PTB_MAX_SUMMARY + 1];
    uint32_t answered_handle;
    const char* last_confirm_text;

    /* TextBox bewaart alleen een pointer: om en om schrijven (zie ptb_text_begin) */
    FuriString* view_text[2];
    uint8_t view_text_index;
} PtbApp;

/* Versturen naar de Pi (app-thread). false als er geen sessie (meer) is. */
bool ptb_send_action(PtbApp* app, PtbActionKind kind, uint8_t action_id, const char* label);
bool ptb_send_status_request(PtbApp* app);
bool ptb_send_presets_request(PtbApp* app);
/* Antwoord op de vraag met `handle` (die van het scherm bij de knopdruk). false, en niets
 * verstuurd, als dat niet meer de open vraag is (intussen gesloten of vervangen). */
bool ptb_answer_confirm(PtbApp* app, uint32_t handle, bool approve);
bool ptb_send_cancel(PtbApp* app);

/* Tekst voor de TextBox: schrijf in ptb_text_begin(), toon met ptb_text_commit(). */
FuriString* ptb_text_begin(PtbApp* app);
void ptb_text_commit(PtbApp* app);

void ptb_submenu_callback(void* context, uint32_t index);
uint8_t ptb_confirm_remaining_s(const PtbApp* app);
