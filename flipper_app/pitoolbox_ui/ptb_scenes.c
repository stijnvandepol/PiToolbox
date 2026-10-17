/* Scenes van de PiToolbox-bediening. Eén bestand: elke scene is klein. */
#include "pitoolbox_ui_i.h"

static void ptb_exit(PtbApp* app) {
    scene_manager_stop(app->scene_manager);
    view_dispatcher_stop(app->view_dispatcher);
}

/* --- Wacht: verbinden, offline, handmatige start ------------------------------------------ */

static void ptb_scene_wait_show(PtbApp* app) {
    Popup* popup = app->popup;
    const char* text = "Verbinden met Pi...";
    uint32_t timeout_ms = 0; /* 0 = blijft staan tot Back */

    switch(app->wait_state) {
    case PtbWaitConnecting:
        break;
    case PtbWaitNoAnswer:
        text = "Pi antwoordt niet.\nBack = afsluiten";
        break;
    case PtbWaitOffline:
        text = "Verbinding met Pi\nverbroken.";
        timeout_ms = 2000;
        break;
    case PtbWaitManualSent:
        text = "Pi wordt gevraagd de\napp te openen...";
        timeout_ms = 1500;
        break;
    case PtbWaitManualNoLink:
        text = "Geen verbinding met Pi.\nBluetooth aan en\nPi in de buurt?";
        timeout_ms = 4000;
        break;
    case PtbWaitManualError:
        text = "Kon verzoek niet\nopslaan (SD-kaart?).";
        timeout_ms = 3000;
        break;
    }

    /* Geen popup-timer: die start alleen bij het binnenkomen van de view, en deze tekst
     * verandert soms terwijl de popup al zichtbaar is. De tick controleert de deadline. */
    popup_reset(popup);
    popup_set_header(popup, "PiToolbox", 64, 2, AlignCenter, AlignTop);
    popup_set_text(popup, text, 64, 36, AlignCenter, AlignCenter);
    app->wait_deadline = timeout_ms ? furi_get_tick() + timeout_ms : 0;
}

static void ptb_scene_wait_on_enter(void* context) {
    PtbApp* app = context;
    ptb_scene_wait_show(app);
    view_dispatcher_switch_to_view(app->view_dispatcher, PtbViewPopup);
}

static bool ptb_scene_wait_on_event(void* context, SceneManagerEvent event) {
    PtbApp* app = context;
    if(event.type == SceneManagerEventTypeBack) {
        ptb_exit(app);
        return true;
    }
    if(event.type == SceneManagerEventTypeTick) {
        if(app->wait_deadline && (int32_t)(furi_get_tick() - app->wait_deadline) >= 0) {
            ptb_exit(app);
        }
        return true;
    }
    if(event.type != SceneManagerEventTypeCustom) return false;
    switch(event.event) {
    case PtbEventWelcome:
        scene_manager_next_scene(app->scene_manager, PtbSceneMenu);
        return true;
    case PtbEventWaitChanged:
        ptb_scene_wait_show(app);
        return true;
    default:
        return false;
    }
}

static void ptb_scene_wait_on_exit(void* context) {
    PtbApp* app = context;
    app->wait_deadline = 0;
    popup_reset(app->popup);
}

/* --- Hoofdmenu ---------------------------------------------------------------------------- */

enum {
    PtbMenuQuick,
    PtbMenuPresets,
    PtbMenuStatus,
    PtbMenuLastAnswer,
};

static void ptb_scene_menu_on_enter(void* context) {
    PtbApp* app = context;
    Submenu* submenu = app->submenu;
    submenu_reset(submenu);
    submenu_set_header(submenu, "PiToolbox");
    submenu_add_item(submenu, "Snelle acties", PtbMenuQuick, ptb_submenu_callback, app);
    submenu_add_item(submenu, "Presets (agent)", PtbMenuPresets, ptb_submenu_callback, app);
    submenu_add_item(submenu, "Status", PtbMenuStatus, ptb_submenu_callback, app);
    submenu_add_item(submenu, "Laatste antwoord", PtbMenuLastAnswer, ptb_submenu_callback, app);
    submenu_set_selected_item(
        submenu, scene_manager_get_scene_state(app->scene_manager, PtbSceneMenu));
    view_dispatcher_switch_to_view(app->view_dispatcher, PtbViewSubmenu);
}

