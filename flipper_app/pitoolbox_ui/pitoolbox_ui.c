/**
 * PiToolbox-bediening: FAP-kant van docs/fase4-protocol.md.
 *
 * RPC-afhandeling volgt de eisen uit docs/fase4-spike.md (BusFault na sessieverlies):
 * - SessionClose: in de callback meteen rpc_system_app_set_callback(rpc, NULL, NULL), vlag
 *   zetten, pointer op NULL. Daarna nooit meer een rpc_system_app_*-aanroep.
 * - Elke rpc_system_app_*-aanroep loopt onder rpc_mutex, na controle van de pointer.
 * - Elke DataExchange/AppExit/andere app-opdracht wordt precies één keer bevestigd, in de
 *   callback zelf, in elke toestand van de app.
 * - Geen notificaties per bericht (alleen bij een bevestigingsvraag).
 * Bron voor het gedrag van de firmware: Momentum mntm-012, applications/services/rpc/
 * rpc_app.c (rpc_system_app_free wacht tot de callback NULL is en geeft dan vrij) en rpc.c
 * (rpc_session_thread_pending_callback draait in de timer-thread).
 */
#include "pitoolbox_ui_i.h"

#include <furi_hal_random.h>
#include <furi_hal_rtc.h>
#include <notification/notification_messages.h>
#include <rpc/rpc.h>
#include <storage/storage.h>

#define TAG "PiToolboxUi"

/* --- RPC: callback en versturen ------------------------------------------------------- */

static void ptb_rpc_callback(const RpcAppSystemEvent* event, void* context) {
    PtbApp* app = context;
    __atomic_add_fetch(&app->callbacks_active, 1, __ATOMIC_SEQ_CST);
    furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);

    RpcAppSystem* rpc = app->rpc; /* NULL: we zijn al ontkoppeld, niets meer aanroepen */
    PtbInboxItem* item = &app->cb_item;
    bool post = false;

    switch(event->type) {
    case RpcAppEventTypeSessionClose:
        /* Geen confirm: SessionClose is geen opdracht (dubbel/ongevraagd confirm = crash). */
        if(rpc) rpc_system_app_set_callback(rpc, NULL, NULL);
        app->rpc = NULL;
        app->rpc_gone = true;
        item->kind = PtbItemSessionClosed;
        item->len = 0;
        post = true;
        break;
    case RpcAppEventTypeAppExit:
        if(rpc) {
            rpc_system_app_confirm(rpc, true);
            item->kind = PtbItemAppExit;
            item->len = 0;
            post = true;
        }
        break;
    case RpcAppEventTypeDataExchange:
        if(rpc) {
            /* Eerst kopiëren en in de rij zetten (nooit wachten), dan één keer bevestigen.
             * ERROR_APP_CMD_ERROR betekent voor de Pi: geweigerd of rij vol, later opnieuw. */
            bool ok = false;
            size_t size = event->data.bytes.size;
            const uint8_t* bytes = event->data.bytes.ptr;
            if(event->data.type == RpcAppSystemEventDataTypeBytes && bytes &&
               size >= PTB_HEADER_SIZE && size <= PTB_MAX_MSG && bytes[0] == PTB_MAGIC) {
                item->kind = PtbItemData;
                item->len = (uint16_t)size;
                memcpy(item->data, bytes, size);
                ok = furi_message_queue_put(app->inbox, item, 0) == FuriStatusOk;
            }
            rpc_system_app_confirm(rpc, ok);
        }
        break;
    default:
        /* LoadFile, knoppen: ondersteunen we niet, maar wel precies één keer antwoorden. */
        if(rpc) rpc_system_app_confirm(rpc, false);
        break;
    }

    /* Vlag rpc_gone is de bron van waarheid; dit bericht is alleen om sneller te reageren. */
    if(post) furi_message_queue_put(app->inbox, item, 0);

    furi_mutex_release(app->rpc_mutex);
    __atomic_sub_fetch(&app->callbacks_active, 1, __ATOMIC_SEQ_CST);
}

