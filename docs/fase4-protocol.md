# Fase 4: protocol tussen de Flipper-app en de Pi

Versie 1. Dit document is de spec voor:

- de codec op de Pi: [`src/pitoolbox/frontends/flipper_ui/protocol.py`](../src/pitoolbox/frontends/flipper_ui/protocol.py) (tests: `tests/test_flipper_ui_protocol.py`);
- de FAP: [`flipper_app/pitoolbox_ui/`](../flipper_app/pitoolbox_ui/) (codec: `ptb_proto.c`).

Achtergrond en metingen staan in [fase4-spike.md](fase4-spike.md). Bronverwijzingen naar de firmware gaan over Momentum **mntm-012** (`M` = `https://github.com/Next-Flip/Momentum-Firmware/blob/mntm-012/`), API 87.1.

## 1. Uitgangspunten

- **Kanaal:** de FAP draait met een RPC-koppeling (`StartRequest{args:"RPC"}`), en alle berichten gaan via `app_data_exchange_request` (DataExchange). Pi → FAP is een gewone RPC-opdracht met een `command_id`; de FAP bevestigt hem. FAP → Pi is een ongevraagd frame met `command_id = 0` (spike §3).
- **De FAP is een onbetrouwbaar invoerapparaat.** Hij heeft geen geheimen en neemt geen beslissingen. Hij stuurt alleen ids: een snelle actie, een preset of een bevestigings-handle. Toolnamen, argumenten en vrije tekst stuurt hij nooit. Wat een id betekent, bepaalt de Pi (vaste tabel of Pi-config). Alles wat binnenkomt wordt gevalideerd voordat de bridge iets doet.
- **Elke actie gaat via de `ToolRunner`** met frontend-id `flipper-ui`. Validatie, policy, scope en audit blijven dus precies zoals bij MCP.
- **Klein en voorspelbaar:** elk bericht is hoogstens 512 bytes, er gaan hoogstens 5 berichten per seconde per richting, en de Pi stuurt naar de FAP met stop-and-wait.

## 2. Codering: binair, vaste velden

Elk bericht heeft een kop van 5 bytes plus een vaste body per type. Getallen zijn little-endian. Tekst is een `u8`-lengte gevolgd door printbare ASCII (0x20–0x7E). Alleen de samenvatting en het resultaat mogen ook `\n` bevatten.

| Offset | Grootte | Veld |
|---|---|---|
| 0 | 1 | magic `0x50` (`'P'`) |
| 1 | 1 | protocolversie (`1`) |
| 2 | 1 | type |
| 3 | 2 | `seq` (u16, per afzender, 1..65535, 0 komt niet voor) |

**Waarom geen JSON?** Qua grootte zou het passen, ook een samenvatting van 200 tekens. Maar API 87.1 exporteert geen JSON-parser voor FAP's (`targets/f7/api_symbols.csv`: wel nanopb, geen JSON). We zouden dus een eigen JSON-parser in C moeten schrijven, en dat is aanvalsoppervlak aan de kant die het minst te testen is. Escapes en unicode moeten daarbij ook nog goed. **Waarom geen protobuf (nanopb)?** Dan is er aan beide kanten een generator nodig, plus `.options` voor de maximale groottes. Dat geeft meer code in de FAP dan dit hele protocol. De vaste layout hieronder is aan beide kanten in ongeveer 150 regels volledig te valideren. De C-decoder is op de host gekruiscontroleerd met de Python-codec: 14.200 geldige, gemuteerde en willekeurige berichten, 0 verschillen in accepteren of weigeren, en dezelfde velden.

Strikt decoderen betekent aan beide kanten hetzelfde: verkeerde magic of versie, seq 0, een onbekend type, een type uit de verkeerde richting, te kort, bytes over na de body, een waarde buiten bereik, onbekende vlagbits of tekst buiten printbare ASCII geeft altijd "ongeldig". Er zijn geen standaardwaarden en geen stille correcties.

**Regels op het scherm.** De FAP toont het resultaat in een TextBox van de firmware. Die breekt een regel af zodra het volgende teken niet meer in 120 pixels past, ook midden in een woord (`text_box_seek_next_line` in Momentum mntm-012). Daarom breekt de Pi de tekst zelf af vóór het opsplitsen in delen (`to_text_box` in `protocol_text.py`): op spaties, en alleen in een woord dat breder is dan een hele regel na `/`, `-` of `_`, of anders midden in het woord. De breedte per teken komt uit het font van de firmware (`_font_widths.py`, gegenereerd door `scripts/gen_flipper_font_widths.py`). Een Momentum-assetpack met een eigen font maakt die breedtes onjuist; dan breekt de TextBox weer zelf af.

PROGRESS en ERROR mogen sinds 2026-10-17 ook `\n` bevatten (decoder Pi en FAP; een oudere FAP weigert ze, dus Pi en app samen bijwerken met `ui-install`). De FAP toont ze in dezelfde TextBox: PROGRESS op een eigen regel onder "Bezig...", ERROR als `<titel>: <tekst>` (`ptb_error_title` in `pitoolbox_ui.c`; de Pi kent dezelfde titels, `_ERROR_TITLES` in `bridge_text.py`, en een test vergelijkt ze met de C-bron). De Pi breekt ze net als RESULT af op woordgrenzen, met de titel als deel van de eerste regel. Tekst die de FAP zelf samenstelt (het statusscherm) breekt de FAP zelf af.

## 3. Berichten

`ref` verwijst naar de `seq` van het FAP-bericht waar het antwoord bij hoort. `ref = 0` betekent "geen antwoord" (de Pi begon zelf).

### FAP → Pi