static bool ptb_scene_menu_on_event(void* context, SceneManagerEvent event) {
    PtbApp* app = context;
    if(event.type == SceneManagerEventTypeBack) {
        ptb_exit(app); /* niet terug naar "Verbinden..." */
        return true;
    }
    if(event.type != SceneManagerEventTypeCustom || event.event < PtbEventMenuBase) return false;
    uint32_t index = event.event - PtbEventMenuBase;
    scene_manager_set_scene_state(app->scene_manager, PtbSceneMenu, index);
    static const uint32_t next[] = {
        [PtbMenuQuick] = PtbSceneQuick,
        [PtbMenuPresets] = PtbScenePresets,
        [PtbMenuStatus] = PtbSceneStatus,
        [PtbMenuLastAnswer] = PtbSceneResult,
    };
    if(index >= COUNT_OF(next)) return false;
    scene_manager_next_scene(app->scene_manager, next[index]);
    return true;
}

static void ptb_scene_menu_on_exit(void* context) {
    PtbApp* app = context;
    submenu_reset(app->submenu);
}

/* --- Snelle acties (vaste ids; de Pi koppelt ze aan vaste tool-calls) --------------------- */

static const struct {
    uint8_t id;
    const char* label;
} ptb_quick_items[] = {
    {PtbQuickStatus, "Status"},
    {PtbQuickWifiScan, "Wifi-scan"},
    {PtbQuickStopCaptures, "Stop captures"},
    {PtbQuickEmergencyStop, "NOODSTOP"},
};

static void ptb_scene_quick_on_enter(void* context) {
    PtbApp* app = context;
    Submenu* submenu = app->submenu;
    submenu_reset(submenu);
    submenu_set_header(submenu, "Snelle acties");
    for(size_t i = 0; i < COUNT_OF(ptb_quick_items); i++) {
        submenu_add_item(submenu, ptb_quick_items[i].label, i, ptb_submenu_callback, app);
    }
    /* Terug uit het resultaat: de cursor blijft op de gekozen actie (zoals het hoofdmenu). */
    submenu_set_selected_item(
        submenu, scene_manager_get_scene_state(app->scene_manager, PtbSceneQuick));
    view_dispatcher_switch_to_view(app->view_dispatcher, PtbViewSubmenu);
}

static bool ptb_scene_quick_on_event(void* context, SceneManagerEvent event) {
    PtbApp* app = context;
    if(event.type != SceneManagerEventTypeCustom || event.event < PtbEventMenuBase) return false;
    uint32_t index = event.event - PtbEventMenuBase;
    if(index >= COUNT_OF(ptb_quick_items)) return false;
    scene_manager_set_scene_state(app->scene_manager, PtbSceneQuick, index);
    /* Eén actie tegelijk, behalve de noodstop: die mag altijd (de Pi voert hem direct uit). */
    bool emergency = ptb_quick_items[index].id == PtbQuickEmergencyStop;
    if(!app->busy || emergency) {
        ptb_send_action(app, PtbActionQuick, ptb_quick_items[index].id, ptb_quick_items[index].label);
    }
    scene_manager_next_scene(app->scene_manager, PtbSceneResult);
    return true;
}

static void ptb_scene_quick_on_exit(void* context) {
    PtbApp* app = context;
    submenu_reset(app->submenu);
}

/* --- Presets (labels van de Pi; de prompttekst blijft op de Pi) --------------------------- */

static void ptb_scene_presets_fill(PtbApp* app) {
    Submenu* submenu = app->submenu;
    submenu_reset(submenu);
    submenu_set_header(submenu, "Presets");
    if(!app->presets_valid || app->presets.count == 0) {
        submenu_add_item(
            submenu,
            app->presets_valid ? "(geen presets)" : "(ophalen...)",
            0xFF,
            ptb_submenu_callback,
            app);
    } else {
        for(uint8_t i = 0; i < app->presets.count; i++) {
            submenu_add_item(submenu, app->presets.label[i], i, ptb_submenu_callback, app);
        }
        /* Ook na het verversen van de lijst: de cursor blijft op de laatst gekozen preset. */
        submenu_set_selected_item(
            submenu, scene_manager_get_scene_state(app->scene_manager, PtbScenePresets));
    }
}

static void ptb_scene_presets_on_enter(void* context) {
    PtbApp* app = context;
    if(!app->presets_valid) ptb_send_presets_request(app);
    ptb_scene_presets_fill(app);
    view_dispatcher_switch_to_view(app->view_dispatcher, PtbViewSubmenu);
}

