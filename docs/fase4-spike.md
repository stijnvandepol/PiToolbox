# Fase 4 spike: Flipper als bediening

Bron: Momentum **mntm-012** (commit `e1784e7`, API-tabel `Version 87.1`), protobuf-submodule `ea4f185` (= 0.25 + 5 commits). Waar de officiële firmware (OFW, branch `release` @ `9e810dc`, API 88.2) afwijkt, staat dat erbij. Alles komt uit de broncode. Wat alleen op hardware te beslissen is, staat onder "Open vragen".

Afkortingen voor links: `M` = `https://github.com/Next-Flip/Momentum-Firmware/blob/mntm-012/`, `P` = `https://github.com/Next-Flip/flipperzero-protobuf/blob/ea4f185f5eaa265955c520eae2832887ee6aa5e4/`, `O` = `https://github.com/flipperdevices/flipperzero-firmware/blob/9e810dc399e027e48ce07751df72168abbb19065/`.

## 1. Kan een FAP het seriële BLE-profiel gebruiken?

**Technisch geëxporteerd: ja. Bruikbaar als eigen kanaal naast RPC: nee.**

- In `targets/f7/api_symbols.csv` staan met `+` (dus beschikbaar voor FAP's): `bt_profile_start`, `bt_profile_restore_default`, `bt_disconnect`, `bt_set_status_changed_callback` (r. 826-828, 813), `ble_profile_serial_set_event_callback`, `ble_profile_serial_tx`, `ble_profile_serial_set_rpc_active` (r. 790-793), de variabele `ble_profile_serial` (r. 4365) en zelfs `ble_svc_serial_start` en `rpc_session_open` (r. 809, 3298). OFW exporteert dezelfde symbolen.
- `bt_profile_start` → `bt_change_profile` sluit **eerst** de RPC-sessie (`bt_close_rpc_connection`) en herstart core 2. De verbinding met de Pi valt dus altijd weg (`M/applications/services/bt/bt_service/bt.c#L405-L433`, `bt.h#L25-L46`).
- Het seriële profiel negeert zijn `params`, met dezelfde MAC en naam (`M/targets/f7/ble_glue/profiles/serial_profile.c#L20-L21`, `#L66-L77`). Start een FAP `ble_profile_serial`, dan opent bt.c bij de volgende `GapEventTypeConnected` toch weer een `RpcOwnerBle`-sessie en claimt hij de event-callback van de seriële service (`bt.c#L269-L273`, `#L370-L392`). Er is maar één callback per service, dus een FAP zou die na elke connect moeten "stelen". Daarna zitten bt.c (TX via `bt_rpc_send_bytes_callback`) en de FAP door elkaar te praten. Dat is een race en geen ondersteund patroon.
- Het nette alternatief is wat `hid_app` doet: een **eigen profiel** starten, met eigen keys-opslag, en bij afsluiten `bt_profile_restore_default` aanroepen (`M/applications/system/hid_app/hid.c#L331-L359`). Zolang dat loopt, is er **geen RPC over BLE** (`bt_open_rpc_connection` werkt alleen bij het seriële profiel). De Pi moet dan opnieuw koppelen met een andere GATT-service. Het herstel geeft weer een core-2-herstart, en de Pi moet opnieuw verbinden.
- In Momentum gebruikt geen enkele in-tree app `ble_profile_serial_set_event_callback`, behalve bt.c zelf. Het enige externe voorbeeld dat ik vond (andydixon/flipper-bluepp, "GATT server"-echo) draait op eigen firmware en is niet geverifieerd.
- Extra Momentum-gedrag: bij een vergrendelde desktop sluit Momentum BLE-RPC, tenzij `allow_locked_rpc_ble` aanstaat (standaard UIT) (`M/applications/services/desktop/desktop.c#L408-L449`, `M/applications/services/rpc/rpc.c#L388-L392`). In OFW bestaat die check niet.

**Conclusie:** gebruik het seriële profiel niet vanuit de FAP. Het kost de RPC-sessie en is racegevoelig. Er is een beter kanaal (zie 3).

## 2. Apps starten via RPC

- **Al een app open:** `rpc_system_app_start_process` → `loader_start`. Bij `LoaderStatusErrorAppStarted` komt **`ERROR_APP_SYSTEM_LOCKED` (17)** terug (`M/applications/services/rpc/rpc_app.c#L91-L102`). De loader geeft die status zodra `loader->app.thread != NULL` (`M/applications/services/loader/loader.c#L684-L716`). Er is geen preemptie: de Pi kan een draaiende app niet vervangen. Met `app_lock_status_request` kan de Pi vooraf vragen of de loader bezet is (`rpc_app.c#L113-L137`).
- **Op pad:** `name` mag een pad zijn. `loader_do_start_by_name` probeert eerst interne apps, dan `FLIPPER_EXTERNAL_APPS`, en daarna `storage_file_exists(name)` → `loader_start_external_app` (`loader.c#L734-L764`). `/ext/apps/Tools/pitoolbox.fap` werkt dus. Een onbekend pad geeft `ERROR_INVALID_PARAMETERS`.
- **Args:** `StartRequest.args` wordt ongewijzigd doorgegeven, behalve als het precies `"RPC"` is. Dan vervangt de firmware het door `"RPC %08lX"` met de pointer naar `RpcAppSystem` (`rpc_app.c#L82-L89`). Voor FAP's doet `flipper_application_alloc_thread` een `strdup(args)`, en de entry point krijgt dat als `void* p` (C-string) (`M/lib/flipper_application/flipper_application.c#L269-L286`). Interne apps krijgen ook een kopie (`loader.c#L446-L453`).
- **Limiet:** `application.options` zet `args` op `FT_POINTER` met `max_length:512` (`P/application.options`). Bij FT_POINTER legt nanopb volgens mij geen `max_length` op. Het RPC-decodepad heeft `bytes_left = SIZE_MAX` en een streambuffer van 1024 B (`rpc.c`, `rpc.h#L12`). Advies: houd args ≤ 256 B en test 512+ (zie testplan).
- **Let op, crashpad (alleen Momentum):** de mapping in `rpc_app.c#L92-L102` kent `LoaderStatusErrorApiMismatchCanceled` niet. Zie je een "App Too Old/New"-dialoog en kies je Cancel, dan volgt **`furi_crash()`**. Ook blokkeert die dialoog de RPC-worker (`loader.c#L578-L612`). De FAP moet dus exact tegen de SDK van mntm-012 (API 87.1) gebouwd worden.
- **Gevolg voor de CLI:** `subghz` en `rfid` zijn `CliCommandFlagDefault`. De shell doet dan `loader_lock` en weigert met "cannot be run while an application is open" (`M/lib/toolbox/cli/shell/cli_shell.c#L258-L265`, `subghz_cli.c#L1186`, `lfrfid_cli.c#L569`). Zolang de FAP draait, kunnen die CLI-acties (USB) dus niet. `nfc` is parallel-safe.

## 3. Kan een draaiende FAP de Pi iets vragen? Ja: DataExchange

`DataExchangeRequest { bytes data = 1; }` zit in protobuf 0.25 (`P/application.proto#L55-L57`, `flipper.proto#L113`, tag 65). Het werkt in **beide richtingen** en alle functies zijn geëxporteerd (`api_symbols.csv#L3304-L3311`):

- **Koppelen:** de Pi stuurt `StartRequest{name="/ext/apps/.../pitoolbox.fap", args="RPC"}`. De FAP parset `sscanf(args,"RPC %lX",&ptr)`, roept `rpc_system_app_set_callback` aan en daarna `rpc_system_app_send_started`. De Pi krijgt dan een ongevraagde `app_state_response=APP_STARTED`. Referentie-implementatie: `M/applications/debug/rpc_debug_app/rpc_debug_app.c#L45-L84` (en ook subghz/nfc/infrared).
- **FAP → Pi:** `rpc_system_app_exchange_data(rpc, buf, len)` stuurt een `PB_Main` met `command_id = 0` (de heap wordt op nul gezet, `M/furi/core/memmgr_heap.c#L467`), status OK en `has_next = false` (`rpc_app.c#L420-L439`). Onze `RpcClient` zet command_id 0 al als event in de wachtrij (`client.py`, docstring r. 8). Dit is "fire-and-forget": de Pi geeft geen ack.
- **Pi → FAP:** `app_data_exchange_request` met eigen command_id → `RpcAppEventTypeDataExchange` (bytes) in de callback (`rpc_app.c#L316-L349`). De FAP **moet** `rpc_system_app_confirm(rpc,true/false)` aanroepen, en pas daarna krijgt de Pi `OK`/`ERROR_APP_CMD_ERROR` (`#L361-L389`). App-commando's lopen strikt na elkaar: zonder confirm krijgt het vorige commando nooit antwoord. Een dubbele confirm geeft een `furi_check`-crash.
- **Sessie weg** (BLE valt weg): `rpc_system_app_free` stuurt `RpcAppEventTypeSessionClose` en **blijft hangen tot de FAP zijn callback op NULL zet** (`rpc_app.c#L483-L506`). Een nieuwe sessie kan niet opnieuw aan de draaiende FAP koppelen: `app_exit_request` op de nieuwe sessie geeft `ERROR_APP_NOT_RUNNING` en `StartRequest` geeft `SYSTEM_LOCKED`. De FAP moet bij SessionClose dus zichzelf afsluiten of naar een "offline"-scherm gaan (zoals `subghz.c#L33-L43`).
- **Een FAP die de gebruiker zelf start** (zonder "RPC"-arg) heeft geen `RpcAppSystem*` en dus geen kanaal. De Pi kan alleen pollen, bijvoorbeeld een bestand in `/ext/apps_data/...` via storage-RPC (traag over BLE: 1,2 KiB/s), of het scherm lezen via de GUI-stream. Dat is een noodoplossing.
- **Beveiliging, verschil met OFW:** in mntm-012 kan elke RPC-client `args="RPC <willekeurige hex>"` meegeven. Dat gaat ongecontroleerd door en de FAP zou het als pointer gebruiken. OFW-release weigert zulke vervalste args en houdt een lijst van geldige instanties bij (`O/applications/services/rpc/rpc_app.c#L26-L84`, `#L144-L157`). Gevolg: de FAP vertrouwt de pointer alleen als de Pi hem startte. Zelf valideren kan niet, het risico is beperkt tot partijen die al RPC-toegang hebben.
- Andere kanalen: GUI/desktop-proto heeft geen "app → host"-berichten. `loader_signal` en de pubsub zijn alleen lokaal. Bestanden in /ext werken, maar dan moet de Pi pollen.

## 4. Handoff-ontwerp en advies

| | A: FAP sluit → Pi doet actie → Pi herstart FAP met resultaat als arg | B: FAP blijft draaien, DataExchange |
|---|---|---|
| Robuustheid | Elke stap kan falen: de herstart geeft SYSTEM_LOCKED als de gebruiker intussen iets opent, er is een API-dialoogrisico en state gaat verloren | Eén sessie. Bij een BLE-drop moet de FAP opnieuw gestart worden (SessionClose) |
| Beveiliging | args ≤ ~512 B, plat. De Pi bepaalt alles | Gelijk: elk verzoek is een frontend-call `flipper_ui` via `ToolRunner`. Transmit geeft een `pending_id`, en de bevestiging is een **aparte** expliciete knop in de FAP (eigen DataExchange-bericht), gebonden aan frontend `flipper_ui` met TTL 60 s |
| UX | App knippert of sluit bij elke actie, en over BLE kost dat seconden | Vloeiend, met voortgang en het resultaat live |
| Complexiteit | Eenvoudige FAP, de Pi beheert de app-levenscyclus | Iets meer C (callback, confirm, mutex rond `rpc`-pointer); een klein berichtprotocol |
| CLI-acties (Sub-GHz/RFID, USB) | Werkt, want de loader is vrij | FAP moet tijdelijk sluiten (loader_lock) |

**Advies: B (DataExchange) als hoofdroute, met A alleen als fallback** voor CLI-acties die een vrije loader nodig hebben. De FAP vraagt dan "ik sluit even", de Pi voert de actie uit en start de FAP opnieuw met `args="RPC"`. Het resultaat volgt via DataExchange en niet via args, want args en RPC-koppeling sluiten elkaar uit.

Protocol (voorstel): CBOR/JSON-lines van ≤ 512 B per bericht met `{v, id, type: "call"|"confirm"|"cancel"|"result"|"progress"|"error", tool, args}`. De FAP stuurt `call`, en de Pi antwoordt met `result` of `confirmation_required{pending_id, summary}`. Alleen een expliciete druk op OK in de FAP stuurt `confirm{pending_id}`. De Pi controleert met `PendingActions.take(pending_id, "flipper_ui")`, dus met scope-check, TTL en eenmaligheid zoals nu. De FAP kent geen geheimen en neemt zelf geen beslissingen.

## Open vragen

1. Komen Flipper-geïnitieerde DataExchange-frames over BLE betrouwbaar door als de Pi tegelijk een eigen request open heeft (interleaving met command_id 0)?
2. Werkelijke limiet van `StartRequest.args` en `DataExchange.data` (nanopb FT_POINTER, heap)?
3. Gedrag bij vergrendelde desktop: willen we `allow_locked_rpc_ble` aanzetten (veiligheid: dan werkt BLE-RPC ook op een vergrendelde Flipper)?
4. Wie start de FAP? Automatisch bij elke BLE-connect als de loader vrij is (ongevraagd scherm), of handmatig via een Pi-tool `flipper_ui_start`?
5. Moet de FAP bij SessionClose sluiten, of een offline-scherm tonen en de gebruiker vragen hem opnieuw te laten starten?

## Hardwaretestplan (Stijns Flipper, mntm-012)

Doe dit pas als de Flipper vrij is. Gebruik eerst de ingebouwde **RPC Debug**-app als stand-in, dan is er nog geen eigen FAP nodig. Zoek eerst het exacte pad op met `storage list /ext/apps/Debug`.

1. **Lock:** open een willekeurige app met de hand → `app_lock_status_request` (verwacht `locked=true`) → `StartRequest{name:"/ext/apps/Debug/rpc_debug.fap"}`. Verwacht `ERROR_APP_SYSTEM_LOCKED` (17).
2. **Start op pad + RPC-koppeling (USB en BLE):** `StartRequest{name:<pad>, args:"RPC"}`. Verwacht OK plus een ongevraagde `app_state_response APP_STARTED`.
3. **Pi → FAP:** `app_data_exchange_request{data:01 02 03}`. De hex moet op het scherm verschijnen. Antwoord pas na de confirm in de receive-scene.
4. **FAP → Pi:** voer in de app "Input data exchange" bytes in. De Pi moet een frame met `command_id=0` en `app_data_exchange_request` ontvangen. Herhaal dit terwijl de Pi een lange `storage list` doet (open vraag 1).
5. **Sessieverlies:** verbreek BLE terwijl de app draait. Verwacht dat de app stopt (SessionClose). Verbind opnieuw → `StartRequest` moet weer OK geven.
6. **Args-limiet:** start een eenvoudige test-FAP (of `rpc_debug` zonder "RPC") met args van 255, 511, 513 en 2000 B. Noteer OK, foutstatus of crash.
7. **Lock + BLE:** vergrendel de desktop. Controleer dat de BLE-RPC-sessie sluit en na ontgrendelen terugkomt.
8. **Niet testen** (bekende crash): de API-mismatch-dialoog via RPC wegklikken.

## Resultaten hardwaretest (2026-10-16)

Uitgevoerd door Claude Code op de Pi 4, met Stijns Flipper `A1tkethe` (Momentum mntm-012, API 87.1, protobuf 0.25). USB via `/dev/ttyACM0`, BLE via `hci1` naar `80:E1:26:20:F6:6B`. Ik heb alleen de eigen `RpcClient`, `UsbSerialTransport` en `BleTransport` gebruikt, vanuit losse scripts buiten de repo. Er is niets uitgezonden: geen Sub-GHz, IR, NFC/RFID-emulatie of BadUSB. Test 8 is bewust overgeslagen.

**Protobuf:** `DataExchangeRequest` (`app_data_exchange_request`), `LockStatusRequest/Response` (`app_lock_status_request/response`), `AppStateResponse`, `gui_send_input_event_request` en `desktop_is_locked_request`/`desktop_unlock_request` zitten allemaal al in de gegenereerde `pb`. Er hoeft niets opnieuw gegenereerd te worden. Een `desktop_lock_request` bestaat niet in protobuf 0.25.

### Voorbereiding: RPC Debug staat niet op de Flipper

`/ext/apps/Debug` bestaat niet, en nergens op de SD-kaart staat `rpc_debug`. RPC Debug is in de firmware een `FlipperAppType.DEBUG`-app en zit dus niet in de release-SD-pakketten. Daarom heb ik hem zelf gebouwd met ufbt en de officiële mntm-012-SDK (`up.momentum-fw.dev`, channel release, `Version 87.1`). De bron is ongewijzigd, `applications/debug/rpc_debug_app` @ mntm-012. Alleen in `application.fam` staat `EXTERNAL` in plaats van `DEBUG`, anders bouwt ufbt hem niet. APPCHK gaf `Target: 7, API: 87.1`, dus geen API-mismatchdialoog (test 8-risico). Daarnaast heb ik twee kleine eigen test-FAP's gebouwd tegen dezelfde SDK:

- **`pitb_argprobe.fap`** toont `strlen(args)`, een checksum en de eerste en laatste 8 tekens van `args`. Bedoeld voor test 1 en 6.
- **`pitb_dxprobe.fap`** is een minimale DataExchange-FAP, ongeveer wat onze eigen FAP straks moet doen. `E`+data wordt bevestigd en als `e`+data teruggestuurd. `G`+n+ms laat de FAP zelf n berichten (`T`+seq+tick) sturen, zonder dat de Pi iets doet. Zo kon ik FAP→Pi zonder menselijke knopdruk testen.

Alle drie staan nu op de Flipper in `/ext/apps/Debug/` (md5 gecontroleerd). Ik heb ze laten staan, zodat test 6 en 7 later meteen kunnen. Weghalen kan met de storage-RPC of qFlipper.

"Knoppen indrukken" deed ik met `gui_send_input_event_request` (PRESS, SHORT, RELEASE). Let op: alleen SHORT sturen doet niets. De view_dispatcher negeert een SHORT zonder voorafgaande PRESS. Schermcontrole ging via één frame uit de screen stream, omgezet naar PNG.

### Test 1: lock (USB)

Stand-in voor "met de hand een app openen": `StartRequest{name:"/ext/apps/Debug/pitb_argprobe.fap", args:"hand"}` zonder RPC-koppeling. De app krijgt `len=4 head=hand`, dus args komen intact aan.

| Stap | Resultaat |
|---|---|
| `app_lock_status` vooraf | `locked=false` |
| `app_lock_status` met argprobe open | `locked=true` (2,7 ms) |
| `StartRequest` rpc_debug.fap (`args:"RPC"`) | `ERROR_APP_SYSTEM_LOCKED` (17), 3 ms |
| `StartRequest` `Infrared` (interne app) | ook `ERROR_APP_SYSTEM_LOCKED` |
| `app_exit_request` op dezelfde sessie | `ERROR_APP_NOT_RUNNING`: de Pi kan een app die niet met "RPC" gestart is niet via `app_exit` sluiten |
| GUI-input Back | app sluit, daarna `locked=false` |

**Oordeel: zoals verwacht.** Wel een kanttekening: de Pi kan een handmatig geopende app dus wel sluiten door Back te "drukken". Dat moeten we in de FAP-tools niet aanbieden, want het grijpt in op wat de gebruiker aan het doen is. Over BLE heb ik test 1 niet herhaald, omdat de loader-check niet van het transport afhangt.

### Test 2: start op pad + RPC-koppeling (USB en BLE)

`StartRequest{name:"/ext/apps/Debug/rpc_debug.fap", args:"RPC"}`.

| | USB | BLE |
|---|---|---|
| Antwoord | OK in 63 tot 76 ms | OK in 146 ms |
| Ongevraagde `app_state_response` | `APP_STARTED`, `command_id=0`, 2 ms na de OK | `APP_STARTED`, `command_id=0`, 98 ms na de OK (244 ms na versturen) |
| `app_exit_request` | OK in 5 ms, daarna `APP_CLOSED` | OK in 90 ms, daarna `APP_CLOSED` |

**Oordeel: zoals verwacht**, op beide transports. De `RpcClient` zet het `APP_STARTED`-frame netjes in de eventqueue. Kleine verrassing: direct na `APP_CLOSED` (7 ms later, USB) gaf `app_lock_status` nog één keer `locked=true`. Na 0,5 s was hij altijd `false`. De Pi moet dus even wachten of opnieuw proberen voordat hij na een exit iets nieuws start.

### Test 3: Pi → FAP (USB en BLE)

In RPC Debug naar Test Data Exchange → Receive Data, dan `app_data_exchange_request{data:01 02 03}`.

- **Scherm:** toont `01 02 03`, op USB en op BLE.
- **Antwoord:** OK pas na de confirm in de receive-scene. USB 3,0 ms, BLE 87 ms.
- **Latency 4 B (10× met 0,5 s pauze):** USB 2,7 tot 4,6 ms. BLE 84 tot 86 ms, dus iets onder de ping (94 tot 98 ms). Met dxprobe (confirm direct in de callback): USB mediaan 7 ms, BLE mediaan 97 ms (87 tot 100).
- **Grootte:** 64, 243, 512 en 1024 B gaven over USB allemaal OK (7, 9, 13 en 21 ms). Over BLE gaven 64, 243 en 512 B OK (97, 98 en 194 ms). Daarna werkte de sessie gewoon door (ping OK). 2000 B heb ik niet geprobeerd.
- **Ongeldig commando** (dxprobe, `X`): `ERROR_APP_CMD_ERROR`, zoals verwacht bij `confirm(false)`.

Twee verrassingen die er voor het ontwerp toe doen:

1. **DataExchange in de verkeerde scène krijgt nooit antwoord.** Stuurt de Pi DataExchange terwijl RPC Debug in het startmenu staat, dan wordt het custom event niet afgehandeld en volgt er geen confirm. De Pi krijgt een time-out (3 s, op USB en BLE). Het volgende app-commando overschrijft `last_command_id` gewoon, want `furi_assert` doet niets in een release-build. Er volgt dus geen crash, maar het eerste commando blijft voor altijd open. Onze FAP moet daarom **elke** DataExchange direct in de callback bevestigen, in elke toestand, en pas daarna de inhoud verwerken.
2. **Een notificatie in het RPC-pad vertraagt alles.** Tien verzoeken direct achter elkaar: de eerste vijf kwamen binnen 3 tot 6 ms terug, daarna steeds ongeveer 200 ms (USB én BLE). RPC Debug roept per bericht `sequence_blink_cyan_100` aan. Zodra de notificatiewachtrij vol zit, blokkeert de app-thread. Met 0,5 s pauze trad het niet op. In onze FAP dus geen blink of backlight in het pad van een bericht, of hooguit één per seconde.

### Test 4: FAP → Pi (USB en BLE)

Er is geen mens nodig geweest. In RPC Debug ging ik via GUI-input naar Send Data, typte `A5` en koos Save.

- **Frame:** `command_id=0`, status OK, `has_next=false`, `app_data_exchange_request` met **64 bytes** (`a5 00 00 …`). RPC Debug stuurt altijd zijn hele `DATA_STORE_SIZE`. Ruwe frame: `45 8a 04 42 0a 40 a5 00 00 …` (lengte 69, veld 65, `data` 64 B). `command_id` en `command_status` staan er niet in, want ze zijn 0. De `RpcClient` zet het frame in de eventqueue en `wait_event` vindt het.
- **Latency vanaf de knopdruk (SHORT verstuurd):** USB 20 tot 22 ms (10×). BLE 196 tot 198 ms (8×). Over BLE komt de ack van de SHORT na 97 ms en het DataExchange-frame precies één indicatie later. Op een rustige link duurt FAP→Pi dus ongeveer 100 tot 150 ms enkele reis.
- **Interleaving met storage list (open vraag 1):** drie keer een knopdruk direct gevolgd door `storage_list` van `/ext/apps/Tools` (30 items, 4 frames) en `/ext/apps/Games` (41 items, 6 frames). Op USB en BLE kwam in alle gevallen het DataExchange-frame binnen terwijl `request()` nog op de lijst wachtte. De lijst was compleet, en er ging niets verloren of door elkaar.

Met dxprobe heb ik dit verder uitgediept, omdat de FAP daar zelf het moment van verzenden kiest:

| Scenario | USB | BLE |
|---|---|---|
| Echo Pi→FAP→Pi (20×, 16 B) | mediaan 10 ms, 0 fout | mediaan 195 ms (184 tot 207), 0 fout |
| FAP stuurt 40× per 250 ms, link verder stil | 40/40, op volgorde, jitter < 4 ms | 40/40, op volgorde, jitter −39 tot +8 ms |
| 40× per 100 ms | | 40/40, vertraging 148 tot 196 ms |
| 30× per 50 ms | | 30/30, maar achterstand loopt op tot 1,6 s |
| 60× per 200 ms tijdens `storage_read` van 12 KiB | 60/60 | 60/60, waarvan 23 midden in de read binnenkwamen; md5 van de read klopt; read duurde 11,6 s in plaats van 9,3 s; vertraging 3,3 tot 11,5 s |
| FAP 40× per 100 ms terwijl de Pi echo's stuurt | 489/489 echo's, 40/40 | 21/21 echo's, 40/40, vertraging tot 4,1 s |

**Oordeel: DataExchange werkt in beide richtingen betrouwbaar.** Over USB en BLE ging niets verloren, kwam niets dubbel en klopte de volgorde. Frames met `command_id=0` vlechten zich over BLE ook midden in een antwoord van meerdere frames, en de `RpcClient` sorteert ze goed. Daarmee is open vraag 1 beantwoord.

Wat wel opvalt:

- **De bandbreedte Flipper→Pi over BLE is klein:** ongeveer 10 frames per seconde, ongeacht de grootte. Gaat de FAP sneller, dan loopt er een achterstand op. Die gaat niet verloren, de FAP-thread blokkeert gewoon in `rpc_system_app_exchange_data`. Een lange read of list van de Pi verdringt FAP-berichten secondenlang.
- **Head-of-line blocking aan de Flipper-kant:** een knopdruk die de Pi 3 s na de start van een `storage_read` stuurt, werd pas na de hele read verwerkt (9,4 s). De RPC-worker doet één commando tegelijk. Pi→FAP-berichten wachten dus ook op een lopende storage-operatie.
- **De eventqueue van `RpcClient` laat stilletjes frames vallen.** Hij heeft `maxlen=256`. Bij de eerste USB-run met dxprobe "verdween" de helft van de echo's, omdat ik de queue niet leegde. Het lag dus niet aan de Flipper. Voor fase 4 betekent dit dat de Pi een eigen lezer/dispatcher nodig heeft die DataExchange-frames meteen weghaalt. Of de cap moet hoger, met een waarschuwing bij overloop.

Optioneel nog met de hand: een **fysieke** knopdruk in RPC Debug (Send Data → Save) tijdens een lange `storage_read` over BLE. Het effect verwacht ik gelijk aan dxprobe (frames midden in de read), maar het zou de laatste stap zijn.

### Test 5: sessieverlies (BLE en USB): **niet goed, Flipper is gecrasht en daarna vastgelopen**

| Run | Wat ik deed | Resultaat |
|---|---|---|
| BLE, rpc_debug | app gestart met "RPC", na 1 s `BleTransport.close()`, 2 s wachten, opnieuw verbinden | na reconnect `locked=false`, `app_exit` → `ERROR_APP_NOT_RUNNING`, `StartRequest` weer OK + `APP_STARTED`. **Maar:** op hetzelfde moment heeft de Pi de USB-Flipper opnieuw zien enumereren (dmesg 5325,3 s), dus de Flipper is herstart |
| BLE, dxprobe (ticker liep) | idem | idem, en opnieuw een USB-herenumeratie (5345,3 s) tijdens het verbreken |
| USB, rpc_debug | app gestart, seriële poort dicht zonder `stop_session` | **reboot** 0,4 s na het sluiten (USB weg, 2,2 s later terug; CLI `uptime` gaf daarna 28 s, dat bevestigt de reboot). Daarna `system_info` OK |
| BLE, rpc_debug, met `log debug` via de USB-CLI ernaast | idem als de eerste run | **netjes:** `Disconnect … Reason: 13` → `Close RPC connection` → `Session terminated` → `SendExit` → `App returned: 0` → `Application stopped`, geen reboot |

Zo'n 15 tot 25 s na die laatste run is de Flipper **volledig vastgelopen**. USB-writes geven een time-out (de poort is er nog, maar de Flipper leest niets), BLE adverteert niet meer (ook niet zichtbaar in `bluetoothctl scan le`) en hij is na meer dan 8 minuten niet vanzelf herstart. Een USB-reset vanaf de Pi kan niet zonder root. Daarom ben ik hier gestopt.

**Oordeel:** de gewenste uitkomst (de app stopt, reconnect geeft weer OK) is gehaald. Maar in 3 van de 4 runs ging dat via een reboot en niet via een nette SessionClose. In de vierde run ging het eerst goed, waarna de Flipper vastliep. Dat de oorzaak in de SessionClose-afhandeling zit, is een **aanname**: de reboots vallen in tijd samen met het verbreken, maar een crashlog heb ik niet. Het kan ook aan de test-FAP's liggen. De officiële `rpc_debug_app_free` ruimt bijvoorbeeld `view_dispatcher` en `scene_manager` op met `free()` in plaats van met `*_free()`, en dat is als interne app nooit een probleem geweest. Het vastlopen kan ook komen doordat ik de `log debug`-sessie op USB met Ctrl+C en close heb afgebroken. Uitzoeken voordat we de FAP bouwen; zie "Handmatig nog te doen".

### Test 6: args-limiet: **nog te doen**

Niet uitgevoerd, omdat de Flipper na test 5 vastzat. `pitb_argprobe.fap` staat klaar en laat precies zien wat er aankomt (lengte, checksum, begin en eind).

### Test 7: lock + BLE: **nog te doen**

Niet uitgevoerd, om dezelfde reden. Wat ik wel weet: `desktop_is_locked_request` geeft op een ontgrendelde Flipper `ERROR` en geen `OK` met een vlag. Bij Momentum is de status dus het antwoord zelf. Een `desktop_lock_request` bestaat niet. Vergrendelen moet dus met de hand, of via GUI-input in het lock-menu op de desktop.

### Handmatig nog te doen (Stijn)

1. **Flipper resetten:** houd **Links + Back** ongeveer 5 s ingedrukt. Schrijf eerst op wat er op het scherm staat. Staat er een crashmelding ("furi_check failed", "System halted" of iets dergelijks), dan is dat de belangrijkste informatie uit deze test. Controleer daarna met `pitoolbox-flipper` (system_info) dat hij er weer is, op het bureaublad zonder open app.
2. **Test 5 opnieuw, met crashinformatie.** Koppel de Flipper aan USB en open in een terminal `log debug` (bijv. `picocom /dev/ttyACM0`, typ `log debug`). Laat die open, dus niet afbreken. Start daarna over BLE `rpc_debug.fap` met `args:"RPC"` (bijv. `scratchpad/t5.py ble rpc_debug`) en verbreek BLE. Noteer de laatste logregels bij een reboot. Herhaal met `pitb_dxprobe.fap`. Zo wordt duidelijk of de crash in de firmware zit (SessionClose) of in de test-FAP's.
3. **Test 6:** over USB `StartRequest{name:"/ext/apps/Debug/pitb_argprobe.fap", args:<n×'a'>}` voor n = 255, 511 en 513, en pas als laatste 2000. Lees per keer het scherm af (`len=`). Sluit met Back. Bij een crash: noteren, wachten tot hij terug is, `system_info`, en stoppen met deze test.
4. **Test 7:** verbind over BLE (bijv. `pitoolbox-flipper` met `--transport ble`, of een ping-loop). Druk op de Flipper op het bureaublad **Omhoog** → **Lock**. Verwacht: de BLE-RPC-sessie sluit (de ping faalt, of de flow-control valt weg), omdat `allow_locked_rpc_ble` standaard uit staat. Ontgrendel (3× Back) en controleer dat een nieuwe BLE-verbinding weer RPC geeft.

De scripts staan alleen in de scratchpad van deze sessie en niet in de repo (`fl.py`, `t1_lock.py`, `t234.py`, `t4y.py`, `t4z.py`, `t5.py`, `t5log.py`).

### Bijgestelde aanbeveling

Ik blijf bij **B (DataExchange) als hoofdroute.** Over BLE werkt het betrouwbaar: geen verlies, de volgorde klopt, het werkt in beide richtingen tegelijk en ook terwijl er een lang antwoord loopt. De gemeten latency over BLE (Pi 4, verbindingsinterval 48 ms volgens de Flipper-log):

| Richting | Gemeten |
|---|---|
| Pi → FAP, tot de OK na de confirm | 84 tot 100 ms (zoals een ping) |
| FAP → Pi, enkele reis op een rustige link | ongeveer 100 tot 150 ms (frame komt één indicatie na de ack, ~196 ms na een knopdruk vanaf de Pi) |
| Pi → FAP → Pi echo | mediaan 195 ms |
| Ter vergelijking over USB | 3 tot 7 ms, 20 ms en 10 ms |

Voor de bouw volgt hieruit:

- **Confirm direct in de RPC-callback,** in elke toestand van de FAP, en daarna pas verwerken. Anders blijft een Pi-verzoek eeuwig open (test 3).
- **Geen blokkerende notificaties in het berichtpad** (test 3), en de FAP stuurt **hooguit ~5 berichten per seconde.** Voortgang bundelen tot ≤ 512 B per bericht. Boven ~10 frames per seconde loopt er een achterstand op.
- **Geen lange storage-operaties terwijl de FAP actief is.** Of ze opknippen, want ze blokkeren beide richtingen secondenlang.
- **Pi-kant:** een eigen lezer die `command_id=0`-frames meteen afhandelt. De huidige eventqueue (`maxlen=256`) gooit bij overloop stilletjes frames weg.
- **SessionClose is het zwakke punt.** Zolang test 5 niet opnieuw gedaan is met een crashlog, is het risico dat een BLE-drop met een draaiende FAP de Flipper herstart of laat vastlopen. Voor de FAP betekent dit: de SessionClose-afhandeling minimaal houden (callback op NULL, pointer weg, niets meer versturen) en geen mutex vasthouden tijdens `rpc_system_app_exchange_data`. Dat laatste deed dxprobe wel, dus dat is een mogelijke oorzaak voor die run. Ook na een herstart moet de Pi gewoon opnieuw kunnen verbinden. Dat werkte in alle runs: reconnect in 1,6 s en `StartRequest` weer OK.