| Type | Naam | Body | Regels |
|---|---|---|---|
| `0x01` | HELLO | `app_major u8, app_minor u8, proto_max u8, instance u32, max_rx u16` | `proto_max ≥ 1`, `instance ≠ 0` (willekeurig per start), `max_rx` 64..512 |
| `0x02` | STATUS_REQUEST | leeg | |
| `0x03` | PRESETS_REQUEST | leeg | |
| `0x04` | ACTION | `kind u8, action_id u8` | `kind` 1 = snelle actie, 2 = preset; `action_id` 1..255 |
| `0x05` | CONFIRM_ANSWER | `handle u32, answer u8` | `handle ≠ 0`, `answer` 0 = nee, 1 = ja |
| `0x06` | CANCEL | `ref u16` | `ref ≠ 0`: breek de actie met die seq af |

### Pi → FAP

| Type | Naam | Body | Regels |
|---|---|---|---|
| `0x81` | WELCOME | `proto u8, instance u32, flags u8` | `instance` = die uit HELLO; bit0 = bevestigen op de Flipper aan, bit1 = agent beschikbaar |
| `0x82` | STATUS | `flags u8, link u8, captures u8, pending u8, last_result u8, last_age_s u16, uplink str8, last_tool str8` | flags: bit0 uplink up, bit1 onderspanning nu, bit2 onderspanning eerder, bit3 voeding onbekend. `link` 0 = ?, 1 = USB, 2 = BLE. `last_result` 0 = geen, 1 = OK, 2 = fout. `last_age_s` 0xFFFF = onbekend. `uplink` ≤ 32, `last_tool` ≤ 24 |
| `0x83` | PRESETS | `count u8` en dan `count ×(id u8, label str8)` | `count` ≤ 12, ids 1..255 en uniek, label 1..24 tekens. Alleen het label gaat naar de FAP, de prompt blijft op de Pi |
| `0x84` | CONFIRM_PROMPT | `handle u32, ttl_s u8, summary str8` | `handle ≠ 0`, `ttl_s` 1..120, samenvatting 1..200 (mag `\n` bevatten) |
| `0x85` | CONFIRM_CLOSED | `handle u32, reason u8` | 1 uitgevoerd, 2 geweigerd, 3 verlopen, 4 ingetrokken, 5 mislukt |
| `0x86` | PROGRESS | `ref u16, text str8` | tekst ≤ 64, `\n` toegestaan; vervangt de vorige |
| `0x87` | RESULT | `ref u16, part u8, total u8, text…` | `total` 1..8, `part < total`, tekst = rest van het bericht, ≤ 500 bytes, mag `\n` bevatten |
| `0x88` | ERROR | `ref u16, code u8, text str8` | code 1 bezig, 2 rate limit, 3 onbekende actie, 4 ongeldig bericht, 5 versie, 6 tool faalde, 7 geen agent, 8 afgebroken, 9 time-out, 10 bevestiging ongeldig, 11 nog geen HELLO; tekst ≤ 120, `\n` toegestaan |

### Grenzen

| Constante | Waarde | Waarom |
|---|---|---|
| `MAX_MSG` | 512 B | over BLE gemeten: 512 B OK (spike, test 3) |
| `MAX_CHUNK` / `MAX_PARTS` | 500 B / 8 | 5 + 4 + 500 = 509 B; een antwoord is hoogstens 4000 B, en dat is ook de buffer in de FAP |
| `MAX_SUMMARY` | 200 | ongeveer 8 regels op het scherm, met scrollen |
| `MAX_PRESETS` / label | 12 / 24 | het grootste PRESETS-bericht is 318 B |
| `MAX_TTL_S` | 120 | past in u8; de Pi-TTL voor bevestigingen is standaard 60 s |
| rate | 5/s per richting, burst 5 | spike: boven ~10 frames/s loopt BLE achter |

### Vaste vectoren

Deze bytes staan ook in de tests (`test_known_vectors`) en zijn de referentie voor `ptb_proto.c`:

```
HELLO          seq=1 v1.0 instance=0x11223344 max_rx=512   50 01 01 01 00 01 00 01 44 33 22 11 00 02
ACTION         seq=0x0102 quick id=4 (noodstop)            50 01 04 02 01 01 04
CONFIRM_ANSWER seq=3 handle=0xA1B2C3D4 ja                  50 01 05 03 00 d4 c3 b2 a1 01
CONFIRM_PROMPT seq=2 handle=0xA1B2C3D4 ttl=60 "IR tv"      50 01 84 02 00 d4 c3 b2 a1 3c 05 49 52 20 74 76
RESULT         seq=5 ref=7 part=1 total=3 "ok"             50 01 87 05 00 07 00 01 03 6f 6b
WELCOME        seq=1 v1 instance=0x11223344 bevestigen aan 50 01 81 01 00 01 44 33 22 11 01
```

## 4. Volgorde, duplicaten, verlies