static bool ptb_scene_presets_on_event(void* context, SceneManagerEvent event) {
    PtbApp* app = context;
    if(event.type != SceneManagerEventTypeCustom) return false;
    if(event.event == PtbEventPresets) {
        ptb_scene_presets_fill(app);
        return true;
    }
    if(event.event < PtbEventMenuBase) return false;
    uint32_t index = event.event - PtbEventMenuBase;
    if(!app->presets_valid || index >= app->presets.count) return true; /* placeholder */
    scene_manager_set_scene_state(app->scene_manager, PtbScenePresets, index);
    if(!app->busy) {
        ptb_send_action(app, PtbActionPreset, app->presets.id[index], app->presets.label[index]);
    }
    scene_manager_next_scene(app->scene_manager, PtbSceneResult);
    return true;
}

static void ptb_scene_presets_on_exit(void* context) {
    PtbApp* app = context;
    submenu_reset(app->submenu);
}

/* --- Status ------------------------------------------------------------------------------- */

static void ptb_scene_status_show(PtbApp* app) {
    FuriString* t = ptb_text_begin(app);
    if(!app->status_valid) {
        furi_string_set_str(t, "Status ophalen...");
    } else {
        const PtbStatus* s = &app->status;
        /* Regels binnen de 120 px van de TextBox (die breekt per teken af, ook midden in een
         * woord): wat lang kan zijn (adres, "eerder onderspanning", toolnaam) op een eigen
         * regel. Breedtes gemeten met de tabel van FontSecondary (_font_widths.py op de Pi). */
        const char* power = "OK";
        if(s->flags & PTB_STATUS_POWER_UNKNOWN) {
            power = "onbekend";
        } else if(s->flags & PTB_STATUS_UNDERVOLTAGE_NOW) {
            power = "ONDERSPANNING!";
        } else if(s->flags & PTB_STATUS_UNDERVOLTAGE_OCCURRED) {
            power = "OK\n(eerder onderspanning)";
        }
        static const char* const links[] = {"?", "USB", "BLE"};
        furi_string_printf(
            t,
            "Uplink: %s\n%s\nVoeding: %s\nFlipper-link: %s\nCaptures: %u actief\n"
            "Open bevestigingen: %u\n",
            (s->flags & PTB_STATUS_UPLINK_UP) ? "aan" : "UIT",
            s->uplink,
            power,
            links[s->link],
            s->captures,
            s->pending);
        if(s->last_result == PtbLastNone) {
            furi_string_cat_str(t, "Laatste tool: -\n");
        } else {
            furi_string_cat_printf(
                t, "Laatste tool: %s", s->last_result == PtbLastOk ? "OK" : "FOUT");
            if(s->last_age_s != PTB_AGE_UNKNOWN) {
                furi_string_cat_printf(t, " (%us)", s->last_age_s);
            }
            furi_string_cat_printf(t, "\n%s\n", s->last_tool);
        }
    }
    furi_string_cat_printf(t, "Bevestiging: %s", app->last_confirm_text);
    ptb_text_commit(app);
}

static void ptb_scene_status_on_enter(void* context) {
    PtbApp* app = context;
    text_box_reset(app->text_box);
    text_box_set_font(app->text_box, TextBoxFontText);
    ptb_scene_status_show(app);
    ptb_send_status_request(app);
    view_dispatcher_switch_to_view(app->view_dispatcher, PtbViewTextBox);
}

static bool ptb_scene_status_on_event(void* context, SceneManagerEvent event) {
    PtbApp* app = context;
    if(event.type == SceneManagerEventTypeCustom && event.event == PtbEventStatus) {
        ptb_scene_status_show(app);
        return true;
    }
    return false;
}

static void ptb_scene_status_on_exit(void* context) {
    PtbApp* app = context;
    text_box_reset(app->text_box);
}

/* --- Resultaat (scrollbaar) ----------------------------------------------------------------- */

static void ptb_scene_result_show(PtbApp* app) {
    FuriString* t = ptb_text_begin(app);
    if(app->busy) {
        furi_string_printf(t, "Bezig...\n%s", furi_string_get_cstr(app->progress));
        if(app->next_part) {
            furi_string_cat_printf(t, "\n(deel %u/%u)", app->next_part, app->total_parts);
        }
    } else if(furi_string_empty(app->result)) {
        furi_string_set_str(t, "Nog geen antwoord.");
    } else {
        furi_string_set(t, app->result);
    }
    ptb_text_commit(app);
}

static void ptb_scene_result_on_enter(void* context) {
    PtbApp* app = context;
    text_box_reset(app->text_box);
    text_box_set_font(app->text_box, TextBoxFontText);
    text_box_set_focus(app->text_box, TextBoxFocusStart);
    ptb_scene_result_show(app);
    view_dispatcher_switch_to_view(app->view_dispatcher, PtbViewTextBox);
}

