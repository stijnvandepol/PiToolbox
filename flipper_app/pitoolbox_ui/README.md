# PiToolbox-bediening op de Flipper (`pitoolbox_ui`)

Een Flipper-app (FAP) waarmee je PiToolbox op de Pi bedient, zonder telefoon of laptop:

- **Bevestigen op de Flipper:** stelt een agent een transmit voor, dan verschijnt de samenvatting op de Flipper. OK = ja, Back = nee, met aftellen. OK werkt pas na 1 s, zodat een knopdruk die al onderweg was nooit per ongeluk "ja" wordt.
- **Snelle acties** zonder LLM: status, wifi-scan (samenvatting), alle captures stoppen, noodstop.
- **Status:** uplink, voeding/onderspanning, Flipper-link (USB/BLE), actieve captures, open bevestigingen, laatste toolcall.
- **Presets:** vaste prompts uit de Pi-config gaan naar de agent-loop. Het antwoord kun je scrollen.

De app is een **dom invoerapparaat**. Hij stuurt alleen ids (actie, preset, bevestigings-handle), nooit toolnamen, argumenten of tekst. De Pi valideert alles en voert alles uit via de `ToolRunner` (frontend `flipper-ui`). Het protocol staat in [docs/fase4-protocol.md](../../docs/fase4-protocol.md).

> **Status:** de app bouwt zonder waarschuwingen tegen mntm-012 (API 87.1). De bridge op de Pi is klaar (`PITOOLBOX_FLIPPER_UI=on`, zie de README in de root) en getest tegen een nagebootste app. Op hardware is de app nog **niet getest** (de Flipper was offline na de BusFault uit de spike). Na een `ir_send` gaat de app even dicht en weer open: de Infrared-app heeft de loader nodig.

## Bestanden

| Bestand | Wat |
|---|---|
| `application.fam` | manifest: appid `pitoolbox_ui`, categorie Tools, stack 4 KiB |
| `pitoolbox_ui.c` | start, RPC-koppeling (callback, mutex, versturen), berichten van de Pi, handmatige start |
| `pitoolbox_ui_i.h` | app-struct en draadmodel |
| `ptb_scenes.c/.h` | scenes: wachten/verbinden, hoofdmenu, snelle acties, presets, status, resultaat, bevestigen |
| `ptb_confirm_view.c/.h` | eigen view: samenvatting met woordomloop en scrollen, aftellen, OK/Back |
| `ptb_proto.c/.h` | codec, puur C (zonder furi), dezelfde layouts als `protocol.py` |
| `pitoolbox_ui.png` | icoon 10×10, 1-bit |

## Bouwen

Bouw **altijd tegen de SDK van exact de firmware op de Flipper**. Bij een API-mismatch toont de Flipper "App Too Old/New". Kies je daar Cancel terwijl de app via RPC gestart is, dan volgt in Momentum een `furi_crash()` (fase4-spike.md §2). Voor Stijns Flipper is dat Momentum **mntm-012**, API **87.1**.

```bash
# eenmalig: ufbt in een eigen venv (Kali blokkeert systeem-pip)
uv venv ~/.venvs/ufbt && uv pip install --python ~/.venvs/ufbt ufbt
~/.venvs/ufbt/bin/ufbt update --index-url=https://up.momentum-fw.dev/firmware/directory.json --channel=release
#   "release" is de NIEUWSTE Momentum-release. Is dat niet meer mntm-012, gebruik dan
#   --local <pad>/flipper-z-f7-sdk-mntm-012.zip (de SDK-zip uit directory.json).
#   controleer: ~/.ufbt/current/sdk_headers/f7_sdk/targets/f7/api_symbols.csv begint met "Version,+,87.1"

cd flipper_app/pitoolbox_ui
~/.venvs/ufbt/bin/ufbt          # -> dist/pitoolbox_ui.fap
```

Laatste controle bij elke build: de regel `APPCHK … Target: 7, API: 87.1` in de uitvoer. ufbt bouwt met `-Wall -Wextra -Werror`, dus de build is alleen geslaagd zonder waarschuwingen. Gebouwd en gecontroleerd met ufbt 0.2.6: 0 waarschuwingen, `.text` ongeveer 8 KiB, `.fap` 24 KiB. `dist/` en `.vscode/` staan in `.gitignore`.