- **Pi → FAP: stop-and-wait.** App-opdrachten lopen op de Flipper strikt na elkaar (`rpc_app.c`, `last_command_id`). De Pi stuurt het volgende bericht pas als het vorige een antwoord heeft. `OK` betekent dat het bericht in de wachtrij van de FAP staat. `ERROR_APP_CMD_ERROR` betekent dat de FAP het bericht heeft geweigerd (ongeldig of wachtrij vol): de Pi probeert het na 200 ms opnieuw, hoogstens 3 keer, en laat het daarna vallen. Komt er binnen 3 s geen antwoord, dan geldt de FAP als hangend: niet meer sturen en de sessie als dood behandelen. De Pi sluit dan zijn eigen app (`app_exit`) en start hem daarna opnieuw; de verbinding met de Flipper blijft staan.
- **FAP → Pi: fire-and-forget.** De spike mat geen verlies en geen verkeerde volgorde. Toch heeft elke kant per afzender een `seq` en een `SeqTracker` (serieel rekenen, RFC 1982, modulo 65535). Een bericht dat niet nieuwer is dan het vorige, is een duplicaat en wordt weggegooid. Een gat wordt gelogd. Na een nieuwe HELLO (andere `instance`) begint alles opnieuw.
- **Antwoorden in delen:** de Pi stuurt de delen op volgorde. De FAP accepteert alleen `part == verwacht` en dezelfde `total`. Anders toont hij "Antwoord onvolledig ontvangen" en gooit de rest weg. `protocol.Reassembler` is de algemene referentie (ook voor een nagebootste FAP in tests). Die verdraagt een andere volgorde en identieke duplicaten. Een duplicaat met andere tekst geeft een fout. Ontbrekende delen verlopen na 30 s. Er staan hoogstens 4 antwoorden tegelijk open, met hoogstens 8 delen en 4000 B per antwoord.
- **Eén actie tegelijk.** Zolang een actie loopt, stuurt de FAP geen nieuwe ACTION. De noodstop is de uitzondering: die mag altijd, en de Pi voert hem direct uit, ook als hij al bezig is. Back tijdens een actie stuurt CANCEL. De Pi antwoordt op een tweede ACTION met ERROR `BUSY`.
- **Rate limits:** de FAP houdt minstens 200 ms tussen twee berichten. De Pi telt per bridge (dus over alle FAP-sessies heen) met een token bucket (5/s, burst 5) en laat de rest vallen (geteld, gelogd). Bewust niet per sessie: dan kreeg een FAP met elke nieuwe HELLO een nieuwe burst. De Pi stuurt zelf ook hoogstens 5 berichten/s: PROGRESS hoogstens 1 per seconde, en een nieuwere vervangt een oudere die nog niet verstuurd is.

## 5. Handdruk en versies

1. De Pi start de FAP (`StartRequest{name, args:"RPC"}`). De FAP koppelt en roept `rpc_system_app_send_started` aan, waarna de Pi `APP_STARTED` krijgt.
2. De FAP stuurt HELLO. Krijgt hij binnen 3 s geen WELCOME, dan stuurt hij hem opnieuw, hoogstens 3 keer. Daarna toont hij "Pi antwoordt niet".
3. Bij een HELLO met een nieuwe `instance` wist de Pi de vorige FAP-staat: seq-trackers en openstaande handles worden ingetrokken. Dan stuurt hij WELCOME, PRESETS en STATUS. Een herhaalde HELLO met dezelfde `instance` krijgt gewoon nog een WELCOME, zonder reset.
4. Elk ander bericht vóór HELLO krijgt ERROR `NOT_READY`. De FAP verwerkt niets behalve WELCOME zolang de handdruk niet rond is.
5. **Opnieuw groeten.** Krijgt de FAP ERROR `NOT_READY` (ook met `ref = 0`), dan kent de Pi deze sessie niet (meer). De FAP laat dan alles van de sessie los (open vraag, lopende actie), toont "Verbinden met Pi..." en stuurt opnieuw HELLO met **dezelfde** `instance`, hoogstens één per 3 s en weer hoogstens 3 keer. Ook na "Pi antwoordt niet" begint hij zo opnieuw. Tijdens een lopende handdruk negeert hij `NOT_READY` (de HELLO-herhaling loopt al).

   Wanneer: de Pi laat een sessie los terwijl de app nog draait, bijvoorbeeld als een CONFIRM_PROMPT of CONFIRM_CLOSED niet afgeleverd kon worden (§6). `start_rpc_app` is dan een no-op (de app draait al), en de FAP stuurt uit zichzelf geen HELLO meer. Daarom stuurt de bridge bij zo'n "start" zelf ERROR `NOT_READY` met `ref = 0`. Bij de HELLO die volgt, maakt de Pi een nieuwe sessie (nieuwe `ConfirmHandles`, nieuwe seq-tracker); handles van de vorige sessie gelden niet meer. Komt er binnen de HELLO-time-out (10 s) niets, dan sluit de Pi **zijn eigen** app (nooit een app van de gebruiker) en start hem bij de volgende pending opnieuw.

**Versies:** de kopversie is de versie van de layout van dat bericht. De Pi decodeert alleen versies die hij kent. Een onbekende versie geeft ERROR `UNSUPPORTED_VERSION`, met `ref` = de seq uit de kop (de codec bewaart die in `ProtocolError.seq`). HELLO, WELCOME en ERROR houden in elke latere versie dezelfde v1-layout, zodat een oude en een nieuwe kant elkaar altijd nog kunnen vertellen dat ze niet passen. `proto_max` in HELLO en `proto` in WELCOME zijn voor de onderhandeling. In v1 is dat altijd 1.

## 6. Bevestigen op de Flipper

Doel: een transmit die een agent of frontend voorstelt (`confirmation_required` met `pending_id`), wordt op de Flipper door een mens met OK bevestigd of met Back geweigerd.

```
Pi (bridge)                                    FAP
 pending van ELKE frontend (mcp, agent, flipper-ui) → PendingStore-luisteraar → rij
 handle = ConfirmHandles.issue(pending, instance, ttl)
 CONFIRM_PROMPT{handle, ttl_s, summary "(van mcp)"}  ───▶  scherm "Zenden? 60s", licht + 1x trillen
                                               OK pas na 1 s actief (geen doorgeschoten knopdruk)
                                        ◀───  CONFIRM_ANSWER{handle, ja/nee}
 pending = ConfirmHandles.take(handle, instance)   (eenmalig, TTL, zelfde instance)
 ja:  runner.confirm_physical(lease, pending)   (TTL, eenmalig, nieuwe scope-check)
 nee: runner.reject_physical(lease, pending)
 CONFIRM_CLOSED{handle, uitgevoerd|geweigerd|mislukt}  ───▶  "Bevestiging: uitgevoerd" in status
```