static bool ptb_send(PtbApp* app, const uint8_t* data, size_t len) {
    /* Hoogstens ~5 berichten/s: boven ~10 frames/s loopt BLE achter (spike, test 4). */
    if(app->last_tx_tick) {
        int32_t wait = (int32_t)(app->last_tx_tick + PTB_TX_GAP_MS - furi_get_tick());
        if(wait > 0) furi_delay_ms(wait);
    }

    bool sent = false;
    furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
    if(app->rpc && !app->rpc_gone) {
        /* De mutex blijft vast tijdens het versturen: zo kan de SessionClose-callback (en
         * dus rpc_system_app_free) niet tussendoor de context vrijgeven. Versturen blokkeert
         * niet eeuwig: bt.c zet DISCONNECTED vóór rpc_session_close, en pipe_send (USB)
         * stopt bij een gebroken pipe. */
        rpc_system_app_exchange_data(app->rpc, data, len);
        sent = true;
    }
    furi_mutex_release(app->rpc_mutex);

    app->last_tx_tick = furi_get_tick();
    if(!app->last_tx_tick) app->last_tx_tick = 1;
    return sent;
}

static uint16_t ptb_next_seq(PtbApp* app) {
    app->tx_seq = ptb_seq_next(app->tx_seq);
    return app->tx_seq;
}

static void ptb_send_hello(PtbApp* app) {
    uint8_t buf[PTB_MAX_FAP_MSG];
    size_t n =
        ptb_encode_hello(buf, ptb_next_seq(app), PTB_APP_MAJOR, PTB_APP_MINOR, app->instance);
    ptb_send(app, buf, n);
    app->hello_tries++;
    app->hello_tick = furi_get_tick();
}

bool ptb_send_status_request(PtbApp* app) {
    uint8_t buf[PTB_MAX_FAP_MSG];
    return ptb_send(app, buf, ptb_encode_empty(buf, PtbMsgStatusRequest, ptb_next_seq(app)));
}

bool ptb_send_presets_request(PtbApp* app) {
    uint8_t buf[PTB_MAX_FAP_MSG];
    return ptb_send(app, buf, ptb_encode_empty(buf, PtbMsgPresetsRequest, ptb_next_seq(app)));
}

bool ptb_send_action(PtbApp* app, PtbActionKind kind, uint8_t action_id, const char* label) {
    uint8_t buf[PTB_MAX_FAP_MSG];
    uint16_t seq = ptb_next_seq(app);
    bool sent = ptb_send(app, buf, ptb_encode_action(buf, seq, kind, action_id));
    if(sent) {
        app->busy = true;
        app->active_ref = seq;
        app->next_part = 0;
        app->total_parts = 0;
        furi_string_reset(app->result);
        furi_string_printf(app->progress, "%s", label);
    } else {
        app->busy = false;
        furi_string_set_str(app->result, "Geen verbinding met de Pi.");
    }
    return sent;
}

bool ptb_answer_confirm(PtbApp* app, uint32_t handle, bool approve) {
    /* Alleen voor de vraag die nu open staat. Een knopdruk op een vraag die intussen gesloten
     * of vervangen is, telt niet: OK mag nooit naar een vraag gaan die de mens niet zag. */
    if(!app->confirm_active || handle == 0 || handle != app->confirm_handle) {
        FURI_LOG_W(TAG, "antwoord op een vraag die niet meer open staat genegeerd");
        return false;
    }
    uint8_t buf[PTB_MAX_FAP_MSG];
    size_t n = ptb_encode_confirm_answer(buf, ptb_next_seq(app), handle, approve);
    app->answered_handle = handle;
    app->confirm_active = false;
    app->last_confirm_text = approve ? "ja gestuurd" : "nee gestuurd";
    ptb_send(app, buf, n);
    return true;
}

bool ptb_send_cancel(PtbApp* app) {
    if(!app->busy) return false;
    uint8_t buf[PTB_MAX_FAP_MSG];
    size_t n = ptb_encode_cancel(buf, ptb_next_seq(app), app->active_ref);
    app->busy = false;
    furi_string_set_str(app->result, "Afgebroken.");
    return ptb_send(app, buf, n);
}

