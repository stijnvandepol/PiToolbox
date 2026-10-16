# pitoolbox

Software voor een Raspberry Pi 4 of 5 (Kali Linux ARM64) die een **Flipper Zero** en een **Alfa AWUS036AXML** aanstuurt via een LLM, voor je eigen lab en apparaten.

> **Status: fase 3.** Kern + MCP-server over stdio (lokaal) en streamable HTTP met OAuth 2.1 (remote, via een Cloudflare Tunnel naar de Claude-app). Flipper via USB of Bluetooth LE. De Flipper-app en de agent-loop komen in latere fases.

## Ontwerp in het kort

```
frontend (MCP stdio, later HTTP / Flipper-bridge)
        │  runner.call(frontend, tool, args)
        ▼
ToolRunner ── valideren (pydantic, extra velden = fout)
        ├── Policy: allowlist → scope.yaml → risicoklasse
        ├── transmit binnen scope → pending_id → confirm_action
        ├── uitvoeren (FlipperDevice / AlfaAdapter / CaptureManager / SystemProbe)
        └── AuditLog: één JSON-regel per call
```

- **Geen generieke shell, nergens.** Alleen benoemde tools met een vast argumentmodel. Subprocessen lopen via één runner met een allowlist (`iw`, `ip`, `airodump-ng`, `vcgencmd`), altijd argv-lijsten en nooit `shell=True`. De Flipper-CLI wordt alleen gebruikt met vaste sjablonen die uit gevalideerde getallen worden opgebouwd.
- **Risicoklassen:**

  | Klasse | Betekenis | Regel |
  |---|---|---|
  | `read` | scannen/lezen | toegestaan (met validatie zoals frequentiegrenzen) |
  | `capture` | opslaan, adapter omzetten | alleen doelen uit `scope.yaml` |
  | `transmit` | alles wat zendt | buiten scope altijd geweigerd; binnen scope eerst een `pending_id` en daarna expliciet `confirm_action` |
  | `control` | `emergency_stop`, `confirm_action` | altijd toegestaan |

- **Bevestiging (transmit):** de tool voert niets uit maar geeft `confirmation_required`, een `pending_id` en een samenvatting terug. Pas `confirm_action(pending_id)` voert de actie uit. De bevestiging is eenmalig, verloopt na 60 s, is gebonden aan de frontend, en de scope wordt bij het bevestigen opnieuw gecontroleerd.
- **Scope** (`~/.config/pitoolbox/scope.yaml`, voorbeeld: [scope.example.yaml](scope.example.yaml)) wordt live herladen bij wijziging. Is het bestand ongeldig of ontbreekt het, dan geldt een lege scope (fail closed).

## Tools (fase 1)

| Tool | Klasse | Wat |
|---|---|---|
| `system_info` | read | Flipper DeviceInfo (firmware, hardware) via RPC |
| `storage_list` / `storage_read` | read | Bestanden op de Flipper (`/ext`, `/int`), met een maximumgrootte |
| `subghz_rx` | read | Sub-GHz ontvangen op een frequentie, 1–120 s (CLI `subghz rx`) |
| `nfc_read` | read | NFC-tag detecteren en protocollen tonen (CLI `nfc` → `scanner -t`) |
| `rfid_read` | read | 125 kHz RFID lezen (CLI `rfid read`) |
| `ir_send` | transmit | IR-knop uit een `.ir`-bestand van een apparaat in scope (RPC, Infrared-app) |
| `alfa_set_mode` | capture | Alfa naar managed/monitor (nooit de uplink) |
| `wifi_scan` | read | `iw scan` (managed) of `airodump-ng` (monitor), JSON met een `in_scope`-vlag per AP |
| `capture_start` / `capture_stop` | capture | pcap van een BSSID uit scope, op het kanaal uit scope, naar `~/captures/<sessie>/` |
| `status` | read | onderspanning (`vcgencmd get_throttled`), uplink, schijf, Flipper, adapter, captures |
| `emergency_stop` | control | stopt alle captures, breekt Flipper-acties af, wist openstaande bevestigingen |
| `confirm_action` | control | voert een `pending_id` uit, alleen na een expliciete "ja" van jou |

### Waarom Sub-GHz/NFC/RFID via de CLI gaan