- **Handle:** een willekeurig u32 van de Pi (`secrets`), nooit 0 en binnen één `ConfirmHandles` nooit hergebruikt. De `pending_id` zelf gaat niet naar de FAP. Een antwoord geldt alleen voor een handle die deze Pi in de **huidige FAP-sessie** gaf (sinds de laatste HELLO), alleen **één keer**, en alleen **binnen de TTL** volgens de klok van de Pi. CONFIRM_ANSWER heeft zelf geen `instance`-veld: de bridge zoekt de handle op in de `ConfirmHandles` van de huidige sessie, en bij elke nieuwe HELLO (of loskoppelen) worden alle handles ingetrokken en begint een nieuwe `ConfirmHandles`. Een handle uit een vorige sessie is dus onbekend en verbruikt niets. Het aftellen op de FAP is alleen voor de mens; de Pi berekent `ttl_s` pas als het bericht de zendrij verlaat (afgerond naar beneden), zodat het aftellen nooit langer is dan de handle op de Pi (los van de verzendtijd).
- **Antwoord hoort bij wat de mens zag.** De FAP legt de handle vast **bij de knopdruk** (de view bewaart de handle van de vraag die hij toont) en stuurt het antwoord meteen. Is die handle op dat moment niet meer de open vraag (intussen gesloten of vervangen), dan stuurt hij niets. Vroeger ging het antwoord pas mee met een custom event, en kon een CONFIRM_PROMPT die tussendoor binnenkwam de handle al vervangen hebben: dan ging OK naar een vraag die de mens nooit zag.
- **Flipper verplicht (besluit Stijn, VOORTGANG.md).** Zodra WELCOME echt is afgeleverd, koppelt de bridge zich als **fysieke bevestiger** (`runner.attach_physical_confirmer`). De lease is een object van de `PendingStore`, geen frontend-id: een andere frontend kan hem dus niet nadoen. Zolang hij gekoppeld is:
  - weigert `confirm_action` uit **elke** frontend (ook `flipper-ui` zelf) met `confirm_on_flipper`, en de pending blijft staan;
  - toont de bridge pendings van **alle** frontends, met de bron erbij ("(van mcp)"), zodat de mens ziet wie het vraagt;
  - bevestigt alleen `runner.confirm_physical(lease, …)`. TTL, eenmalig gebruik en de nieuwe scope-check gelden zoals altijd. De audit krijgt `confirmed_on: "flipper"` en `created_by`;
  - mag afwijzen uit de chat nog wel, want afwijzen zendt niets.

  Een agent-sessie krijgt bij een geweigerde bevestiging de status `ON_FLIPPER`. De bevestiging blijft dan open. De runner meldt de uitkomst van elke pending aan wie luistert (`ToolRunner.subscribe_resolutions`: `CONFIRMED` met het resultaat, `REJECTED`, `EXPIRED`, `CLEARED`), en elke `AgentSession` luistert naar haar eigen pendings. Het model hoort de uitkomst dus in zijn volgende beurt, ook in `pitoolbox-agent` (niet alleen bij presets), en kan nog steeds niets bevestigen. `pitoolbox-agent` vraagt niet "Bevestigen?" zolang de app gekoppeld is: hij wacht op OK/Back op de Flipper en toont de uitkomst (Ctrl+C = niet wachten; de uitkomst verschijnt dan vóór de volgende invoer). Valt de app weg terwijl de bevestiging nog openstaat, dan vraagt hij alsnog `Bevestigen? [j/N]`. De luisteraars worden onder de lock van de `PendingStore` aangeroepen en zetten alleen iets in een eigen wachtrij.
- **Loskoppelen** gebeurt als eerste stap, vóór alle andere opruimwerk, bij APP_CLOSED, een verbroken link, een hangende app, geen HELLO op tijd of het stoppen van de bridge. Daarna geldt weer de oude regel (gebonden aan de eigen frontend).
- **Racevrij:** koppelen, loskoppelen en elke `claim` lopen onder dezelfde lock van de `PendingStore`. Een bevestiging uit de chat ziet dus óf de lease (geweigerd, niets verbruikt) óf geen lease (oude regel). Valt de link weg, dan blijft de lease staan tot de bridge hem loskoppelt: in dat gat weigert de chat (fail closed). Een OK die nog onderweg is na het loskoppelen, gebruikt een verlopen lease en voert niets uit (`STALE_CONFIRMER`). OK en chat tegelijk: de claim is atomair en eenmalig, dus de actie kan nooit twee keer lopen. Een stresstest koppelt los en aan terwijl threads bevestigen, en controleert dat er nooit een chat-bevestiging slaagde terwijl de lease gekoppeld was.
- **Eén vraag tegelijk, en pas na CONFIRM_CLOSED:** de rij is de `PendingStore` zelf (oudste eerst, zonder de pendings waarvoor een OK/Back loopt), dus geen enkele pending valt uit een eigen rij. Een nieuwe CONFIRM_PROMPT gaat pas weg als de CONFIRM_CLOSED van de vorige echt afgeleverd is (bevestigd door de FAP). Kan een CONFIRM_PROMPT of CONFIRM_CLOSED niet afgeleverd worden terwijl de link in orde lijkt (de FAP weigert 4 keer, de zendrij is vol), dan weet de Pi niet meer wat er op het scherm staat: hij koppelt los (eerst de lease) en laat de app opnieuw groeten (§5). Daarna komen de open pendings opnieuw, met nieuwe handles.
- **Sluiten:** een CONFIRM_CLOSED met `ingetrokken` haalt de vraag weg: `emergency_stop` wist alle pendings, en de bridge stuurt dan CONFIRM_CLOSED voor de open handle. Verloopt de TTL, dan sluit de FAP het scherm zonder iets te sturen, en de Pi stuurt CONFIRM_CLOSED met `verlopen`. Komt er een antwoord op een handle die volgens de Pi net verlopen is (de timer was nog niet langs), dan krijgt de FAP ERROR `CONFIRM_INVALID` **en** CONFIRM_CLOSED met `verlopen`, zodat er niet "ja gestuurd" blijft staan terwijl er niets gebeurde.
- **Langer dan een handle:** een handle leeft hoogstens 120 s (`ttl_s` is een u8), een pending zo lang als `PITOOLBOX_CONFIRM_TTL_S`. Verloopt de handle terwijl de pending nog leeft, dan stuurt de Pi CONFIRM_CLOSED (`verlopen`) en daarna dezelfde vraag opnieuw, met een nieuwe handle. Ook als de confirm-worker vol zit (ERROR `BUSY`), trekt de Pi de verbruikte handle in (`ingetrokken`) en vraagt hij opnieuw.
- **Sessieverlies of nieuwe HELLO:** alle handles van die instance worden ingetrokken. De pending zelf verloopt daarna vanzelf in de `PendingStore`.