/* Ontkoppelen bij afsluiten. Na deze functie roept de firmware ons niet meer aan. */
static void ptb_rpc_detach(PtbApp* app) {
    furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
    if(app->rpc) {
        /* Volgorde is essentieel: EERST send_exited, DAN de callback op NULL.
         * rpc_system_app_free (rpc_app.c) roept onze callback alleen aan als die niet NULL is
         * en geeft daarna vrij zodra hij NULL is. Zetten we hem eerst op NULL, dan kan een
         * gelijktijdige SessionClose de context vrijgeven terwijl wij nog in send_exited zitten
         * (use-after-free, zelfde soort fout als in rpc_debug_app). Zolang de callback nog
         * staat, blokkeert de free-thread in onze callback op rpc_mutex, die wij vasthouden. */
        rpc_system_app_send_exited(app->rpc);
        rpc_system_app_set_callback(app->rpc, NULL, NULL);
        app->rpc = NULL;
    }
    app->rpc_gone = true;
    furi_mutex_release(app->rpc_mutex);
    /* Een callback die al liep (en op de mutex wachtte) ziet nu rpc == NULL en stopt. Pas
     * daarna mogen mutex, inbox en app vrijgegeven worden. */
    while(__atomic_load_n(&app->callbacks_active, __ATOMIC_SEQ_CST)) {
        furi_delay_tick(1);
    }
}

/* --- tekst voor de TextBox ------------------------------------------------------------ */

FuriString* ptb_text_begin(PtbApp* app) {
    FuriString* next = app->view_text[app->view_text_index ^ 1];
    furi_string_reset(next);
    return next;
}

void ptb_text_commit(PtbApp* app) {
    /* text_box_set_text neemt de model-lock: daarna leest de GUI de vorige buffer niet meer,
     * dus die mag bij de volgende update overschreven worden. */
    app->view_text_index ^= 1;
    text_box_set_text(app->text_box, furi_string_get_cstr(app->view_text[app->view_text_index]));
}

/* --- berichten van de Pi (app-thread) ------------------------------------------------- */

uint8_t ptb_confirm_remaining_s(const PtbApp* app) {
    int32_t left = (int32_t)(app->confirm_deadline - furi_get_tick());
    if(left <= 0) return 0;
    return (uint8_t)((left + 999) / 1000);
}

static const char* ptb_close_text(uint8_t reason) {
    switch(reason) {
    case PtbCloseExecuted:
        return "uitgevoerd";
    case PtbCloseRejected:
        return "geweigerd";
    case PtbCloseExpired:
        return "verlopen";
    case PtbCloseRevoked:
        return "ingetrokken";
    default:
        return "mislukt";
    }
}

static void ptb_on_result(PtbApp* app, const PtbResultChunk* chunk) {
    if(!app->busy || chunk->ref != app->active_ref) return; /* oud of van iemand anders */
    if(chunk->part == 0) {
        furi_string_reset(app->result);
        app->next_part = 0;
        app->total_parts = chunk->total;
    }
    /* De Pi stuurt de delen op volgorde (stop-and-wait); alles anders = onvolledig. */
    if(chunk->part != app->next_part || chunk->total != app->total_parts) {
        app->busy = false;
        furi_string_set_str(app->result, "Antwoord onvolledig ontvangen.");
        return;
    }
    for(size_t i = 0; i < chunk->len; i++) {
        furi_string_push_back(app->result, chunk->text[i]);
    }
    app->next_part++;
    if(app->next_part == app->total_parts) app->busy = false;
}

/* ERROR NOT_READY: de Pi kent deze sessie niet (meer), bijv. nadat hij een bericht niet kon
 * afleveren en de sessie losliet. Dan opnieuw groeten met dezelfde instance (protocol §5). De
 * open vraag vervalt: zijn handle geldt op de Pi niet meer (de Pi vraagt hem opnieuw). */