De officiële Flipper-RPC ([flipperzero-protobuf](https://github.com/flipperdevices/flipperzero-protobuf)) kent alleen system, storage, app, gui, gpio, property en desktop. Ontvangen via Sub-GHz, NFC of RFID bestaat daar niet. Dezelfde USB-poort heeft ook een CLI. `FlipperDevice` schakelt automatisch tussen beide (`start_rpc_session` ↔ `stop_session`). De CLI-commando's en hun uitvoerformaten komen uit de firmwarebronnen en zijn vastgelegd per **firmwareprofiel**:

| Profiel | Firmware | Getest |
|---|---|---|
| `official` | officiële firmware 1.5.x | tegen de bron |
| `momentum` | Momentum `mntm-012` | tegen de bron en op hardware (USB) |

Momentum heeft dezelfde CLI als official. Alleen accepteert de firmware een ruimer Sub-GHz-ontvangstbereik (281–361, 378–481 en 749–962 MHz, `furi_hal_subghz.c`). Het beleid van pitoolbox houdt `subghz_rx` bewust op het officiële bereik, zodat elk profiel hetzelfde toestaat. Het profiel wordt automatisch gekozen op basis van `firmware_origin_fork`. Bij een andere firmware weigeren de CLI-tools tot er een profiel is. Je kunt een profiel forceren met `PITOOLBOX_FLIPPER_CLI_PROFILE`.

## Installatie (ontwikkelen / fase 1)

Kali blokkeert systeem-pip, dus alles gaat via [uv](https://docs.astral.sh/uv/) in een venv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <deze repo> ~/pitoolbox && cd ~/pitoolbox
uv sync
mkdir -p ~/.config/pitoolbox && cp scope.example.yaml ~/.config/pitoolbox/scope.yaml
# vul scope.yaml met je eigen BSSID's en IR-bestanden
```

Systeempakketten op de Pi: `sudo apt install iw iproute2 aircrack-ng libraspberrypi-bin`. De gebruiker moet in de groepen `dialout` (Flipper-serial) en `video` (`vcgencmd`) zitten.

Rechten voor wifi: `iw set type` en `airodump-ng` hebben `CAP_NET_ADMIN` en `CAP_NET_RAW` nodig. Voor handmatig testen kan dat met `sudo`. De systemd-unit ([deploy/systemd/pitoolbox-mcp.service](deploy/systemd/pitoolbox-mcp.service)) geeft de service die capabilities via `AmbientCapabilities`, zodat hij niet als root draait.

### Testen en lint

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```

Alle tests draaien zonder hardware: de Flipper wordt gesimuleerd (CLI met echo/prompt, RPC-frames) en `iw`, `ip`, `airodump-ng` en `vcgencmd` krijgen opgenomen uitvoer.

### Lokaal als MCP-server (stdio)

```bash
uv run pitoolbox-mcp --scope ~/.config/pitoolbox/scope.yaml
```

Testen met de [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector uv --directory ~/pitoolbox run pitoolbox-mcp
```

De server werkt met elke MCP-client (Claude Desktop, Cursor, Open WebUI, ...). Pitoolbox is niet aan één LLM-provider gebonden. De agent-loop in fase 4 krijgt een provider-laag met OpenAI-compatibele endpoints (lokaal: Ollama, llama.cpp, LM Studio, vLLM; cloud: o.a. DeepSeek, Qwen, OpenAI) plus native adapters.

### Configuratie (omgevingsvariabelen)

| Variabele | Standaard |
|---|---|
| `PITOOLBOX_SCOPE_FILE` | `~/.config/pitoolbox/scope.yaml` |
| `PITOOLBOX_AUDIT_LOG` | `~/.local/state/pitoolbox/audit.jsonl` |
| `PITOOLBOX_CAPTURES_DIR` | `~/captures` |
| `PITOOLBOX_FLIPPER_PORT` | automatisch (USB 0483:5740) |
| `PITOOLBOX_FLIPPER_CLI_PROFILE` | `auto` (op basis van `firmware_origin_fork`) |
| `PITOOLBOX_ALFA_IFACE` / `PITOOLBOX_UPLINK_IFACE` | `wlan1` / `wlan0` |
| `PITOOLBOX_ALFA_DRIVER` | `mt7921u`: wifi-acties weigeren als de Alfa-interface een andere driver heeft (bescherming als namen verwisseld raken); leeg = uit |
| `PITOOLBOX_CONFIRM_TTL_S` | `60` |
| `PITOOLBOX_TOOL_ALLOWLIST` | leeg = alle tools |
| `PITOOLBOX_PUBLIC_URL` | geen (verplicht voor `--transport http`), bijv. `https://toolbox.mijndomein.nl` |
| `PITOOLBOX_HTTP_HOST` / `PITOOLBOX_HTTP_PORT` | `127.0.0.1` / `8765` (alleen loopback toegestaan) |
| `PITOOLBOX_AUTH_DB` | `~/.local/state/pitoolbox/auth.sqlite` |
| `PITOOLBOX_ACCESS_TOKEN_TTL_S` / `PITOOLBOX_REFRESH_TOKEN_TTL_S` | `900` / `604800` |
| `PITOOLBOX_EXTRA_REDIRECT_URIS` | leeg (alleen tijdelijk voor de MCP Inspector) |
| `PITOOLBOX_FLIPPER_TRANSPORT` | `auto` (eerst USB, anders BLE), of `usb` / `ble` |
| `PITOOLBOX_FLIPPER_BLE_ADDRESS` | geen (dan zoeken op naamprefix); aanbevolen om vast te zetten |
| `PITOOLBOX_FLIPPER_BLE_NAME_PREFIX` | `Flipper` |
| `PITOOLBOX_FLIPPER_KEEPALIVE_S` | `20` (alleen BLE; `0` = uit) |
| `PITOOLBOX_FLIPPER_BLE_ADAPTER` | leeg = standaardadapter van BlueZ (de eerste die aan staat), bijv. `hci1` |
| `PITOOLBOX_LLM_PROVIDER` | **geen** (verplicht voor de agent): `openai-compatible`, `anthropic` of `openai` |
| `PITOOLBOX_LLM_MODEL` | **geen** (verplicht voor de agent) |
| `PITOOLBOX_LLM_BASE_URL` | verplicht bij `openai-compatible`; anders het officiële endpoint van de provider |
| `PITOOLBOX_LLM_API_KEY` | geen; verplicht bij `anthropic`/`openai`, optioneel bij lokale servers |
| `PITOOLBOX_LLM_TIMEOUT_S` / `PITOOLBOX_LLM_MAX_RETRIES` / `PITOOLBOX_LLM_MAX_TOKENS` | `60` / `2` / `4096` |
| `PITOOLBOX_AGENT_MAX_STEPS` / `PITOOLBOX_AGENT_MAX_CALLS_PER_STEP` | `8` / `8`: tool-rondes per beurt, tool calls per modelantwoord |
| `PITOOLBOX_AGENT_TURN_TIMEOUT_S` / `PITOOLBOX_AGENT_MAX_RESULT_CHARS` | `300` / `8000`: tijd per beurt (tussen stappen gecontroleerd), lengte van een tool-resultaat naar het model |

**LLM-provider (fase 4).** Er is bewust geen standaardprovider: zonder `PITOOLBOX_LLM_PROVIDER` en `PITOOLBOX_LLM_MODEL` weigert de agent te starten. `openai-compatible` werkt met elke server met Chat Completions + tools, bijv. `http://127.0.0.1:11434/v1` (Ollama), `http://127.0.0.1:8080/v1` (llama.cpp), `http://127.0.0.1:1234/v1` (LM Studio), vLLM, `https://api.deepseek.com`, `https://api.moonshot.ai/v1`, `https://open.bigmodel.cn/api/paas/v4` en `https://api.minimax.io/v1`. `http://` mag alleen naar `localhost` of een IP-literal in loopback of privé-LAN; voor de rest is https verplicht. De sleutel staat alleen in `PITOOLBOX_LLM_API_KEY`, komt nooit in logs of foutmeldingen, en redirects worden niet gevolgd. Code: `src/pitoolbox/llm/` (httpx, geen vendor-SDK's; bronnen per provider in de moduledocstrings).

## Agent in de terminal (fase 4)

```bash
uv run pitoolbox-agent            # chat met het ingestelde LLM (PITOOLBOX_LLM_*)
```

Het model gebruikt dezelfde tools als de MCP-server, altijd via de ToolRunner (validatie, scope, policy, audit). **Een transmit voert het model nooit zelf uit.** `confirm_action` is voor het model onzichtbaar en wordt geweigerd als het hem toch aanroept, en het model ziet geen `pending_id`. Na de beurt toont de agent de samenvatting en vraagt `Bevestigen? [j/N]`. Alleen `j`/`ja` voert de actie uit, eenmalig en binnen 60 s. Prompt-injectie via tool-uitvoer (een SSID, bestandsinhoud) kan zo hooguit een bevestigingsvraag opleveren. Tools buiten `PITOOLBOX_TOOL_ALLOWLIST` krijgt het model niet te zien.

Commando's: `/stop` (noodstop), `/help`, `/exit`. Ctrl+C breekt alleen de lopende beurt af en is **geen** noodstop: captures lopen door, en een open bevestiging wordt afgewezen. Zonder geconfigureerde provider stopt `pitoolbox-agent` met exitcode 2. In de audit-log staat per beurt een `agent_turn_start`/`agent_turn_end` (model, tellingen, tokens), zonder berichtinhoud.

## Flipper draadloos via Bluetooth LE (fase 3)

`FlipperDevice` heeft één interface met twee transports: USB (serial) en BLE ([bleak](https://github.com/hbldh/bleak) op BlueZ). Over BLE gebruikt pitoolbox de seriële GATT-service van de Flipper (UUID's uit de firmwarebron `targets/f7/ble_glue/services/serial_service_uuid.inc`), met de flow control van de firmware: de host schrijft nooit meer dan de door de Flipper gemelde buffercredit.

> **Beperking:** over BLE opent de Flipper direct een RPC-sessie en is er **geen CLI** (`applications/services/bt/bt_service/bt.c`). Daarom werken over BLE alleen `system_info`, `storage_list`, `storage_read` en `ir_send`. `subghz_rx`, `nfc_read` en `rfid_read` hebben USB nodig: met `transport=auto` en een aangesloten kabel schakelt pitoolbox daar automatisch voor over, anders krijg je `unsupported_transport`.

```bash
uv run pitoolbox-flipper ble-scan              # Flippers in de buurt
uv run pitoolbox-flipper ble-pair AA:BB:..:FF  # voer de 6-cijferige code van het Flipper-scherm in
uv run pitoolbox-flipper ble-test --compare-usb  # system_info, storage_list, ping-latency, doorvoer
```

Koppelen gaat via een eigen BlueZ-agent over D-Bus (geen `bluetoothctl`, geen shell). Daarna zet je `PITOOLBOX_FLIPPER_BLE_ADDRESS`. Een gebonden Flipper wordt daarna zonder scan verbonden. Gemeten op een Pi 4: ~98 ms per RPC-frame en 1,2 KiB/s, tegenover 2 ms en 97 KiB/s over USB. Dat komt door het verbindingsinterval; sneller kan alleen met root (zie fase3-ble.md). Bij `auto` en `ble` wordt na een verbroken verbinding opnieuw verbonden, en een keepalive-ping houdt een idle BLE-link levend. Stap voor stap, met troubleshooting: [docs/fase3-ble.md](docs/fase3-ble.md).

## Remote via de Claude-app (fase 2)

```
Claude-app (telefoon/web) ──HTTPS──▶ Cloudflare ──tunnel──▶ cloudflared op de Pi
                                                              │
                                              http://127.0.0.1:8765  (pitoolbox-mcp --transport http)
                                                ├── /mcp                         MCP (streamable HTTP, Bearer vereist)
                                                ├── /.well-known/oauth-*         discovery (RFC 9728 / RFC 8414)
                                                ├── /register /authorize /token  OAuth 2.1 (DCR, PKCE S256, refresh-rotatie)
                                                └── /login                       wachtwoord + TOTP
```

- **Eén gebruiker.** Inloggen met wachtwoord (scrypt) en TOTP (RFC 6238); 5 mislukte pogingen geven 15 min lockout, en een TOTP-code werkt maar één keer.
- **Alleen Claude als client.** Redirect-URI's moeten exact `https://claude.ai/api/mcp/auth_callback` of `https://claude.com/api/mcp/auth_callback` zijn ([bron](https://claude.com/docs/connectors/building/authentication)).
- **Korte tokens.** Access 15 min, refresh 7 dagen met rotatie; hergebruik van een oude refresh-token trekt de hele familie in. Tokens worden alleen als SHA-256-hash in SQLite bewaard en zijn gebonden aan de resource `<public_url>/mcp` (RFC 8707).
- **Bevestigingen per OAuth-client.** Een `pending_id` van de ene client kan niet door een andere bevestigd worden.
- **Alleen loopback.** De server weigert te binden op iets anders dan 127.0.0.1/::1 en controleert de Host-header (alleen de publieke hostnaam en loopback).

Beheer:

```bash
uv run pitoolbox-auth init        # wachtwoord + TOTP instellen (toont otpauth-URI voor je authenticator)
uv run pitoolbox-auth status      # clients, actieve tokens, lockout
uv run pitoolbox-auth revoke-all  # alle tokens en clients intrekken
uv run pitoolbox-auth reset-totp
```

**Stap voor stap van Pi tot werkende connector (cloudflared, systemd, connector toevoegen, testen met de MCP Inspector, troubleshooting): [docs/fase2-remote.md](docs/fase2-remote.md).** Deploybestanden: [deploy/](deploy/).

Connector toevoegen, kort: op claude.ai via **Customize → Connectors → Add custom connector**, URL `https://<jouw-domein>/mcp`. Log daarna in op de pitoolbox-loginpagina; controleer dat er `claude.ai` als bestemming staat. Een connector die je op het web toevoegt, is ook beschikbaar in de mobiele app.

## Klaar voor onderweg (fase 5)

`deploy/install.sh` installeert zonder opties alleen de service, net als in fase 2. Met `--onderweg` zet het ook de systeemconfig voor gebruik zonder ethernet neer:

- vaste interfacenamen via systemd `.link`: ingebouwde wifi = `wlan0` (uplink), Alfa = `wlan1`, gematcht op driver en USB-id, niet op MAC;
- NetworkManager beheert de Alfa niet meer (`unmanaged-devices+=driver:mt7921u`), zodat monitor mode blijft staan;
- logrotate voor de audit-log (hernoemen, geen `copytruncate`, omdat de log per regel opnieuw wordt geopend);
- een tmpfiles-regel voor de capture-map (standaard wordt er nooit iets opgeruimd).

`--bluez-le` zet optioneel een korter BLE-verbindingsinterval in `/etc/bluetooth/main.conf` (niet getest). Kijk eerst met `--dry-run` wat er verandert. Bestanden die al bestaan en anders zijn, gaan eerst naar `/var/backups/pitoolbox/`.

```bash
deploy/install.sh --dry-run --onderweg                # niets schrijven, alleen tonen (geen root)
sudo deploy/install.sh --alleen-systeem --onderweg   # alleen fase 5, daarna: sudo reboot
```

Ontwerpkeuzes met bronnen, controle na de herstart, terugdraaien en de hardware-checklist: [docs/fase5-onderweg.md](docs/fase5-onderweg.md).

## Structuur

```
src/pitoolbox/
  app.py                      # bouwt ToolRunner uit Settings
  config.py
  core/
    scope.py policy.py audit.py errors.py proc.py
    devices/flipper.py        # FlipperDevice: USB of BLE achter één interface
    devices/_flipper_ble.py   # BLE-transport (bleak), serial-service-UUID's, flow control
    devices/_ble_pairing.py   # BlueZ-agent (D-Bus) voor koppelen met PIN
    devices/_flipper_rpc/     # framing, RPC-client, gegenereerde protobuf (pb/)
    devices/_flipper_cli.py   # vaste CLI-sjablonen + parsers per firmwareprofiel
    devices/wifi.py           # AlfaAdapter, CaptureManager, parsers
    devices/system.py         # get_throttled, uplink, schijf
    tools/                    # base, runner, flipper_tools, wifi_tools, system_tools
  frontends/mcp_server.py     # MCPServer (opvolger van FastMCP in mcp 2.x), stdio + HTTP
  auth/                       # OAuth 2.1: provider, SQLite-store, /login, crypto, CLI
  flipper_cli.py              # pitoolbox-flipper: ble-scan/pair/unpair/list/test
deploy/                       # install.sh, env-voorbeeld, en per doel een map:
  systemd/ cloudflared/       #   service-units, tunnelconfig (fase 2)
  network/                    #   .link-bestanden: wlan0 = ingebouwd, wlan1 = Alfa (fase 5)
  NetworkManager/             #   drop-in: Alfa unmanaged (fase 5)
  logrotate/ tmpfiles/        #   audit-log-rotatie, beleid capture-map (fase 5)
  bluetooth/                  #   optioneel BLE-interval voor main.conf (fase 5, niet getest)
docs/fase2-remote.md          # installatie remote + troubleshooting
docs/fase3-ble.md             # Flipper via Bluetooth: koppelen, testen, troubleshooting
docs/fase4-spike.md           # onderzoek Flipper als bediening (nog niet gebouwd)
docs/fase5-onderweg.md        # klaar voor onderweg: installeren, controleren, terugdraaien
scripts/gen_proto.py          # protobuf opnieuw genereren
tests/
```

## Hardware-testchecklist fase 3

Volledige lijst in [docs/fase3-ble.md](docs/fase3-ble.md). In het kort:

- [ ] `ble-scan` vindt de Flipper; `ble-pair` vraagt de code van het Flipper-scherm en koppelt
- [ ] `ble-test`: system_info en storage_list via BLE; noteer ping-latency en doorvoer (KiB/s) en vergelijk met `--compare-usb`
- [ ] Haalt BlueZ op de Pi een grote MTU, of blijft het bij 20 bytes per write (traag maar werkend)?
- [ ] `ir_send` via BLE (bevestigen → zendt)
- [ ] `subghz_rx` met `transport=auto`: zonder kabel `unsupported_transport`, met kabel automatisch via USB
- [ ] Flipper buiten bereik en terug: de volgende call verbindt opnieuw; keepalive houdt een idle link levend
- [ ] De service als gebruiker `pitoolbox` (groep `bluetooth`) kan BLE gebruiken zonder D-Bus-fouten

## Hardware-testchecklist fase 2

- [ ] `pitoolbox-auth init`: de otpauth-URI werkt in je authenticator-app, en `timedatectl` toont `System clock synchronized: yes`
- [ ] `curl -si https://<domein>/mcp` → `401` met `resource_metadata` (van buiten je netwerk)
- [ ] `/.well-known/oauth-protected-resource/mcp` en `/.well-known/oauth-authorization-server` → JSON
- [ ] De connector toevoegen op claude.ai → loginpagina toont `claude.ai` → na inloggen zie je de tools
- [ ] Vanaf de telefoon: `status` aanroepen en een `ir_send` voorstellen → bevestigen in het gesprek → zendt
- [ ] Na 15 min nog steeds verbonden (refresh werkt); na `pitoolbox-auth revoke-all` moet je opnieuw inloggen
- [ ] `systemctl status pitoolbox-mcp cloudflared-pitoolbox`; `grep CapAmb /proc/$(pidof -s python3)/status` toont `0000000000003000`
- [ ] Werkt `wifi_scan`/`alfa_set_mode` onder systemd (ambient caps, geen root)?
- [ ] Langste tool (`subghz_rx` van 100 s) haalt de Cloudflare-timeout van 125 s

## Hardware-testchecklist fase 1

Alles hieronder is alleen getest tegen nagebootste apparaten. Op de Pi zelf controleren:

**Flipper (USB)**
- [ ] `system_info` geeft `firmware_origin_fork`, en het juiste CLI-profiel wordt gekozen
- [ ] `storage_list /ext` en `storage_read` van een klein `.ir`-bestand werken
- [ ] Omschakelen RPC ↔ CLI en terug, meerdere keren achter elkaar (bijv. `system_info` → `rfid_read` → `system_info`)
- [ ] `subghz_rx` op 433.92 MHz met een eigen afstandsbediening: klopt `packet_count`, en worden pakketten goed gesplitst? (de splitsheuristiek is niet uit de bron geverifieerd)
- [ ] `rfid_read` met een eigen tag: protocol, hex en infotekst
- [ ] `nfc_read` met en zonder tag (gedrag na Ctrl+C zonder tag); weigert de Flipper als de NFC-app open staat?
- [ ] `ir_send` op je eigen tv: bevestiging → zendt → app sluit weer
- [ ] `emergency_stop` tijdens een `subghz_rx` van 60 s: stopt binnen ~1 s
- [ ] USB eruit en erin tijdens gebruik: de volgende call verbindt opnieuw

**Alfa AWUS036AXML (mt7921u)**
- [ ] `alfa_set_mode monitor` / `managed`. Gooit NetworkManager roet in het eten? (oplossing: `install.sh --onderweg`, zie [docs/fase5-onderweg.md](docs/fase5-onderweg.md))
- [ ] `wifi_scan iw` (managed) en `wifi_scan airodump` (monitor), ook op 5 GHz
- [ ] `capture_start` op je eigen BSSID: bevat de pcap **alleen** dat BSSID? (`--bssid` is in de docs alleen als weergavefilter beschreven)
- [ ] `capture_stop` levert een geldige `.cap` op (opent in Wireshark)
- [ ] Rechten: werkt het met `CAP_NET_ADMIN`/`CAP_NET_RAW`, of is root nodig?

**Pi**
- [ ] `status`: klopt `vcgencmd get_throttled` (gebruiker in de groep `video`), en worden uplink en schijf goed gerapporteerd?

Bronnen en licenties: [THIRD_PARTY.md](THIRD_PARTY.md).