## 7. Snelle acties en presets

Snelle acties zijn vast in de FAP (ids) en vast op de Pi (`protocol.QUICK_ACTIONS`). Een test controleert dat elke tool bestaat en dat de argumenten geldig zijn.

| id | Label | Tool-call(s) via de runner |
|---|---|---|
| 1 | Status | `status {}` |
| 2 | Wifi-scan | `wifi_scan {method: iw, band: abg}`. Naar de FAP gaat een samenvatting: aantal, aantal in scope, sterkste SSID's |
| 3 | Stop captures | `capture_stop {capture_id}` voor elke actieve capture. De ids komen uit `status`, nooit van de FAP |
| 4 | Noodstop | `emergency_stop {}`. Altijd toegestaan, ook als er al een actie loopt |

Presets staan in de Pi-config, als lijst van `{id, label, prompt}`. Alleen `id` en `label` gaan naar de FAP. Een ACTION met `kind = preset` start een agent-beurt (`AgentSession(frontend="flipper-ui").run_turn(prompt)`). Tussendoor gaat er PROGRESS naar de FAP (bijv. "tool: wifi_scan"), en aan het eind het antwoord in RESULT-delen (`chunk_result`, tekst via `to_text_box` naar ASCII en afgebroken op woordgrenzen). Zonder LLM-config is bit1 in WELCOME uit, en een preset krijgt ERROR `AGENT_UNAVAILABLE`.

## 8. Starten

### Door de Pi

1. `app_lock_status_request`. Is `locked = true`, dan doet de Pi niets: hij **sluit nooit een app van de gebruiker**, ook niet via GUI-input. Spike test 2: direct na een `APP_CLOSED` kan `locked` nog één keer `true` zijn, dus hooguit 1,5 s wachten en elke 0,25 s opnieuw kijken (`FlipperTiming.loader_free_wait_s`/`loader_poll_s`).
2. `StartRequest{name: "/ext/apps/Tools/pitoolbox_ui.fap", args: "RPC"}`. Bij `ERROR_APP_SYSTEM_LOCKED` (de gebruiker was net eerder) geeft de Pi het op. Uitzondering: na een **verloren link** probeert de bridge het `locked_retry_s` (10 s) lang opnieuw, omdat de bezette loader dan nog onze eigen, sluitende app kan zijn (§10). Er wordt nooit een app gesloten.
3. Wachten op `APP_STARTED` en dan op HELLO (`hello_timeout_s`, 10 s: de FAP probeert HELLO 3x per 3 s, §5).

### Handmatig (vanuit het Apps-menu)

Een FAP die je zelf start, krijgt geen `"RPC %08lX"` en heeft dus geen kanaal naar de Pi (spike §3). **Gekozen ontwerp: verzoekbestand plus RPC-start door de Pi.**

1. De FAP ziet geen geldige RPC-args. Met `rpc_get_sessions_count` (geëxporteerd, `api_symbols.csv` r. 3293, `M/applications/services/rpc/rpc.c#L520`) kijkt hij of er een host verbonden is.
   - Geen sessie: hij toont "Geen verbinding met Pi" en sluit na 4 s. Er blijft niets achter.
   - Wel een sessie: hij schrijft `/ext/apps_data/pitoolbox_ui/start_request` met de inhoud `PTBUI 1 <furi_hal_rtc_get_timestamp>\n`. Dat gaat atomair: eerst `.tmp` schrijven, dan hernoemen. Hij toont "Pi wordt gevraagd de app te openen..." en **sluit na 1,5 s zichzelf**, zodat de loader vrij is.
2. De bridge op de Pi pollt als er geen FAP-sessie is en er geen tool loopt, elke 3 s, met `storage_stat` op dat pad. Is er nog **geen** verbinding, dan opent de bridge er zelf een (een RPC-ping) en houdt die open (keepalive). Zonder RPC-sessie van de Pi schrijft de FAP immers geen verzoek (stap 1), en een vers gestarte service had anders nooit een sessie: op hardware gevonden op 2026-10-17 ("Geen verbinding met Pi"). Mislukt verbinden, dan volgt de backoff `link_backoff_s` (3, 6, 12, 30, 60 s), dus geen reconnect-storm. Een open link in CLI-modus (een Sub-GHz/NFC/RFID-actie over USB) laat de bridge met rust: die zet hij niet terug naar RPC. Dat is één klein frame, ongeveer 100 ms over BLE, dus ongeveer 3 % van de link. `ERROR_STORAGE_NOT_EXIST` betekent niets te doen.
3. Is het bestand er, dan leest de Pi het (≤ 64 B), wist het **meteen**, en controleert inhoud en versheid. Daarvoor vergelijkt hij de tijdstempel met `system_get_datetime_request`, omgerekend met `calendar.timegm` op dezelfde manier als `datetime_datetime_to_timestamp`. Het verschil moet ≤ 30 s zijn. Een oud verzoek (de Pi was niet verbonden toen je de app startte) wordt dus genegeerd, en de app springt niet uren later ongevraagd open.
4. Daarna volgt dezelfde route als "Door de Pi". De lock-check mag 3 s lang (`manual_lock_wait_s`) elke 0,25 s herhaald worden, omdat de handmatige instance nog aan het sluiten is. Is de loader dan bezet, dan heeft de gebruiker intussen iets anders geopend: opgeven.