static void ptb_on_not_ready(PtbApp* app) {
    if(!app->rpc_mode || app->wait_state == PtbWaitOffline) return;
    if(!app->connected && app->wait_state == PtbWaitConnecting) return; /* HELLO loopt al */
    if(app->busy) furi_string_set_str(app->result, "Onderbroken: opnieuw verbonden met de Pi.");
    app->connected = false;
    app->busy = false;
    app->confirm_active = false;
    app->wait_state = PtbWaitConnecting;
    app->hello_tries = 0;
    /* Hoogstens één HELLO per PTB_HELLO_RETRY_MS; anders verstuurt de tick hem. */
    if(furi_get_tick() - app->hello_tick >= PTB_HELLO_RETRY_MS) ptb_send_hello(app);
    if(!scene_manager_search_and_switch_to_previous_scene(app->scene_manager, PtbSceneWait)) {
        scene_manager_handle_custom_event(app->scene_manager, PtbEventWaitChanged);
    }
}


/* Kort woord bij een ERROR-code (protocol.py ErrorCode), i.p.v. een nummer op het scherm.
 * NULL: onbekende code, dan "Fout <n>". */
static const char* ptb_error_title(uint8_t code) {
    static const char* const titles[] = {
        [1] = "Bezig",
        [2] = "Te snel",
        [3] = "Onbekende actie",
        [4] = "Ongeldig bericht",
        [5] = "Andere versie",
        [6] = "Mislukt",
        [7] = "Agent niet beschikbaar",
        [8] = "Afgebroken",
        [9] = "Time-out",
        [10] = "Bevestiging ongeldig",
        [11] = "Pi kent sessie niet",
    };
    return code < COUNT_OF(titles) ? titles[code] : NULL;
}

static void ptb_on_message(PtbApp* app, const PtbMsg* msg) {
    SceneManager* sm = app->scene_manager;
    if(msg->type == PtbMsgError && msg->error.code == PtbErrorNotReady) {
        ptb_on_not_ready(app);
        return;
    }
    switch(msg->type) {
    case PtbMsgWelcome:
        if(msg->welcome.instance != app->instance || msg->welcome.proto != PTB_PROTO_VERSION)
            return;
        app->welcome_flags = msg->welcome.flags;
        if(!app->connected) {
            app->connected = true;
            scene_manager_handle_custom_event(sm, PtbEventWelcome);
        }
        return;
    default:
        break;
    }
    if(!app->connected) return; /* alles behalve WELCOME pas na de handdruk */

    switch(msg->type) {
    case PtbMsgStatus:
        app->status = msg->status;
        app->status_valid = true;
        scene_manager_handle_custom_event(sm, PtbEventStatus);
        break;
    case PtbMsgPresets:
        app->presets = msg->presets;
        app->presets_valid = true;
        scene_manager_handle_custom_event(sm, PtbEventPresets);
        break;
    case PtbMsgConfirmPrompt:
        app->confirm_active = true;
        app->confirm_handle = msg->prompt.handle;
        app->confirm_deadline = furi_get_tick() + msg->prompt.ttl_s * 1000U;
        strlcpy(app->confirm_summary, msg->prompt.summary, sizeof(app->confirm_summary));
        /* Eén keer licht + tril: een mens moet dit zien. Niet per bericht (spike, test 3). */
        notification_message(app->notifications, &sequence_display_backlight_on);
        notification_message(app->notifications, &sequence_single_vibro);
        if(scene_manager_get_current_scene(sm) == PtbSceneConfirm) {
            scene_manager_handle_custom_event(sm, PtbEventConfirmPrompt);
        } else {
            scene_manager_next_scene(sm, PtbSceneConfirm);
        }
        break;
    case PtbMsgConfirmClosed:
        if(msg->closed.handle == app->confirm_handle ||
           msg->closed.handle == app->answered_handle) {
            app->last_confirm_text = ptb_close_text(msg->closed.reason);
        }
        if(app->confirm_active && msg->closed.handle == app->confirm_handle) {
            app->confirm_active = false;
            scene_manager_handle_custom_event(sm, PtbEventConfirmClosed);
        }
        break;
    case PtbMsgProgress:
        if(app->busy && msg->progress.ref == app->active_ref) {
            furi_string_set_str(app->progress, msg->progress.text);
            scene_manager_handle_custom_event(sm, PtbEventResult);
        }
        break;
    case PtbMsgResult:
        ptb_on_result(app, &msg->result);
        scene_manager_handle_custom_event(sm, PtbEventResult);
        break;
    case PtbMsgError:
        if(msg->error.ref == 0 || (app->busy && msg->error.ref == app->active_ref)) {
            if(msg->error.ref) app->busy = false;
            const char* title = ptb_error_title(msg->error.code);
            if(title) {
                furi_string_printf(app->result, "%s: %s", title, msg->error.text);
            } else {
                furi_string_printf(app->result, "Fout %u: %s", msg->error.code, msg->error.text);
            }
            scene_manager_handle_custom_event(sm, PtbEventResult);
        }
        break;
    default:
        break;
    }
}

