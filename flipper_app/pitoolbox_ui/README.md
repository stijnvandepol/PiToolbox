# PiToolbox-bediening op de Flipper (`pitoolbox_ui`)

Een Flipper-app (FAP) waarmee je PiToolbox op de Pi bedient, zonder telefoon of laptop:

- **Bevestigen op de Flipper:** stelt een agent een transmit voor, dan verschijnt de samenvatting op de Flipper. OK = ja, Back = nee, met aftellen. OK werkt pas na 1 s, zodat een knopdruk die al onderweg was nooit per ongeluk "ja" wordt.
- **Snelle acties** zonder LLM: status, wifi-scan (samenvatting), alle captures stoppen, noodstop.
- **Status:** uplink, voeding/onderspanning, Flipper-link (USB/BLE), actieve captures, open bevestigingen, laatste toolcall.
- **Presets:** vaste prompts uit de Pi-config gaan naar de agent-loop. Het antwoord kun je scrollen.

De app is een **dom invoerapparaat**. Hij stuurt alleen ids (actie, preset, bevestigings-handle), nooit toolnamen, argumenten of tekst. De Pi valideert alles en voert alles uit via de `ToolRunner` (frontend `flipper-ui`). Het protocol staat in [docs/fase4-protocol.md](../../docs/fase4-protocol.md).