Overwogen en afgewezen (bron: mntm-012):

| Alternatief | Waarom niet |
|---|---|
| FAP start zelf het seriële BLE-profiel | sluit de RPC-sessie en is racegevoelig (spike §1) |
| `rpc_session_open` vanuit de FAP (geëxporteerd, `api_symbols.csv` r. 3298) | maakt een **lokale** sessie zonder transport. De sessie van de Pi blijft onbereikbaar |
| `loader_enqueue_launch` (start een app nadat de huidige sluit, `M/applications/services/loader/loader.c#L309`) | kan geen geldige `RpcAppSystem*` meegeven. Alleen `rpc_app.c` vervangt `"RPC"` door de pointer van de sessie |
| Pi kijkt alleen naar `app_lock_status` | zegt niet **welke** app open is, dus niet te onderscheiden van een andere app van de gebruiker |
| Pi leest het scherm via de GUI-stream | ~1 KiB per frame bij 1,2 KiB/s over BLE: legt de link plat en is fragiel |
| Pi drukt Back via GUI-input en start opnieuw | grijpt in op wat de gebruiker doet, en is racy (spike test 1) |
| `storage_timestamp_request` als versheid | geeft de tijd van de laatste wijziging van **de hele opslag**, niet van het bestand (`M/applications/services/storage/storage_processing.c#L349`, `storage_glue.c#L68`). Daarom staat de tijd in de inhoud |
| Pi start de FAP bij elke BLE-connect | ongevraagd scherm (spike, open vraag 4). Kan later als optie, niet als standaard |

## 9. FAP: veilig bij sessieverlies

Wat de firmware doet (mntm-012):

- Valt de sessie weg (BLE-disconnect, USB dicht), dan zet `bt.c` eerst `BT_RPC_EVENT_DISCONNECTED` en roept dan `rpc_session_close` aan. Die zet de send-callback van de sessie op NULL (`M/applications/services/bt/bt_service/bt.c#L394-L403`, `rpc.c#L433`). De worker stopt, en `rpc_session_thread_pending_callback` draait daarna **in de timer-thread** (`rpc.c#L354`, `#L384`). Die roept `rpc_system_app_free` aan, met SessionClose naar de app, en **wacht in een lus tot de callback NULL is. Pas dan geeft hij het `RpcAppSystem` vrij** (`rpc_app.c#L483-L506`). Meteen daarna gaat ook de sessie zelf vrij.
- `rpc_system_app_confirm` controleert met `furi_check` dat er een open opdracht is (`rpc_app.c#L361-L363`). Een tweede confirm, of een confirm op SessionClose, crasht dus.

**Waarschijnlijke oorzaak van de BusFault uit de spike** (aanname, niet met een crashlog bewezen): `rpc_debug_app` roept bij SessionClose `view_dispatcher_stop` aan **vanuit de timer-thread**, en pas daarna `app->rpc = NULL` (`rpc_debug_app.c#L50-L54`). De app-thread kan dan al in `rpc_debug_app_free` zitten, ziet `app->rpc` nog staan, en roept `rpc_system_app_send_exited` aan op een context die de timer-thread net vrijgeeft (`#L143-L147`). De log van run 4 laat precies die volgorde zien: `Session terminated` → `SendExit`. Dat is een use-after-free, en het verklaart reboots die afhangen van de timing. In dxprobe vond ik zo'n race niet. Daar viel de reboot samen met een ticker die op het moment van verbreken verstuurde. Of dat een firmwareprobleem is, moet de hardwaretest uitwijzen.

Wat `pitoolbox_ui` daarom doet (`pitoolbox_ui.c`):

