#pragma once

#include <gui/scene_manager.h>

typedef enum {
    PtbSceneWait, /* verbinden / offline / handmatige start */
    PtbSceneMenu, /* hoofdmenu */
    PtbSceneQuick, /* snelle acties */
    PtbScenePresets, /* vaste prompts naar de agent */
    PtbSceneStatus,
    PtbSceneResult, /* scrollbaar antwoord */
    PtbSceneConfirm, /* bevestigen: OK = ja, Back = nee */
    PtbSceneNum,
} PtbScene;

extern const SceneManagerHandlers ptb_scene_handlers;