> **Status:** de app bouwt zonder waarschuwingen tegen mntm-012 (API 87.1). De bridge op de Pi is klaar (`PITOOLBOX_FLIPPER_UI=on`, zie de README in de root) en getest tegen een nagebootste app. Op hardware getest op 2026-10-17 (installeren, rooktest, valtest 20/20; zie [Hardwaretest](#hardwaretest)); OK/Back en het menu moeten nog met de hand. Na een `ir_send` gaat de app even dicht en weer open: de Infrared-app heeft de loader nodig.

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

Zet `dist/pitoolbox_ui.fap` op de SD-kaart, in **`/ext/apps/Tools/pitoolbox_ui.fap`**. De Pi start de app op dat pad. Het makkelijkst gaat dat vanaf de Pi, via USB of BLE:

```bash
uv run pitoolbox-flipper ui-install            # standaard dist/pitoolbox_ui.fap; --transport usb|ble, --force
```

`ui-install` controleert het bestand eerst: een ELF voor ARM, het manifest, de naam "PiToolbox", en API en hardware-target gelijk aan de firmware. Een verkeerde API weigert hij, want "App Too Old/New" kon de Flipper laten crashen (spike §2). Daarna schrijft hij in blokken van 512 B naar `<pad>.part`, controleert de MD5 op de Flipper en hernoemt pas dan. Een mislukte upload laat de oude app dus staan. Is er een app open op de Flipper, dan weigert hij. Staat dezelfde versie er al, dan schrijft hij niets. Gemeten: 24,5 KB in 3,6 s over BLE.

Andere manieren:

- qFlipper: File manager → `SD Card/apps/Tools/`, en sleep het bestand erin;
- `ufbt launch` met de Flipper via USB (installeert en start, zonder RPC-koppeling, dus je ziet de handmatige start);

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

## Hardwaretest

Stop eerst `pitoolbox-mcp` of `pitoolbox-agent` als die de Flipper gebruiken. Er wordt nooit iets uitgezonden: alleen de tool `status` is toegestaan, en de test draait met een lege scope (een eigen tijdelijke map, niet jouw `scope.yaml`).

```bash
uv run pitoolbox-flipper ui-install                     # app erop (MD5-gecontroleerd)
uv run pitoolbox-flipper ui-test [--wait-action 30]     # starten, HELLO, WELCOME, STATUS, sluiten
uv run pitoolbox-flipper ui-test --confirm-check        # plus OK en Back op twee test-vragen
uv run pitoolbox-flipper ui-droptest --rounds 20        # valtest: BLE-link N keer verbreken
```

Met `--wait-action 30` kies je zelf op de Flipper Snelle acties → Status. Met `--confirm-check` test je "Flipper verplicht" met de hand. De Pi zet twee test-vragen klaar (`ir_send` naar `pitoolbox_confirm_check`, een apparaat dat nergens in scope staat). Bij vraag 1 ("Test 1/2: druk OK") controleert de test eerst dat `confirm_action` uit een chat geweigerd wordt (`confirm_on_flipper`). Daarna druk jij OK. De Pi moet dat als bevestigd op de Flipper zien, en de policy weigert de actie alsnog: hij staat niet in de allowlist en niet in de scope. In Status staat dan "Bevestiging: mislukt", en dat klopt. Bij vraag 2 ("Test 2/2: druk Back") druk je Back ("geweigerd"). Je hebt per vraag 60 s; met `--confirm-check 90` krijg je 90 s (hoogstens 110). Druk je de verkeerde knop of komt er niets binnen, dan zegt de test wat je op het scherm moet nakijken. `ui-droptest` verbreekt de BLE-link via BlueZ (D-Bus, geen root) terwijl de Pi STATUS-berichten stuurt. Daarna moet de Flipper op een ping antwoorden en de app opnieuw starten. De test stopt bij de eerste fout en zegt wat je van het scherm moet noteren.

**Resultaat 2026-10-17** (mntm-012, Pi 4, BLE via hci1):

- `ui-install` over BLE: 24.560 B in 3,6 s, MD5 klopt.
- `ui-test` over BLE: verbinden 8,1 s; daarna app gestart 0,9 s, HELLO 0,1 s, WELCOME 0,1 s, STATUS 0,6 s, sluiten 0,7 s.
- `ui-droptest --rounds 20`: **20 van 20 geslaagd**, geen bevriezing of BusFault. Verbreking gezien na mediaan 2,4 s, app terug na mediaan 6,8 s (max 8,7 s), ping daarna ~97 ms.
- Na de review-fixes (2026-10-17, 04:00): valtest opnieuw 20/20 (app terug mediaan 6,8 s, max 7,6 s); 5 minuten open en stil zonder verbreking; keepalive loopt door terwijl de app open is.
- Duurtest 40 minuten over BLE, app open, elke 10 s STATUS: 240/240 afgeleverd, 0 verbrekingen, geheugen stabiel.
- **Nog met de hand:** OK/Back op een echte bevestigingsvraag, de snelle acties en presets vanuit het menu, de handmatige start (verzoekbestand).

### Op afstand (alleen voor ontwikkeling)

Om hardwaretests zonder mens te kunnen draaien, kan de Pi knoppen indrukken en het scherm lezen via de officiële GUI-RPC (`gui.proto`: `SendInputEventRequest`, de schermstream). Geen reverse engineering, geen extra afhankelijkheid.

```bash
uv run pitoolbox-flipper remote screen [--png scherm.png] [--scale 4]   # scherm als tekst (en PNG)
uv run pitoolbox-flipper remote press ok [--long] [--times 3]           # up/down/left/right/ok/back
uv run pitoolbox-flipper ui-test --confirm-check --auto                 # OK en Back drukt de Pi zelf
```

Beide kennen `--transport`, `--address` en `--adapter` zoals de andere commando's. Een klik is PRESS, SHORT (of LONG met `--long`), RELEASE, zoals de officiële Python-client. Eén schermbeeld kost over BLE ongeveer een seconde (1 KiB); de stream stopt altijd weer, ook na een time-out of fout.

**De wachter.** Vóór elke klik vraagt de Pi de loader opnieuw. Drukken mag alleen als er geen app open is (bureaublad, menu, Apps-browser: dat zijn geen apps voor de loader) of als de open app de PiToolbox-app is die **dit proces** via RPC startte. Staat er een andere app open, dan weigert de Pi: een OK op afstand in Sub-GHz of Infrared kan uitzenden. Elke klik staat als WARNING in de log (`invoer op afstand: OK`). Gevolgen: met `remote press` navigeer je vanaf het bureaublad, bijvoorbeeld naar Apps → Tools → PiToolbox voor de handmatige start (de handmatige app sluit zichzelf na 1,5 s, dus daarna is de loader weer vrij). Binnen de PiToolbox-app drukken kan alleen het proces dat hem startte: `ui-test --confirm-check --auto`. Die drukt OK op vraag 1 en Back op vraag 2, pas nadat de bridge CONFIRM_PROMPT voor die vraag meldde, en dan nog 1,5 s later (de app telt OK pas na `PTB_CONFIRM_ARM_MS`, 1 s). De tijdlijn zegt "OK op afstand gedrukt". Let op: één klik vanaf een vrije loader kan een app openen die je met een sneltoets op het bureaublad hebt ingesteld; de volgende klik weigert dan.

**Waarom alleen voor ontwikkeling.** Met invoer op afstand kan de Pi zelf OK drukken op een "Flipper verplicht"-vraag, en dan bewijst die bevestiging niets meer. In `ui-test` mag het, want daar laat de runner alleen `status` toe en is de scope leeg: niets kan uitzenden. In het product nooit: er is geen tool voor, en een test (`tests/test_flipper_remote.py`) controleert dat de tools, de runner, MCP, de agent en de bridge `gui_press`/`gui_screen` niet kunnen bereiken.