1. **Eén mutex (`rpc_mutex`) rond elke `rpc_system_app_*`-aanroep**, steeds na de controle `app->rpc != NULL`. Dat geldt ook voor `set_callback`/`send_started` bij het starten en voor `exchange_data`.
2. **SessionClose in de callback:** onder de mutex `rpc_system_app_set_callback(rpc, NULL, NULL)`, `app->rpc = NULL`, `rpc_gone = true`. Geen confirm, geen GUI-aanroep. Daarna komt er nooit meer een `rpc_system_app_*`-aanroep, uit geen enkele thread, want alle paden controleren eerst `app->rpc`.
3. **De mutex blijft vast tijdens `exchange_data`.** Daardoor kan de SessionClose-callback (en dus het vrijgeven in de timer-thread) niet midden in een verzending komen. Dat kan niet eeuwig blokkeren: over BLE stopt het versturen op `DISCONNECTED`, dat vóór het sluiten wordt gezet (`bt.c#L226-L245`), en over USB stopt `pipe_send` bij een gebroken pipe (`lib/toolbox/pipe.c#L145-L160`). De volgorde van de locks is overal dezelfde (`rpc_mutex` → `callbacks_mutex` van de sessie), dus er is geen deadlock. Dit gaat in tegen de voorzichtige tip uit de spike ("geen mutex vasthouden tijdens exchange_data"). Zonder de mutex kan de context juist tijdens het versturen vrijgegeven worden.
4. **Elke DataExchange wordt precies één keer bevestigd, in de callback zelf, in elke toestand.** Eerst controleren (grootte 5..512, magic), dan kopiëren naar een eigen wachtrij met `put(…, 0)` (nooit wachten), en dan `confirm(ok)`. AppExit krijgt `confirm(true)`, alle andere app-opdrachten `confirm(false)`.
5. **Callbacks raken geen GUI aan.** De app-thread leest de wachtrij via een event-loop-abonnement (`furi_event_loop_subscribe_message_queue`). Daarom geen `view_dispatcher_send_custom_event` vanuit een callback: die wacht met `FuriWaitForever` (`view_dispatcher.c#L393`) en zou bij afsluiten vast kunnen blijven op een wachtrij die niemand meer leegt.
6. **Afsluiten:** onder de mutex **eerst `send_exited`, dan `set_callback(NULL)`**, dan `rpc = NULL`. Die volgorde is essentieel. `rpc_system_app_free` (`rpc_app.c`) roept onze callback alleen aan als die niet NULL is, en geeft vrij zodra hij NULL is. Zetten we de callback eerst op NULL, dan kan een gelijktijdige SessionClose de context vrijgeven terwijl wij nog in `send_exited` zitten: dezelfde use-after-free als in `rpc_debug_app`. Zolang de callback staat, blokkeert de free-thread in onze callback op de mutex die wij vasthouden. Gevonden in de review en gerepareerd vóór de eerste hardwaretest. Dan wachten tot er geen callback meer in onze code loopt (een atomische teller). Pas daarna gaan wachtrij, mutex en app vrij, steeds met de eigen `*_free()` (`submenu_free`, `text_box_free`, `popup_free`, `view_free`, `view_dispatcher_free`, `scene_manager_free`, `furi_message_queue_free`, `furi_mutex_free`, `furi_string_free`).
7. **Geen notificaties per bericht** (spike test 3: 200 ms-stalls). Alleen één keer licht en één keer trillen bij een bevestigingsvraag.
8. **Na SessionClose:** de FAP toont 2 s "Verbinding met Pi verbroken" en sluit. Een nieuwe sessie kan niet opnieuw aan een draaiende FAP koppelen (spike §3). Is alleen de **sessie in de bridge** weg (de RPC-sessie bestaat nog), dan groet de FAP opnieuw zodra hij ERROR `NOT_READY` krijgt (§5).
9. **Alleen exact `"RPC %08lX"`** (12 tekens, 8 hexcijfers, niet 0) geldt als RPC-start. Al het andere (geen args, een bestandspad uit het archief) is een handmatige start. Restrisico in mntm-012: een RPC-client kan een vervalste pointer meegeven (spike §3). Dat kan alleen een partij die al RPC-toegang heeft.

Blijvend restrisico: tussen de controle `if(rpc_app->callback)` in de firmware en het eerste statement van onze callback zit een venster van een paar instructies. Daarin kan het afsluiten van de app net vallen. Dat venster hebben alle firmware-apps. De teller maakt het zo klein als vanuit een FAP kan.

## 10. Bridge op de Pi

Gebouwd in [`src/pitoolbox/frontends/flipper_ui/bridge.py`](../src/pitoolbox/frontends/flipper_ui/bridge.py) (tests: `tests/test_flipper_ui_bridge.py`, `tests/test_flipper_app_channel.py`, met een nagebootste FAP in `tests/flipper_fakes.py`). Aan met `PITOOLBOX_FLIPPER_UI=on`, in `pitoolbox-mcp` of `pitoolbox-agent`. **Nooit in twee processen tegelijk:** één proces bezit de Flipper, twee verbreken elkaars BLE-link.

```
FlipperDevice (één RPC-sessie, gedeeld met de tools; één lock)
  ├─ RpcClient.event_hook: elk ongevraagd frame vóór de eventqueue
  │    DataExchange van de app / zijn APP_CLOSED → AppEvent → bridge (niet in de queue van 256)
  ├─ app_send: één DataExchange, stop-and-wait; immuun voor de noodstop, nooit een reconnect
  └─ poll_app_events: leest alleen als niemand op de lock wacht (tool-calls gaan voor)
FlipperUiBridge
  ├─ hoofdlus: alle bridge-toestand; andere threads zetten alleen werk in de inbox
  ├─ lezer: poll_app_events
  ├─ zender: ≤5/s, 3x opnieuw na 200 ms bij CMD_ERROR, PROGRESS ≤1/s (nieuwste wint)
  ├─ workers: action, confirm, device, aux; de noodstop heeft een eigen thread
  ├─ HELLO → WELCOME → lease koppelen → PRESETS, STATUS, open pendings tonen
  ├─ ACTION quick → QUICK_ACTIONS[id] → runner.call("flipper-ui", tool, args) → RESULT
  ├─ ACTION preset → AgentSession("flipper-ui").run_turn(prompt) → PROGRESS/RESULT
  ├─ CONFIRM_ANSWER → ConfirmHandles.take → runner.confirm_physical / reject_physical
  ├─ CANCEL → agent-beurt stopt bij de volgende stap; tool-call via abort waar mogelijk
  └─ zonder app en idle: zo nodig verbinden, start_request pollen (§8); nieuwe pending → app starten als de loader vrij is
```