static void ptb_on_session_closed(PtbApp* app) {
    if(app->offline_handled) return;
    app->offline_handled = true;
    app->connected = false;
    app->busy = false;
    app->confirm_active = false;
    app->wait_state = PtbWaitOffline;
    /* De sessie komt niet terug naar deze FAP (spike §3): kort melden en dan sluiten. */
    if(!scene_manager_search_and_switch_to_previous_scene(app->scene_manager, PtbSceneWait)) {
        scene_manager_handle_custom_event(app->scene_manager, PtbEventWaitChanged);
    }
}

static void ptb_inbox_callback(FuriEventLoopObject* object, void* context) {
    UNUSED(object);
    PtbApp* app = context;
    while(furi_message_queue_get(app->inbox, &app->rx_item, 0) == FuriStatusOk) {
        switch(app->rx_item.kind) {
        case PtbItemSessionClosed:
            ptb_on_session_closed(app);
            break;
        case PtbItemAppExit:
            scene_manager_stop(app->scene_manager);
            view_dispatcher_stop(app->view_dispatcher);
            break;
        case PtbItemData:
            if(!ptb_decode_pi(app->rx_item.data, app->rx_item.len, &app->msg)) {
                FURI_LOG_W(TAG, "ongeldig bericht genegeerd (%u bytes)", app->rx_item.len);
                break;
            }
            if(app->rx_seq_valid && !ptb_seq_newer(app->msg.seq, app->rx_seq_last)) {
                break; /* duplicaat of oud */
            }
            app->rx_seq_valid = true;
            app->rx_seq_last = app->msg.seq;
            ptb_on_message(app, &app->msg);
            break;
        default:
            break;
        }
    }
}

static void ptb_tick_callback(void* context) {
    PtbApp* app = context;
    bool gone;
    furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
    gone = app->rpc_gone;
    furi_mutex_release(app->rpc_mutex);

    if(app->rpc_mode && gone) {
        ptb_on_session_closed(app); /* vangnet als de inbox vol zat */
    } else if(app->rpc_mode && !app->connected && app->wait_state == PtbWaitConnecting) {
        if(furi_get_tick() - app->hello_tick >= PTB_HELLO_RETRY_MS) {
            if(app->hello_tries < PTB_HELLO_TRIES) {
                ptb_send_hello(app);
            } else {
                app->wait_state = PtbWaitNoAnswer;
                scene_manager_handle_custom_event(app->scene_manager, PtbEventWaitChanged);
            }
        }
    }
    scene_manager_handle_tick_event(app->scene_manager);
}

/* --- handmatige start ------------------------------------------------------------------- */

