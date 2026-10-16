/**
 * Bevestigingsscherm: samenvatting (scrollbaar), aftellen, OK = ja, Back = nee.
 *
 * OK werkt pas na PTB_CONFIRM_ARM_MS, zodat een knopdruk die al onderweg was (bijv. OK in
 * een menu op het moment dat de vraag binnenkomt) nooit per ongeluk "ja" wordt.
 */
#pragma once

#include <gui/view.h>
#include <stdbool.h>
#include <stdint.h>

#define PTB_CONFIRM_ARM_MS 1000

typedef struct PtbConfirmView PtbConfirmView;

/** approve: true = OK (ja), false = Back (nee). Loopt in de thread van de view_dispatcher. */
typedef void (*PtbConfirmCallback)(void* context, bool approve);

PtbConfirmView* ptb_confirm_view_alloc(void);
void ptb_confirm_view_free(PtbConfirmView* confirm);
View* ptb_confirm_view_get_view(PtbConfirmView* confirm);
void ptb_confirm_view_set_callback(
    PtbConfirmView* confirm,
    PtbConfirmCallback callback,
    void* context);

/** Nieuwe vraag: tekst wordt gekopieerd, scroll terug naar boven, OK opnieuw vergrendeld. */
void ptb_confirm_view_set_prompt(PtbConfirmView* confirm, const char* summary, uint8_t remaining_s);

/** Aftellen bijwerken (elke tick); tekent ook opnieuw zodra OK vrijgegeven wordt. */
void ptb_confirm_view_set_remaining(PtbConfirmView* confirm, uint8_t remaining_s);