- **Starten:** handmatig (§8, verzoeken ouder dan 30 s volgens de Flipper-klok worden genegeerd), of door de Pi bij een nieuwe pending. De Pi start de app **alleen als de loader vrij is**, en sluit nooit een app van de gebruiker. Na een verbroken link start de bridge de app opnieuw, met backoff, tot 5 minuten. Is de loader daarbij bezet, dan probeert hij het alleen de eerste 10 s na het verlies opnieuw (`locked_retry_s`): zo kort erna kan dat nog de eigen app zijn die 2 s "Verbinding met Pi verbroken" toont (§9); later is het een app van de gebruiker. De Pi sluit daarvoor nooit een app. Sloot de gebruiker de app zelf, of kwam er nooit een HELLO, dan niet. Draait de app nog terwijl de bridge losgekoppeld is, dan vraagt de bridge hem opnieuw te groeten (§5). Elke fout bij het starten (ook `Aborted` door een noodstop terwijl de start op de device-lock wachtte) zet de bridge terug naar idle; als vangnet heeft STARTING ook een deadline (`start_timeout_s`, 120 s).
- **Stoppen:** eerst de lease los, dan de app sluiten. `stop()` wacht daar hoogstens `stop_join_s` op: `exit_rpc_app` wacht op de device-lock, en een lopende tool kan die lang vasthouden. De app sluit dan later alsnog (of bij `device.close()`).
- **Status:** de lezer (`poll_app_events`) wijkt voor elke tool-call; de `status`-tool meldt hem dus niet als "bezet".
- **`ir_send` heeft de loader nodig.** Staat de app open, dan sluit `FlipperDevice` hem eerst, voert de Infrared-app uit en start de app daarna opnieuw (handoff). De app groet dan met een nieuwe instance. Mislukt de herstart, dan koppelt de bridge los. Het scherm van de app gaat bij elke IR-transmit dus even dicht en weer open.
- **Sub-GHz/NFC/RFID via de CLI** weigeren met `app_locked` zolang de app open is: het verlaten van de RPC-sessie zou hem sluiten (`loader_lock`, spike §2). Daarom zitten ze ook niet in de snelle acties.
- Bij `APP_CLOSED` of een verbroken transport: eerst de lease los, dan alle handles intrekken (`revoke_all`) en de zendrij leeg.
- Lange storage-operaties tijdens een app-sessie vermijden of opknippen: ze blokkeren beide richtingen (spike test 4).

### Nog niet op hardware getest

Wel getest (2026-10-17, USB en BLE): de handshake, STATUS via de status-tool, de valtest (20/20, twee keer), keepalive met de app open. Nog niet: `storage_delete` en de datumaanvraag op Momentum (handmatige start); de timing van de `ir_send`-handoff (hoe lang de loader bezet blijft na APP_CLOSED); een app die echt hangt; of OK/Back en de snelle acties op de echte app de vraag-flow volgen.

## 11. Testen

- `uv run pytest tests/test_flipper_ui_bridge.py tests/test_flipper_ui_confirm_flow.py`: de bridge end-to-end tegen een nagebootste FAP. `test_flipper_ui_confirm_flow.py` dekt de regressies uit de review: geen nieuwe vraag zolang CLOSED niet afgeleverd is, opnieuw groeten op `NOT_READY`, CLOSED(`verlopen`) bij een antwoord op een net verlopen handle, opnieuw vragen na een verlopen handle/volle rij/`BUSY`, de noodstop tijdens het starten, pollen zonder te verbinden, een begrensde `stop()`, en dat `pitoolbox-agent` de uitkomst van OK op de Flipper hoort.
- `uv run pytest tests/test_flipper_ui_protocol.py`: roundtrip van elk type, vaste vectoren, grootste berichten ≤ 512 B, modelgrenzen, strikte decoder (34 foutgevallen), fuzz (5000 willekeurige berichten, mutaties en inkortingen: alleen `ProtocolError`, en wat geaccepteerd wordt is canoniek), tekstconversie, seq (duplicaat, gat, wrap), rate limit, reassembly (andere volgorde, duplicaat, conflict, ontbrekend deel, limieten), handles (eenmalig, TTL, instance, intrekken) en de koppeling snelle actie → echte tool.
- C ↔ Python: `ptb_proto.c` op de host gecompileerd (gcc, `-Wall -Wextra -Werror`) en vergeleken met de Python-codec op 14.200 berichten: 0 verschillen. Dat script staat niet in de repo.
- Hardware: `pitoolbox-flipper ui-install`, `ui-test` en `ui-droptest` (zie de README van de FAP, met de resultaten). De valtest (BLE weg terwijl de Pi verstuurt, 20 keer) is twee keer 20/20 geslaagd.
- Op afstand, alleen voor ontwikkeling: `pitoolbox-flipper remote screen|press` en `ui-test --confirm-check --auto` (README van de FAP, "Op afstand"). De Pi drukt knoppen via `gui_send_input_event_request` (PRESS, SHORT of LONG, RELEASE, zoals de officiële Python-client) en leest één frame van de schermstream. Dit staat los van de afwijzingen in §8: het product leest het scherm niet en drukt nooit zelf. **Wachter, vóór elke klik:** alleen als de loader vrij is of als de open app de PiToolbox-app is die dit proces via RPC startte; anders `RemoteInputRefused`, want een OK in een andere app kan uitzenden. Elke klik is een WARNING in de log. **Nooit een tool:** met invoer op afstand zou de Pi zijn eigen "Flipper verplicht"-vraag kunnen bevestigen. Dat mag alleen in `ui-test` (allowlist `status`, lege scope). `tests/test_flipper_remote.py` controleert dat alleen de devicelaag en de ontwikkelaars-CLI `gui_press`/`gui_screen` noemen, en dat geen ToolSpec of handler ze bereikt. Schermframes leest het device met de gewone lezer: `wait_event` pakt het eerste frame na de start, berichten van de app gaan intussen via de event-hook naar de bridge, en een nagekomen frame gooit de hook weg (niet in de eventqueue).