/* Alleen exact "RPC %08lX" (rpc_app.c) telt als RPC-start; al het andere is handmatig. */
static RpcAppSystem* ptb_parse_rpc_args(const char* args) {
    if(!args || strlen(args) != 12 || strncmp(args, "RPC ", 4) != 0) return NULL;
    uint32_t value = 0;
    for(size_t i = 4; i < 12; i++) {
        char c = args[i];
        uint32_t digit;
        if(c >= '0' && c <= '9') {
            digit = (uint32_t)(c - '0');
        } else if(c >= 'A' && c <= 'F') {
            digit = (uint32_t)(c - 'A' + 10);
        } else if(c >= 'a' && c <= 'f') {
            digit = (uint32_t)(c - 'a' + 10);
        } else {
            return NULL;
        }
        value = (value << 4) | digit;
    }
    return value ? (RpcAppSystem*)value : NULL;
}

/* Laat een verzoekbestand achter voor de Pi (die pollt het met storage_stat). Atomair:
 * eerst .tmp schrijven, dan hernoemen, zodat de Pi nooit een half bestand leest. */
static bool ptb_write_start_request(void) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    char line[40];
    int len = snprintf(
        line, sizeof(line), "PTBUI 1 %lu\n", (unsigned long)furi_hal_rtc_get_timestamp());
    bool ok = false;

    storage_simply_mkdir(storage, PTB_DATA_DIR);
    File* file = storage_file_alloc(storage);
    if(storage_file_open(file, PTB_START_TMP, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        ok = storage_file_write(file, line, (size_t)len) == (size_t)len;
    }
    storage_file_close(file);
    storage_file_free(file);
    if(ok) {
        storage_common_remove(storage, PTB_START_FILE);
        ok = storage_common_rename(storage, PTB_START_TMP, PTB_START_FILE) == FSE_OK;
    }
    furi_record_close(RECORD_STORAGE);
    return ok;
}

static PtbWaitState ptb_manual_start(void) {
    Rpc* rpc = furi_record_open(RECORD_RPC);
    size_t sessions = rpc_get_sessions_count(rpc);
    furi_record_close(RECORD_RPC);
    if(sessions == 0) return PtbWaitManualNoLink; /* geen Pi (of andere host) verbonden */
    return ptb_write_start_request() ? PtbWaitManualSent : PtbWaitManualError;
}

/* --- app ---------------------------------------------------------------------------------- */

void ptb_submenu_callback(void* context, uint32_t index) {
    PtbApp* app = context;
    view_dispatcher_send_custom_event(app->view_dispatcher, PtbEventMenuBase + index);
}

static void ptb_confirm_callback(void* context, uint32_t handle, bool approve) {
    PtbApp* app = context;
    /* Meteen versturen, bij de knopdruk, met de handle van het scherm. Niet pas bij een
     * custom event: tussen knopdruk en custom event kan de inbox een nieuwe CONFIRM_PROMPT
     * verwerken, en dan zou het antwoord bij die (ongeziene) vraag horen. Input, custom events
     * en de inbox lopen alle drie in de FuriEventLoop van de view_dispatcher (app-thread,
     * view_dispatcher.h), dus hier is geen lock nodig. */
    if(ptb_answer_confirm(app, handle, approve)) {
        view_dispatcher_send_custom_event(app->view_dispatcher, PtbEventConfirmAnswered);
    }
}

static bool ptb_custom_event_callback(void* context, uint32_t event) {
    PtbApp* app = context;
    return scene_manager_handle_custom_event(app->scene_manager, event);
}

static bool ptb_back_event_callback(void* context) {
    PtbApp* app = context;
    return scene_manager_handle_back_event(app->scene_manager);
}