## Installeren

Zet `dist/pitoolbox_ui.fap` op de SD-kaart, in **`/ext/apps/Tools/pitoolbox_ui.fap`**. De Pi start de app op dat pad. Dat kan op drie manieren:

- qFlipper: File manager → `SD Card/apps/Tools/`, en sleep het bestand erin;
- `ufbt launch` met de Flipper via USB (installeert en start, zonder RPC-koppeling, dus je ziet de handmatige start);
- via de storage-RPC van de Pi (`storage_write_request` in blokken van 512 B, daarna `storage_md5sum_request` ter controle).

## Gebruik

- **Via de Pi:** de Pi start de app alleen als er geen andere app open is (`app_lock_status`). Hij sluit nooit een app van jou.
- **Zelf openen** (Apps → Tools → PiToolbox): de app heeft dan geen kanaal naar de Pi. Hij laat een verzoek achter in `/ext/apps_data/pitoolbox_ui/start_request`, toont "Pi wordt gevraagd de app te openen..." en sluit. De Pi pollt dat bestand en start de app binnen een paar seconden opnieuw, dit keer gekoppeld. Is er geen host verbonden, dan zie je "Geen verbinding met Pi" en blijft er niets achter.
- Back in het hoofdmenu sluit de app. Back tijdens een lopende actie breekt die af. De noodstop werkt altijd, ook als er al iets loopt.
- Valt de verbinding weg, dan zie je 2 s "Verbinding met Pi verbroken" en sluit de app. Na het opnieuw verbinden start de Pi hem weer.
- Raakt alleen de Pi de sessie kwijt (bijv. een bericht kwam niet aan), dan zie je even "Verbinden met Pi..." en groet de app opnieuw (ERROR `NOT_READY`, protocol §5). Een open vraag komt daarna terug, met een nieuwe handle.
- Een antwoord (OK/Back) hoort altijd bij de vraag die op het scherm stond toen je drukte. Is die vraag intussen gesloten of vervangen, dan stuurt de app niets.

## Veiligheid rond sessieverlies

In de spike crashte de Flipper met een BusFault toen BLE wegviel terwijl een test-app draaide. Deze app volgt de eisen uit de spike. De onderbouwing met bronregels staat in het protocoldocument, §9. Kort:

1. Eén `FuriMutex` rond elke `rpc_system_app_*`-aanroep, steeds na de controle `app->rpc != NULL`.
2. SessionClose (komt binnen in de **timer-thread**): onder de mutex de callback op NULL, de pointer op NULL en een vlag zetten. Geen confirm, geen GUI. Daarna nooit meer een RPC-aanroep.
3. De mutex blijft vast tijdens `rpc_system_app_exchange_data`, zodat het `RpcAppSystem` niet tijdens het versturen vrijgegeven kan worden. Dat blokkeert niet eeuwig: BLE stopt op `DISCONNECTED`, en USB stopt bij een gebroken pipe.
4. Elke DataExchange, AppExit en andere opdracht wordt **precies één keer** bevestigd, in de callback zelf.
5. Callbacks wachten nergens op. Ze kopiëren naar een eigen wachtrij (zonder te blokkeren), en de app-thread leest die via de event loop.
6. Bij afsluiten eerst ontkoppelen en wachten tot er geen callback meer loopt. Pas daarna alles vrijgeven, met de juiste `*_free()`.
7. Geen notificatie per bericht. Alleen één keer licht en één keer trillen bij een bevestigingsvraag.

## Hardwaretest (nog te doen)

1. App installeren in `/ext/apps/Tools/`. Start via RPC met `args:"RPC"`: verwacht `APP_STARTED` en daarna HELLO (`50 01 01 …`).
2. WELCOME/STATUS/PRESETS met de hand sturen (scratchscript met `encode()` uit `protocol.py`) en het scherm controleren (screen stream).
3. Handmatige start zonder Pi en met Pi: het verzoekbestand verschijnt en verdwijnt, en de app sluit zelf.
4. **Valtest:** BLE verbreken terwijl de FAP verstuurt, minstens 20 keer. Stop bij de eerste bevriezing of BusFault en noteer wat er op het scherm staat.
5. Na afloop: de Flipper op het bureaublad, zonder open app.