static bool ptb_scene_result_on_event(void* context, SceneManagerEvent event) {
    PtbApp* app = context;
    if(event.type == SceneManagerEventTypeCustom && event.event == PtbEventResult) {
        ptb_scene_result_show(app);
        return true;
    }
    if(event.type == SceneManagerEventTypeBack && app->busy) {
        ptb_send_cancel(app); /* Back tijdens een actie = afbreken */
    }
    return false; /* Back: gewoon terug */
}

static void ptb_scene_result_on_exit(void* context) {
    PtbApp* app = context;
    text_box_reset(app->text_box);
}

/* --- Bevestigen: OK = ja, Back = nee, met aftellen ------------------------------------------ */

static void ptb_scene_confirm_on_enter(void* context) {
    PtbApp* app = context;
    ptb_confirm_view_set_prompt(
        app->confirm_view,
        app->confirm_summary,
        ptb_confirm_remaining_s(app),
        app->confirm_handle);
    view_dispatcher_switch_to_view(app->view_dispatcher, PtbViewConfirm);
}

static bool ptb_scene_confirm_on_event(void* context, SceneManagerEvent event) {
    PtbApp* app = context;
    if(event.type == SceneManagerEventTypeTick) {
        uint8_t left = ptb_confirm_remaining_s(app);
        ptb_confirm_view_set_remaining(app->confirm_view, left);
        if(left == 0 && app->confirm_active) {
            /* Verlopen: niets sturen. De Pi laat de handle zelf verlopen (zijn klok telt). */
            app->confirm_active = false;
            app->last_confirm_text = "verlopen";
            scene_manager_previous_scene(app->scene_manager);
        }
        return true;
    }
    if(event.type == SceneManagerEventTypeBack) {
        return true; /* de view vangt Back zelf af als "nee" */
    }
    if(event.type != SceneManagerEventTypeCustom) return false;
    switch(event.event) {
    case PtbEventConfirmAnswered:
        /* Het antwoord ging al weg bij de knopdruk (ptb_confirm_callback). Kwam er intussen
         * een nieuwe vraag binnen, dan staat die nu open: blijven. */
        if(!app->confirm_active) scene_manager_previous_scene(app->scene_manager);
        return true;
    case PtbEventConfirmPrompt: /* nieuwe vraag terwijl dit scherm open is */
        ptb_scene_confirm_on_enter(app);
        return true;
    case PtbEventConfirmClosed: /* elders afgehandeld, verlopen of ingetrokken */
        scene_manager_previous_scene(app->scene_manager);
        return true;
    default:
        return false;
    }
}

static void ptb_scene_confirm_on_exit(void* context) {
    UNUSED(context);
}

/* --- tabel voor de scene manager ------------------------------------------------------------ */

static void (*const ptb_on_enter[])(void*) = {
    [PtbSceneWait] = ptb_scene_wait_on_enter,
    [PtbSceneMenu] = ptb_scene_menu_on_enter,
    [PtbSceneQuick] = ptb_scene_quick_on_enter,
    [PtbScenePresets] = ptb_scene_presets_on_enter,
    [PtbSceneStatus] = ptb_scene_status_on_enter,
    [PtbSceneResult] = ptb_scene_result_on_enter,
    [PtbSceneConfirm] = ptb_scene_confirm_on_enter,
};

static bool (*const ptb_on_event[])(void*, SceneManagerEvent) = {
    [PtbSceneWait] = ptb_scene_wait_on_event,
    [PtbSceneMenu] = ptb_scene_menu_on_event,
    [PtbSceneQuick] = ptb_scene_quick_on_event,
    [PtbScenePresets] = ptb_scene_presets_on_event,
    [PtbSceneStatus] = ptb_scene_status_on_event,
    [PtbSceneResult] = ptb_scene_result_on_event,
    [PtbSceneConfirm] = ptb_scene_confirm_on_event,
};

static void (*const ptb_on_exit[])(void*) = {
    [PtbSceneWait] = ptb_scene_wait_on_exit,
    [PtbSceneMenu] = ptb_scene_menu_on_exit,
    [PtbSceneQuick] = ptb_scene_quick_on_exit,
    [PtbScenePresets] = ptb_scene_presets_on_exit,
    [PtbSceneStatus] = ptb_scene_status_on_exit,
    [PtbSceneResult] = ptb_scene_result_on_exit,
    [PtbSceneConfirm] = ptb_scene_confirm_on_exit,
};

const SceneManagerHandlers ptb_scene_handlers = {
    .on_enter_handlers = ptb_on_enter,
    .on_event_handlers = ptb_on_event,
    .on_exit_handlers = ptb_on_exit,
    .scene_num = PtbSceneNum,
};