static PtbApp* ptb_app_alloc(void) {
    PtbApp* app = malloc(sizeof(PtbApp));
    memset(app, 0, sizeof(PtbApp));

    app->rpc_mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->inbox = furi_message_queue_alloc(PTB_INBOX_DEPTH, sizeof(PtbInboxItem));
    app->result = furi_string_alloc();
    app->progress = furi_string_alloc();
    app->view_text[0] = furi_string_alloc();
    app->view_text[1] = furi_string_alloc();
    app->last_confirm_text = "-";
    do {
        app->instance = furi_hal_random_get();
    } while(app->instance == 0);

    app->gui = furi_record_open(RECORD_GUI);
    app->notifications = furi_record_open(RECORD_NOTIFICATION);

    app->view_dispatcher = view_dispatcher_alloc();
    app->scene_manager = scene_manager_alloc(&ptb_scene_handlers, app);
    view_dispatcher_set_event_callback_context(app->view_dispatcher, app);
    view_dispatcher_set_custom_event_callback(app->view_dispatcher, ptb_custom_event_callback);
    view_dispatcher_set_navigation_event_callback(app->view_dispatcher, ptb_back_event_callback);
    view_dispatcher_set_tick_event_callback(app->view_dispatcher, ptb_tick_callback, PTB_TICK_MS);
    furi_event_loop_subscribe_message_queue(
        view_dispatcher_get_event_loop(app->view_dispatcher),
        app->inbox,
        FuriEventLoopEventIn,
        ptb_inbox_callback,
        app);

    app->submenu = submenu_alloc();
    view_dispatcher_add_view(app->view_dispatcher, PtbViewSubmenu, submenu_get_view(app->submenu));
    app->text_box = text_box_alloc();
    view_dispatcher_add_view(
        app->view_dispatcher, PtbViewTextBox, text_box_get_view(app->text_box));
    app->popup = popup_alloc();
    view_dispatcher_add_view(app->view_dispatcher, PtbViewPopup, popup_get_view(app->popup));
    app->confirm_view = ptb_confirm_view_alloc();
    ptb_confirm_view_set_callback(app->confirm_view, ptb_confirm_callback, app);
    view_dispatcher_add_view(
        app->view_dispatcher, PtbViewConfirm, ptb_confirm_view_get_view(app->confirm_view));

    view_dispatcher_attach_to_gui(app->view_dispatcher, app->gui, ViewDispatcherTypeFullscreen);
    return app;
}

static void ptb_app_free(PtbApp* app) {
    /* Eerst RPC los: daarna lopen er geen callbacks meer die inbox of app gebruiken. */
    ptb_rpc_detach(app);
    furi_event_loop_unsubscribe(view_dispatcher_get_event_loop(app->view_dispatcher), app->inbox);

    view_dispatcher_remove_view(app->view_dispatcher, PtbViewConfirm);
    view_dispatcher_remove_view(app->view_dispatcher, PtbViewPopup);
    view_dispatcher_remove_view(app->view_dispatcher, PtbViewTextBox);
    view_dispatcher_remove_view(app->view_dispatcher, PtbViewSubmenu);
    ptb_confirm_view_free(app->confirm_view);
    popup_free(app->popup);
    text_box_free(app->text_box);
    submenu_free(app->submenu);
    view_dispatcher_free(app->view_dispatcher);
    scene_manager_free(app->scene_manager);

    furi_record_close(RECORD_NOTIFICATION);
    furi_record_close(RECORD_GUI);

    furi_string_free(app->view_text[1]);
    furi_string_free(app->view_text[0]);
    furi_string_free(app->progress);
    furi_string_free(app->result);
    furi_message_queue_free(app->inbox);
    furi_mutex_free(app->rpc_mutex);
    free(app);
}

int32_t pitoolbox_ui_app(void* p) {
    PtbApp* app = ptb_app_alloc();
    RpcAppSystem* rpc = ptb_parse_rpc_args(p);

    if(rpc) {
        app->rpc_mode = true;
        app->wait_state = PtbWaitConnecting;
        furi_mutex_acquire(app->rpc_mutex, FuriWaitForever);
        app->rpc = rpc;
        rpc_system_app_set_callback(rpc, ptb_rpc_callback, app);
        rpc_system_app_send_started(rpc);
        furi_mutex_release(app->rpc_mutex);
        ptb_send_hello(app);
    } else {
        app->wait_state = ptb_manual_start();
    }

    scene_manager_next_scene(app->scene_manager, PtbSceneWait);
    view_dispatcher_run(app->view_dispatcher);

    ptb_app_free(app);
    return 0;
}
