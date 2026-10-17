# pitoolbox

Software voor een Raspberry Pi 4 of 5 (Kali Linux ARM64) die een **Flipper Zero** en een **Alfa AWUS036AXML** aanstuurt via een LLM, voor je eigen lab en apparaten.

Je praat met een model (Claude via de app, of een ander LLM via de agent in de terminal). Het model roept benoemde tools aan op de Pi: wifi scannen, een capture starten, Sub-GHz ontvangen, een IR-knop zenden. De Pi controleert elke aanroep tegen jouw scope en vraagt een mens om bevestiging voordat er iets zendt.

Deze README neemt je mee van een lege Pi tot een werkende installatie. Details staan in [docs/](docs/).

## Inhoud

- [Status](#status)
- [Veiligheidsmodel](#veiligheidsmodel)
- [Tools](#tools)
- [Hardware](#hardware)
- [Installatie van nul](#installatie-van-nul)
- [Gebruik](#gebruik)
- [Configuratie](#configuratie)
- [Problemen oplossen](#problemen-oplossen)
- [Ontwikkelen](#ontwikkelen)
- [Hardware-testchecklists](#hardware-testchecklists)

## Status

Alle vijf fases zijn gebouwd en getest met nagebootste apparaten (`uv run pytest`). Op echte hardware is nog niet alles getest. Volledige stand: [docs/VOORTGANG.md](docs/VOORTGANG.md).

| Onderdeel | Op hardware getest? |
|---|---|
| Flipper over USB (Momentum `mntm-012`) | ja: `system_info`, storage, `subghz_rx` op 433,92 en 290 MHz, `rfid_read` en `nfc_read` **zonder** tag, wisselen tussen RPC en CLI. Nog niet: een echte tag of afstandsbediening, `ir_send` |
| Flipper over Bluetooth LE | ja: koppelen, `system_info`, storage, ping en doorvoer, keepalive, herverbinden na een verbroken link (valtest 20/20). Nog niet: `ir_send` |
| Alfa (wifi-tools) | **nee** (heeft root of capabilities nodig) |
| Remote via de Claude-app (fase 2) | lokaal op de Pi end-to-end getest (HTTP + volledige OAuth-flow + MCP op 127.0.0.1); tunnel en claude.ai-connector nog niet |
| Agent en LLM-providers (fase 4) | end-to-end als echt proces tegen nagebootste servers van de drie API-formaten (62/64; de 2 afwijkingen lagen aan de stub, nagekeken in de OpenAI-docs). Nog niet tegen de echte API's of Ollama |
| Flipper-app `pitoolbox_ui` en de bridge (fase 4) | ja, over USB en BLE: installeren (`ui-install`), rooktest (`ui-test`), valtest 20/20 (`ui-droptest`), 5 minuten open zonder verbreking, OK/Back op een echte vraag (`ui-test --confirm-check`, door Stijn), handmatige start, alle snelle acties en het statusscherm (met de bediening op afstand, 2026-10-17). Nog niet: presets vanuit het menu |
| Systeemconfig voor onderweg (fase 5) | **nee**: alleen de bestanden, `--dry-run` en de controles zonder root |

## Veiligheidsmodel

```
frontend (MCP stdio of HTTP, pitoolbox-agent, Flipper-app)
        │  runner.call(frontend, tool, args)
        ▼
ToolRunner ── valideren (pydantic, extra velden = fout)
        ├── Policy: allowlist → scope.yaml → risicoklasse
        ├── transmit binnen scope → pending_id → bevestigen door een mens
        ├── uitvoeren (FlipperDevice / AlfaAdapter / CaptureManager / SystemProbe)
        └── AuditLog: één JSON-regel per call
```

- **Geen generieke shell, nergens.** Alleen benoemde tools met een vast argumentmodel. Subprocessen lopen via één runner met een allowlist (`iw`, `ip`, `airodump-ng`, `vcgencmd`), altijd als argv-lijst en nooit met `shell=True`. De Flipper-CLI wordt alleen gebruikt met vaste sjablonen die uit gevalideerde getallen worden opgebouwd.
- **Scope.** `scope.yaml` (voorbeeld: [scope.example.yaml](scope.example.yaml)) noemt jouw eigen BSSID's (met kanaal) en IR-bestanden. Het bestand wordt live herladen. Is het ongeldig of ontbreekt het, dan geldt een lege scope (fail closed).
- **Risicoklassen:**

  | Klasse | Betekenis | Regel |
  |---|---|---|
  | `read` | scannen/lezen | toegestaan (met validatie, zoals frequentiegrenzen) |
  | `capture` | opslaan, adapter omzetten | alleen doelen uit `scope.yaml` |
  | `transmit` | alles wat zendt | buiten scope altijd geweigerd; binnen scope eerst een `pending_id`, daarna een expliciete bevestiging |
  | `control` | `emergency_stop`, `confirm_action` | altijd toegestaan |

- **Bevestiging (transmit).** De tool voert niets uit. Hij geeft `confirmation_required`, een `pending_id` en een samenvatting terug. Pas `confirm_action(pending_id)` voert de actie uit. De bevestiging werkt één keer, verloopt na 60 s, is gebonden aan de frontend (bij HTTP: per OAuth-client), en bij het bevestigen wordt de scope opnieuw gecontroleerd.
- **Flipper verplicht.** Zolang de Flipper-app verbonden is, bevestigt alleen OK op de Flipper een transmit, uit welke frontend die ook komt. Zie [Bediening op de Flipper](#bediening-op-de-flipper).
- **Een model bevestigt nooit zelf.** In `pitoolbox-agent` ziet het model `confirm_action` niet en ook geen `pending_id`. Alleen jij typt `j`.
- **Audit.** Elke toolcall komt als één JSON-regel in de audit-log, ook geweigerde calls, bevestigingen en het koppelen van de Flipper-app.

## Tools

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
| `capture_start` / `capture_stop` | capture | pcap van een BSSID uit scope, op het kanaal uit scope, naar `<captures>/<sessie>/` |
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

> **Over Bluetooth is er geen CLI.** Over BLE opent de Flipper direct een RPC-sessie (`applications/services/bt/bt_service/bt.c`). Daarom werken over BLE alleen `system_info`, `storage_list`, `storage_read` en `ir_send`. `subghz_rx`, `nfc_read` en `rfid_read` hebben USB nodig: met `transport=auto` en een aangesloten kabel schakelt pitoolbox daar automatisch voor over, anders krijg je `unsupported_transport`.

## Hardware

| Onderdeel | Opmerkingen |
|---|---|
| Raspberry Pi 4 of 5 | Kali Linux ARM64. Getest op een Pi 4 Model B. De ingebouwde wifi is de uplink (`wlan0`), de ingebouwde Bluetooth praat met de Flipper. Gebruik een goede voeding: `status` meldt onderspanning. |
| Flipper Zero | Met SD-kaart. Getest met Momentum `mntm-012`; voor de officiële firmware 1.5.x is er een profiel, alleen tegen de bron getest. Via USB-kabel (alle tools) of Bluetooth LE (alleen de RPC-tools). |
| Alfa AWUS036AXML | Chip MT7921AUN, in-kerneldriver `mt7921u`, USB-id `0e8d:7961`. Wordt `wlan1`. De Alfa heeft ook een eigen Bluetooth-controller, maar die start op de test-Pi niet (`Opcode 0x0c03 failed` in `dmesg`). Gebruik voor de Flipper de ingebouwde Bluetooth. |

Voor remote gebruik via de Claude-app heb je daarnaast nodig: een domein met de DNS bij Cloudflare, een authenticator-app (TOTP) en een Claude-account. Voor onderweg: een wifi-netwerk voor `wlan0`, bijvoorbeeld de hotspot van je telefoon.

## Installatie van nul

Er zijn twee plekken waar pitoolbox staat:

- **je checkout** (bijv. `~/pitoolbox`): om te testen, te koppelen en lokaal te gebruiken met `uv run …`;
- **de service** in `/opt/pitoolbox`, neergezet door `deploy/install.sh`. Die draait als gebruiker `pitoolbox` met `pitoolbox-mcp --transport http`, dus altijd remote, achter een Cloudflare Tunnel.

Wil je alleen lokaal werken (stdio of de agent in de terminal), dan heb je stap 7 en 8 niet nodig.

### 1. Kali en systeempakketten

Installeer Kali Linux ARM64 voor de Raspberry Pi en log in met je eigen gebruiker. Daarna:

```bash
sudo apt update
sudo apt install git curl iw iproute2 aircrack-ng bluez
sudo systemctl enable --now bluetooth
sudo usermod -aG dialout,video,bluetooth "$USER"   # daarna uit- en inloggen
```

- `dialout`: de Flipper via USB (`/dev/ttyACM0`). `video`: `vcgencmd`. `bluetooth`: koppelen en BLE via D-Bus.
- `vcgencmd` (voor `status`): controleer met `command -v vcgencmd`. Op de test-Pi stond hij al in `/usr/local/bin`. De docs noemen het pakket `libraspberrypi-bin`; of dat op Kali zo heet, is niet gecontroleerd.

### 2. uv en de repo

Kali blokkeert systeem-pip, dus alles gaat via [uv](https://docs.astral.sh/uv/) in een venv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # daarna een nieuwe shell openen
git clone https://github.com/stijnvandepol/PiToolbox ~/pitoolbox
cd ~/pitoolbox
git checkout fase-1        # het werk staat op deze branch
uv sync
uv run pytest              # draait zonder hardware; alles hoort te slagen
```

Python 3.11 of nieuwer is nodig. Heeft het systeem een oudere versie, dan haalt uv er zelf een.

### 3. Scope

```bash
mkdir -p ~/.config/pitoolbox
cp scope.example.yaml ~/.config/pitoolbox/scope.yaml
```

Vul je **eigen** apparaten in: BSSID en kanaal van je eigen access points, en de `.ir`-bestanden (pad op de Flipper-SD) met de knoppen die mogen. Alles wat er niet in staat, is buiten scope.

### 4. Flipper via USB

Sluit de Flipper met USB aan. Sluit qFlipper en andere programma's die de seriële poort gebruiken. Test met de [MCP Inspector](https://github.com/modelcontextprotocol/inspector) (heeft Node nodig):

```bash
npx @modelcontextprotocol/inspector uv --directory ~/pitoolbox run pitoolbox-mcp
```

Roep `system_info` aan. In het antwoord staat `firmware_origin_fork`, en daarop kiest pitoolbox het CLI-profiel. Probeer daarna `storage_list` op `/ext`.

### 5. Alfa

Steek de Alfa in een USB-poort en controleer de namen en drivers:

```bash
ip -br link
for i in wlan0 wlan1; do echo "$i: $(basename "$(readlink -f /sys/class/net/$i/device/driver)")"; done
# verwacht: wlan0: brcmfmac   wlan1: mt7921u
```

`alfa_set_mode` en `wifi_scan` met `airodump-ng` hebben `CAP_NET_ADMIN` en `CAP_NET_RAW` nodig. Voor handmatig testen kan dat met `sudo`. De service krijgt die capabilities via `AmbientCapabilities` en draait niet als root. Zolang NetworkManager de Alfa beheert, kan hij monitor mode terugzetten. Stap 7 (`--onderweg`) lost dat op.

### 6. Flipper koppelen via Bluetooth

Zet op de Flipper Bluetooth aan (Settings → Bluetooth → `Bluetooth: ON`) en zorg dat hij niet met een telefoon verbonden is. Dan:

```bash
uv run pitoolbox-flipper ble-scan                    # Flippers in de buurt, met adres
uv run pitoolbox-flipper ble-pair AA:BB:CC:DD:EE:FF  # typ de 6-cijferige code van het Flipper-scherm
uv run pitoolbox-flipper ble-test --address AA:BB:CC:DD:EE:FF --compare-usb
```

`ble-pair` drukt aan het eind de regel `PITOOLBOX_FLIPPER_BLE_ADDRESS=…` af. Zet die in je shell-omgeving en, na stap 7, in `/etc/pitoolbox/pitoolbox.env`. De koppeling zit in BlueZ en geldt voor het hele systeem, dus de service kan hem ook gebruiken.

Heeft de Pi meer dan één Bluetooth-adapter (zoals met de Alfa), kies dan met `--adapter hci1` of `PITOOLBOX_FLIPPER_BLE_ADAPTER` de ingebouwde. `bluetoothctl list` toont de adapters.

Koppelen gaat via een eigen BlueZ-agent over D-Bus (geen `bluetoothctl`, geen shell). Gemeten op een Pi 4: ~98 ms per RPC-frame en 1,2 KiB/s, tegenover 2 ms en 97 KiB/s over USB. Dat komt door het verbindingsinterval; sneller kan alleen met root. Stap voor stap, met alle commando's en troubleshooting: [docs/fase3-ble.md](docs/fase3-ble.md).

### 7. De service installeren (`deploy/install.sh`)

Kijk eerst wat de systeemconfig voor onderweg zou veranderen. Dit schrijft niets en heeft geen root nodig:

```bash
deploy/install.sh --dry-run --onderweg
```

Installeer dan de service en de systeemconfig in één keer:

```bash
sudo deploy/install.sh --onderweg
```

| Optie | Wat |
|---|---|
| (geen) | alleen de service: gebruikers `pitoolbox` en `cloudflared`, `/opt/pitoolbox`, `/etc/pitoolbox`, `/var/lib/pitoolbox`, uv, `uv sync --frozen --no-dev`, systemd-units |
| `--onderweg` | ook: vaste interfacenamen (`wlan0` = ingebouwd, `wlan1` = Alfa, via systemd `.link` op driver en USB-id), NetworkManager laat de Alfa met rust, logrotate voor de audit-log, tmpfiles-regel voor de capture-map |
| `--bluez-le` | ook: korter BLE-verbindingsinterval in `/etc/bluetooth/main.conf` (optioneel, **niet getest**) |
| `--alleen-systeem` | sla de service over, doe alleen de gekozen `--onderweg`/`--bluez-le`-stappen |
| `--dry-run` | niets schrijven, per bestand de diff tonen; impliceert `--alleen-systeem`, geen root nodig |
| `-y`, `--ja` | niet om bevestiging vragen (anders vraagt het script per stap) |

Het script is idempotent. Het overschrijft nooit je `/etc/pitoolbox/pitoolbox.env`, `/etc/pitoolbox/scope.yaml`, de auth-database, de audit-log of captures. Een systeembestand dat al bestaat en anders is, gaat eerst naar `/var/backups/pitoolbox/<tijdstempel>/`. Het script enablet of start niets, en installeert of configureert cloudflared niet. Met `PITOOLBOX_SKIP_UV_INSTALL=1` of `PITOOLBOX_UV_BIN=/pad/uv` regel je uv zelf.

Vul daarna de config van de service in (als root; `pitoolbox.env` is 0600):

```bash
sudoedit /etc/pitoolbox/scope.yaml        # je eigen apparaten, zoals in stap 3
sudoedit /etc/pitoolbox/pitoolbox.env     # PITOOLBOX_PUBLIC_URL, PITOOLBOX_FLIPPER_BLE_ADDRESS, ...
```

`pitoolbox.env` is een systemd-`EnvironmentFile`: `KEY=waarde`, geen `export`, geen `~` of `$HOME`.

### 8. Remote: tunnel, login en connector

Kort (alles staat in [docs/fase2-remote.md](docs/fase2-remote.md)):

1. cloudflared installeren, tunnel aanmaken en DNS koppelen (`cloudflared tunnel login`, `create`, `route dns`);
2. tunnelconfig en credentials in `/etc/cloudflared/` zetten;
3. `PITOOLBOX_PUBLIC_URL=https://<jouw-hostnaam>` in `/etc/pitoolbox/pitoolbox.env`;
4. wachtwoord en TOTP instellen, als gebruiker `pitoolbox`:

   ```bash
   sudo -u pitoolbox bash -c 'set -a; . /etc/pitoolbox/pitoolbox.env; set +a; exec /opt/pitoolbox/.venv/bin/pitoolbox-auth init'
   ```

5. starten en controleren:

   ```bash
   sudo systemctl enable --now pitoolbox-mcp
   curl -i http://127.0.0.1:8765/mcp           # verwacht: 401
   sudo systemctl enable --now cloudflared-pitoolbox
   ```

6. de connector toevoegen in Claude (zie [Remote via de Claude-app](#remote-via-de-claude-app)).

In de docs en in `deploy/` staat overal de placeholder `toolbox.example.nl`. Vervang die door je eigen hostnaam.

### 9. Klaar voor onderweg

Na `--onderweg` (stap 7):

1. Zorg dat `wlan0` een wifi-profiel heeft voor onderweg, bijvoorbeeld de hotspot van je telefoon: `nmcli device wifi connect <SSID> ifname wlan0 --ask`. Zonder dat profiel heb je zonder ethernet geen netwerk.
2. Laat `PITOOLBOX_UPLINK_IFACE` niet op `eth0` staan (ook niet in je shell-profiel). Onderweg is de uplink `wlan0`, de standaard.
3. `sudo reboot`.
4. Controleer de namen, NetworkManager, logrotate en captures volgens [docs/fase5-onderweg.md §4](docs/fase5-onderweg.md#4-controleren-na-de-herstart).

Daar staan ook de ontwerpkeuzes met bronnen, het terugdraaien en wat je doet als de namen toch verwisselen.

### 10. De Flipper-app (optioneel)

De app `pitoolbox_ui` bouw je met ufbt, tegen de SDK van exact de firmware op je Flipper. Daarna zet `uv run pitoolbox-flipper ui-install` hem op de SD-kaart in `/ext/apps/Tools/pitoolbox_ui.fap` (gecontroleerd op API en MD5), en test `uv run pitoolbox-flipper ui-test` hem van begin tot eind. Met `ui-test --confirm-check` druk je zelf OK en Back op twee test-vragen; dat zendt nooit iets uit. Bouwen en installeren: [flipper_app/pitoolbox_ui/README.md](flipper_app/pitoolbox_ui/README.md). Daarna `PITOOLBOX_FLIPPER_UI=on` in `/etc/pitoolbox/pitoolbox.env` en `sudo systemctl restart pitoolbox-mcp`. Zie [Bediening op de Flipper](#bediening-op-de-flipper).

## Gebruik

### Lokaal als MCP-server (stdio)

```bash
uv run pitoolbox-mcp --scope ~/.config/pitoolbox/scope.yaml
```

Opties: `--transport stdio|http` (standaard `stdio`), `--scope <pad>` en `--audit-log <pad>`. Die laatste twee overschrijven `PITOOLBOX_SCOPE_FILE` en `PITOOLBOX_AUDIT_LOG`.

Testen met de MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv --directory ~/pitoolbox run pitoolbox-mcp
```

De server werkt met elke MCP-client (Claude Desktop, Cursor, Open WebUI, ...). Pitoolbox is niet aan één LLM-provider gebonden.

### Remote via de Claude-app

```
Claude-app (telefoon/web) ──HTTPS──▶ Cloudflare ──tunnel──▶ cloudflared op de Pi
                                                              │
                                              http://127.0.0.1:8765  (pitoolbox-mcp --transport http)
                                                ├── /mcp                         MCP (streamable HTTP, Bearer vereist)
                                                ├── /.well-known/oauth-*         discovery (RFC 9728 / RFC 8414)
                                                ├── /register /authorize /token  OAuth 2.1 (DCR, PKCE S256, refresh-rotatie)
                                                └── /login                       wachtwoord + TOTP
```

- **Eén gebruiker.** Inloggen met wachtwoord (scrypt) en TOTP (RFC 6238). 5 mislukte pogingen geven 15 min lockout, en een TOTP-code werkt maar één keer.
- **Alleen Claude als client.** Redirect-URI's moeten exact `https://claude.ai/api/mcp/auth_callback` of `https://claude.com/api/mcp/auth_callback` zijn ([bron](https://claude.com/docs/connectors/building/authentication)).
- **Korte tokens.** Access 15 min, refresh 7 dagen met rotatie. Hergebruik van een oude refresh-token trekt de hele familie in. Tokens worden alleen als SHA-256-hash in SQLite bewaard en zijn gebonden aan de resource `<public_url>/mcp` (RFC 8707).
- **Bevestigingen per OAuth-client.** Een `pending_id` van de ene client kan niet door een andere bevestigd worden.
- **Alleen loopback.** De server weigert te binden op iets anders dan 127.0.0.1/::1 en controleert de Host-header (alleen de publieke hostnaam en loopback).

Connector toevoegen: op claude.ai via **Customize → Connectors → Add custom connector**, URL `https://<jouw-domein>/mcp`. Log daarna in op de pitoolbox-loginpagina en controleer dat er `claude.ai` als bestemming staat. Een connector die je op het web toevoegt, is ook beschikbaar in de mobiele app. Laat tools die zenden of capturen in Claude op **Needs approval** staan. Details en troubleshooting: [docs/fase2-remote.md](docs/fase2-remote.md).

Beheer (in de checkout met `uv run`; voor de service als gebruiker `pitoolbox`, zoals in stap 8):

```bash
uv run pitoolbox-auth init        # wachtwoord + TOTP instellen (toont otpauth-URI voor je authenticator); --force vervangt
uv run pitoolbox-auth status      # clients, actieve tokens, lockout
uv run pitoolbox-auth revoke-all  # alle tokens en clients intrekken
uv run pitoolbox-auth reset-totp
```

### Agent in de terminal

```bash
uv run pitoolbox-agent            # chat met het ingestelde LLM (PITOOLBOX_LLM_*)
```

Opties: `--scope <pad>`, `--audit-log <pad>`, `-v`/`--verbose`, `--list-providers` (geldige waarden van `PITOOLBOX_LLM_PROVIDER`).

Het model gebruikt dezelfde tools als de MCP-server, altijd via de ToolRunner (validatie, scope, policy, audit). **Een transmit voert het model nooit zelf uit.** `confirm_action` is voor het model onzichtbaar en wordt geweigerd als het hem toch aanroept, en het model ziet geen `pending_id`. Na de beurt toont de agent de samenvatting en vraagt `Bevestigen? [j/N]`. Alleen `j`/`ja` voert de actie uit, eenmalig en binnen 60 s. Prompt-injectie via tool-uitvoer (een SSID, bestandsinhoud) kan zo hooguit een bevestigingsvraag opleveren. Tools buiten `PITOOLBOX_TOOL_ALLOWLIST` krijgt het model niet te zien.

Is de Flipper-app gekoppeld, dan vraagt de agent niet zelf: de vraag staat dan op de Flipper.

Commando's: `/stop` (noodstop), `/help`, `/exit`. Ctrl+C breekt alleen de lopende beurt af en is **geen** noodstop: captures lopen door, en een open bevestiging wordt afgewezen. Zonder geconfigureerde provider stopt `pitoolbox-agent` met exitcode 2. In de audit-log staat per beurt een `agent_turn_start`/`agent_turn_end` (model, tellingen, tokens), zonder berichtinhoud.

**LLM-provider.** Er is bewust geen standaardprovider en geen standaardmodel: zonder `PITOOLBOX_LLM_PROVIDER` en `PITOOLBOX_LLM_MODEL` weigert de agent te starten. `PITOOLBOX_LLM_PROVIDER` is een **preset** (naam van een provider) of een van de drie wire-formaten. `uv run pitoolbox-agent --list-providers` toont ze allemaal.

| `PITOOLBOX_LLM_PROVIDER` | Endpoint (standaard) | Sleutel | Voorbeeldmodel (uit de docs, 2026-10) |
|---|---|---|---|
| `openai-compatible` | zelf zetten via `PITOOLBOX_LLM_BASE_URL` | optioneel | |
| `anthropic` | `https://api.anthropic.com` (Messages API) | verplicht | |
| `openai` | `https://api.openai.com/v1` (Responses API) | verplicht | |
| `deepseek` | `https://api.deepseek.com` | verplicht | `deepseek-flash` |
| `qwen` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` (Singapore) | verplicht | `qwen3.7-plus` |
| `kimi` (alias `moonshot`) | `https://api.moonshot.ai/v1` | verplicht | `kimi-k2.6` |
| `glm` (alias `zhipu`) | `https://api.z.ai/api/paas/v4` | verplicht | `glm-5.3` |
| `minimax` | `https://api.minimax.io/v1` | verplicht | `MiniMax-M3` |
| `mistral` | `https://api.mistral.ai/v1` | verplicht | `mistral-small-latest` |
| `groq` | `https://api.groq.com/openai/v1` | verplicht | `llama-3.3-70b-versatile` |
| `openrouter` | `https://openrouter.ai/api/v1` | verplicht | `google/gemini-3-flash-preview` |
| `xai` | `https://api.x.ai/v1` | verplicht | `grok-4.7` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | verplicht | `gemini-3.8-flash` |
| `ollama` | `http://127.0.0.1:11434/v1` | optioneel | |
| `lmstudio` | `http://localhost:1234/v1` | optioneel | |
| `llamacpp` | `http://127.0.0.1:9931/v1` (`llama-server`, standaardpoort 9931) | optioneel | |
| `vllm` | `http://localhost:8000/v1` (start met `--enable-auto-tool-choice --tool-call-parser ...`) | optioneel | |

De voorbeeldmodellen zijn alleen voorbeelden; er is geen standaard, kies zelf een model met tool calling. Alle presets behalve `anthropic` en `openai` gebruiken de OpenAI-compatibele adapter (Chat Completions). Endpoints en afwijkingen komen uit de officiële docs; de bron staat per preset in `src/pitoolbox/llm/presets.py`.

`PITOOLBOX_LLM_BASE_URL` overschrijft het endpoint van een preset, bijvoorbeeld voor China: `https://dashscope.aliyuncs.com/compatible-mode/v1` (Qwen, Beijing; of je eigen workspace-URL `https://{WorkspaceId}.<regio>.maas.aliyuncs.com/compatible-mode/v1`), `https://api.moonshot.cn/v1` (Kimi), `https://open.bigmodel.cn/api/paas/v4` (GLM/Zhipu) en `https://api.minimax.cn/v1` (MiniMax). Een sleutel werkt meestal alleen in de regio waar je hem maakte. Bij een cloudpreset (sleutel verplicht) moet een eigen URL https zijn, behalve naar loopback (de sleutel verlaat de Pi dan niet; bijv. `openai` tegen een lokale Ollama of proxy); bij een lokale preset mag http naar `localhost` of een IP-literal in loopback of privé-LAN (zoals bij `openai-compatible`), maar alleen **zonder** sleutel: zet je een sleutel, dan is http alleen naar loopback toegestaan. De sleutel staat alleen in `PITOOLBOX_LLM_API_KEY`, komt nooit in logs of foutmeldingen, en redirects worden niet gevolgd. Code: `src/pitoolbox/llm/` (httpx, geen vendor-SDK's).

Bekende beperkingen: denkende modellen tellen hun redenering vaak mee in `max_tokens` (DeepSeek, Kimi, MiniMax, GLM); zet `PITOOLBOX_LLM_MAX_TOKENS` dan ruimer (Kimi adviseert minstens 16000). Open-source Qwen-modellen op DashScope (bijv. `qwen3.5-27b`) denken alleen met streaming en geven hier een fout; kies een commercieel Qwen-model. Quotum- of saldo-429's worden net als gewone 429's maximaal `PITOOLBOX_LLM_MAX_RETRIES` keer opnieuw geprobeerd. Fouten die een provider in een HTTP 200-antwoord meldt (OpenRouter `error`, MiniMax `base_resp`, `finish_reason` `error`/`network_error`) gelden als de bijbehorende HTTP-fout, met dezelfde retries; MiniMax' saldo- en 5-uurslimietcodes (1008, 2056) falen direct.

Voorbeeld met Ollama op de Pi of in je LAN:

```bash
export PITOOLBOX_LLM_PROVIDER=ollama          # of: openai-compatible + PITOOLBOX_LLM_BASE_URL
export PITOOLBOX_LLM_MODEL=<model>
uv run pitoolbox-agent
```

Voorbeeld met DeepSeek:

```bash
export PITOOLBOX_LLM_PROVIDER=deepseek
export PITOOLBOX_LLM_MODEL=<model>             # bijv. deepseek-flash
export PITOOLBOX_LLM_API_KEY=<sleutel>
uv run pitoolbox-agent
```

### Bediening op de Flipper

De Flipper-app `pitoolbox_ui` ([flipper_app/pitoolbox_ui](flipper_app/pitoolbox_ui/README.md)) toont bevestigingsvragen, snelle acties (status, wifi-scan, captures stoppen, noodstop), een statusscherm en presets. Op de Pi draait de bridge in het proces dat de Flipper bezit: zet `PITOOLBOX_FLIPPER_UI=on` in **één** proces (normaal de service `pitoolbox-mcp`). Twee processen met de Flipper verbreken elkaars BLE-link.

**Flipper verplicht.** Zolang de app verbonden is, bevestigt alleen OK op de Flipper een transmit, uit welke frontend die ook komt. `confirm_action` vanuit een chat geeft dan `confirm_on_flipper`: druk OK op de Flipper (Back = nee). De actie blijft open tot de TTL verloopt. Zonder verbonden app geldt de gewone regel. De audit-log toont koppelen en loskoppelen (`physical_confirmer`) en per bevestiging `confirmed_on: "flipper"`.

Starten: open de app zelf op de Flipper (Apps → Tools → PiToolbox), of laat de Pi hem starten zodra er iets te bevestigen is. Dat doet de Pi alleen als er geen andere app open staat. Zolang de app open is, weigeren Sub-GHz/NFC/RFID via de CLI (`app_locked`). Bij `ir_send` gaat de app even dicht en weer open, omdat de Infrared-app de loader nodig heeft.

Presets (`PITOOLBOX_FLIPPER_UI_PRESETS`; hoogstens 12, label hoogstens 24 tekens ASCII; de prompt blijft op de Pi):

```yaml
presets:
  - id: 1
    label: "Wifi-overzicht"
    prompt: "Doe een wifi-scan en vat samen welke netwerken in scope zijn."
```

Een preset draait als agent-beurt en heeft dus een LLM-config nodig (`PITOOLBOX_LLM_*`). Voor de service: zet het bestand bijvoorbeeld in `/etc/pitoolbox/flipper_presets.yaml` (leesbaar voor de groep `pitoolbox`) en zet dat absolute pad in `PITOOLBOX_FLIPPER_UI_PRESETS`. De standaard `~/.config/…` wijst bij de service naar de home van de gebruiker `pitoolbox`. Ontwerp en protocol: [docs/fase4-protocol.md](docs/fase4-protocol.md).

## Configuratie

Alles gaat via omgevingsvariabelen (`src/pitoolbox/config.py`). In je checkout zet je ze in je shell. De service leest `/etc/pitoolbox/pitoolbox.env`; het voorbeeld [deploy/pitoolbox.env.example](deploy/pitoolbox.env.example) zet de paden daar onder `/etc/pitoolbox` en `/var/lib/pitoolbox`. De standaardwaarden hieronder gelden als een variabele niet gezet is.

**Kern**

| Variabele | Standaard | Betekenis |
|---|---|---|
| `PITOOLBOX_SCOPE_FILE` | `~/.config/pitoolbox/scope.yaml` | scope; live herladen, ongeldig of weg = lege scope |
| `PITOOLBOX_AUDIT_LOG` | `~/.local/state/pitoolbox/audit.jsonl` | één JSON-regel per toolcall |
| `PITOOLBOX_CAPTURES_DIR` | `~/captures` | pcap's, per sessie een map |
| `PITOOLBOX_CONFIRM_TTL_S` | `60` | geldigheid van een `pending_id` (s) |
| `PITOOLBOX_TOOL_ALLOWLIST` | leeg = alle tools | kommagescheiden, bijv. `status,system_info,wifi_scan` |

**Flipper**

| Variabele | Standaard | Betekenis |
|---|---|---|
| `PITOOLBOX_FLIPPER_TRANSPORT` | `auto` | `auto` (eerst USB, anders BLE), `usb` of `ble` |
| `PITOOLBOX_FLIPPER_PORT` | automatisch (USB 0483:5740) | seriële poort, bijv. `/dev/ttyACM0` |
| `PITOOLBOX_FLIPPER_CLI_PROFILE` | `auto` | op basis van `firmware_origin_fork`; of vast `official` / `momentum` |
| `PITOOLBOX_FLIPPER_BLE_ADDRESS` | geen (dan zoeken op naamprefix) | aanbevolen om vast te zetten |
| `PITOOLBOX_FLIPPER_BLE_NAME_PREFIX` | `Flipper` | naamprefix bij zoeken zonder adres |
| `PITOOLBOX_FLIPPER_BLE_ADAPTER` | leeg = standaardadapter van BlueZ (de eerste die aan staat) | bijv. `hci1` |
| `PITOOLBOX_FLIPPER_KEEPALIVE_S` | `20` | RPC-ping op een idle BLE-link; `0` = uit |

**Wifi**

| Variabele | Standaard | Betekenis |
|---|---|---|
| `PITOOLBOX_ALFA_IFACE` | `wlan1` | de Alfa (monitor, scan, capture) |
| `PITOOLBOX_UPLINK_IFACE` | `wlan0` | de uplink; wordt nooit omgezet |
| `PITOOLBOX_ALFA_DRIVER` | `mt7921u` | wifi-acties weigeren als de Alfa-interface een andere driver heeft (bescherming als namen verwisseld raken); leeg = uit |

**Remote (fase 2)**

| Variabele | Standaard | Betekenis |
|---|---|---|
| `PITOOLBOX_PUBLIC_URL` | geen | verplicht voor `--transport http`, zonder pad en zonder slash aan het eind, bijv. `https://toolbox.example.nl` |
| `PITOOLBOX_HTTP_HOST` / `PITOOLBOX_HTTP_PORT` | `127.0.0.1` / `8765` | alleen loopback toegestaan |
| `PITOOLBOX_AUTH_DB` | `~/.local/state/pitoolbox/auth.sqlite` | wachtwoord-hash, TOTP-geheim, clients, tokens |
| `PITOOLBOX_ACCESS_TOKEN_TTL_S` / `PITOOLBOX_REFRESH_TOKEN_TTL_S` | `900` / `604800` | levensduur van tokens (s) |
| `PITOOLBOX_EXTRA_REDIRECT_URIS` | leeg | kommagescheiden; alleen tijdelijk, voor de MCP Inspector |

**LLM en agent (fase 4)**

| Variabele | Standaard | Betekenis |
|---|---|---|
| `PITOOLBOX_LLM_PROVIDER` | **geen** (verplicht voor de agent) | preset (`deepseek`, `mistral`, `ollama`, ...) of `openai-compatible`, `anthropic`, `openai`; lijst: `pitoolbox-agent --list-providers` |
| `PITOOLBOX_LLM_MODEL` | **geen** (verplicht voor de agent) | modelnaam bij de provider |
| `PITOOLBOX_LLM_BASE_URL` | geen | verplicht bij `openai-compatible`; bij een preset overschrijft het diens endpoint (bijv. China-regio) |
| `PITOOLBOX_LLM_API_KEY` | geen | verplicht bij cloudpresets (ook `anthropic`/`openai`), optioneel bij lokale servers |
| `PITOOLBOX_LLM_TIMEOUT_S` / `PITOOLBOX_LLM_MAX_RETRIES` / `PITOOLBOX_LLM_MAX_TOKENS` | `60` / `2` / `4096` | per aanvraag; opnieuw alleen bij 429/5xx/time-out/verbindingsfout |
| `PITOOLBOX_AGENT_MAX_STEPS` / `PITOOLBOX_AGENT_MAX_CALLS_PER_STEP` | `8` / `8` | tool-rondes per beurt, tool calls per modelantwoord |
| `PITOOLBOX_AGENT_TURN_TIMEOUT_S` / `PITOOLBOX_AGENT_MAX_RESULT_CHARS` | `300` / `8000` | tijd per beurt (tussen stappen gecontroleerd), lengte van een tool-resultaat naar het model |
| `PITOOLBOX_AGENT_MAX_HISTORY_CHARS` | `120000` | daarboven vallen de oudste hele beurten uit de geschiedenis (het model krijgt daar een melding van) |

**Flipper-app (fase 4)**

| Variabele | Standaard | Betekenis |
|---|---|---|
| `PITOOLBOX_FLIPPER_UI` | `off` | `on` = bridge naar de Flipper-app (in `pitoolbox-mcp` **of** `pitoolbox-agent`, nooit in beide) |
| `PITOOLBOX_FLIPPER_UI_APP` | `/ext/apps/Tools/pitoolbox_ui.fap` | alleen een `.fap` onder `/ext/apps/` |
| `PITOOLBOX_FLIPPER_UI_PRESETS` | `~/.config/pitoolbox/flipper_presets.yaml` | ontbreekt het bestand, dan zijn er geen presets |

`deploy/install.sh` leest daarnaast `PITOOLBOX_SKIP_UV_INSTALL` en `PITOOLBOX_UV_BIN` (zie stap 7).

## Problemen oplossen

Begin met de logs: `journalctl -u pitoolbox-mcp -u cloudflared-pitoolbox -n 100 --no-pager` voor de service, en de audit-log voor wat er per toolcall gebeurde.

| Symptoom | Waar kijken |
|---|---|
| Flipper niet gevonden via USB | gebruiker in `dialout` (`id`), `ls -l /dev/ttyACM*`, qFlipper of een terminal niet tegelijk open |
| `unsupported_transport` | die tool heeft USB nodig (geen CLI over BLE): kabel aansluiten of `PITOOLBOX_FLIPPER_TRANSPORT=usb` |
| `app_locked` | de Flipper-app is open; Sub-GHz/NFC/RFID via de CLI kan dan niet |
| `confirm_on_flipper` | de Flipper-app is verbonden: bevestig met OK op de Flipper |
| `ble-scan` vindt niets, koppelen mislukt, BLE traag | [docs/fase3-ble.md §8](docs/fase3-ble.md#8-problemen-oplossen) |
| "Bluetooth-adapter hciN bestaat niet", Alfa-BT start niet | `bluetoothctl list`, kies de ingebouwde adapter met `PITOOLBOX_FLIPPER_BLE_ADAPTER` |
| monitor mode springt terug naar managed | NetworkManager beheert de Alfa nog: `install.sh --onderweg` ([docs/fase5-onderweg.md](docs/fase5-onderweg.md)) |
| `wlan0`/`wlan1` verwisseld | **gebruik pitoolbox dan niet**; [docs/fase5-onderweg.md §6](docs/fase5-onderweg.md#6-als-de-namen-toch-verwisselen) |
| `iw`/`airodump-ng`: operation not permitted onder systemd | capabilities: [docs/fase2-remote.md §10](docs/fase2-remote.md#10-problemen-oplossen), "Wifi of Flipper werkt niet onder systemd" |
| Claude kan de server niet bereiken, inloggen faalt, 421/403 | [docs/fase2-remote.md §10](docs/fase2-remote.md#10-problemen-oplossen) |
| TOTP-code wordt afgewezen | klok van de Pi: `timedatectl` moet `System clock synchronized: yes` tonen |
| agent start niet (exitcode 2) | `PITOOLBOX_LLM_PROVIDER` en `PITOOLBOX_LLM_MODEL` gezet? De foutmelding noemt de ontbrekende of ongeldige waarde |
| Flipper-app bouwt niet of geeft "App Too Old/New" | SDK moet exact de firmware op de Flipper zijn: [flipper_app/pitoolbox_ui/README.md](flipper_app/pitoolbox_ui/README.md) |

## Ontwikkelen

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```

Alle tests draaien zonder hardware: de Flipper wordt gesimuleerd (CLI met echo/prompt, RPC-frames, een nagebootste Flipper-app) en `iw`, `ip`, `airodump-ng` en `vcgencmd` krijgen opgenomen uitvoer. Python 3.11+, type hints met mypy strict, ruff. Protocoldetails komen uit officiële bronnen, met de bron in een comment. Werkafspraken: [docs/VOORTGANG.md](docs/VOORTGANG.md).

### Structuur

```
src/pitoolbox/
  app.py                      # bouwt ToolRunner uit Settings
  config.py                   # alle PITOOLBOX_*-variabelen
  core/
    scope.py policy.py audit.py errors.py proc.py
    devices/flipper.py        # FlipperDevice: USB of BLE achter één interface
    devices/_flipper_transport.py  # ByteTransport-protocol en USB-transport (pyserial)
    devices/_flipper_types.py # fouttypes, AppEvent en FlipperTiming
    devices/_flipper_link_core.py  # FlipperDevice-basis: toestand, haken gekoppelde app
    devices/_flipper_link.py  # operatiekader: lock/abort, reconnect, keepalive
    devices/_flipper_modes.py # CLI/RPC-modewissel
    devices/_flipper_cli_actions.py  # Sub-GHz/NFC/RFID via de CLI
    devices/_flipper_app_channel.py  # kanaal naar de RPC-gekoppelde PiToolbox-app
    devices/_flipper_install.py      # FAP installeren (ui-install)
    devices/_flipper_ir.py    # ir_send met handoff van de gekoppelde app
    devices/_flipper_ble.py   # BLE-transport (bleak): lezen, schrijven met flow control
    devices/_flipper_ble_scan.py  # serial-service-UUID's, Flipper herkennen, scannen
    devices/_flipper_ble_gatt.py  # pairingfouten, BlueZ-opzoekingen, MTU, krediet
    devices/_flipper_ble_link.py  # BLE-verbinding opzetten, toestand, bleak-callbacks
    devices/_ble_pairing.py   # koppelen met PIN via BlueZ (D-Bus); publieke pairing-API
    devices/_bluez_dbus.py    # BlueZ-namen, foutmeldingen, BluezBus-protocol, opzoekingen
    devices/_bluez_agent.py   # de tijdelijke pairing-agent (org.bluez.Agent1)
    devices/_bluez_fastbus.py # BluezBus op dbus-fast (system bus)
    devices/_bluez_devices.py # bekende apparaten: lijst, ontkoppelen, verbreken
    devices/_flipper_rpc/     # framing, RPC-client, gegenereerde protobuf (pb/)
    devices/_flipper_cli.py   # vaste CLI-sjablonen + parsers per firmwareprofiel
    devices/wifi.py           # AlfaAdapter; publiek importpad voor de wifi-onderdelen
    devices/_wifi_parse.py    # parsers voor iw- en airodump-ng-uitvoer
    devices/_wifi_iface.py    # interfacetype (iw) en driverguard (sysfs)
    devices/_wifi_capture.py  # CaptureManager (airodump-ng pcap)
    devices/system.py         # get_throttled, uplink, schijf
    tools/                    # base, runner, flipper_tools, wifi_tools, system_tools
                              #   (_runner_core.py: confirm_action-spec, uitkomsten, audit)
  frontends/mcp_server.py     # MCPServer (opvolger van FastMCP in mcp 2.x), stdio + HTTP
  frontends/agent_cli.py      # pitoolbox-agent (terminal)
                              #   (_agent_cli_view.py: weergave; _agent_cli_args.py: argumenten)
  frontends/flipper_ui/       # bridge naar de Flipper-app: protocol, bridge, presets
                              #   (protocol_*.py en bridge_*.py: de delen per onderdeel)
  agent/ llm/                 # agent-loop; providers (openai-compatible, anthropic, openai)
  auth/                       # OAuth 2.1: provider, SQLite-store, /login, crypto, CLI
  flipper_cli.py              # pitoolbox-flipper: ble-scan/pair/unpair/list/test, ui-*
  flipper_ble_*.py flipper_ui_*.py  # de subcommando's per groep
deploy/                       # install.sh, env-voorbeeld, en per doel een map:
  systemd/ cloudflared/       #   service-units, tunnelconfig (fase 2)
  network/                    #   .link-bestanden: wlan0 = ingebouwd, wlan1 = Alfa (fase 5)
  NetworkManager/             #   drop-in: Alfa unmanaged (fase 5)
  logrotate/ tmpfiles/        #   audit-log-rotatie, beleid capture-map (fase 5)
  bluetooth/                  #   optioneel BLE-interval voor main.conf (fase 5, niet getest)
docs/VOORTGANG.md             # status per fase, besluiten, overdracht
docs/fase2-remote.md          # installatie remote + troubleshooting
docs/fase3-ble.md             # Flipper via Bluetooth: koppelen, testen, troubleshooting
docs/fase4-spike.md           # onderzoek Flipper als bediening (DataExchange)
docs/fase4-protocol.md        # protocol FAP <-> Pi, bevestigen, bridge
docs/fase5-onderweg.md        # klaar voor onderweg: installeren, controleren, terugdraaien
flipper_app/pitoolbox_ui/     # Flipper-app (C, ufbt)
scripts/gen_proto.py          # protobuf opnieuw genereren
tests/
```

Programma's (`[project.scripts]` in `pyproject.toml`): `pitoolbox-mcp`, `pitoolbox-auth`, `pitoolbox-flipper`, `pitoolbox-agent`.

Bronnen en licenties: [THIRD_PARTY.md](THIRD_PARTY.md).

## Hardware-testchecklists

Deze lijsten werk je af op de echte Pi. De checklists van fase 4 en 5 staan bij hun documenten:

- fase 5: [docs/fase5-onderweg.md §7](docs/fase5-onderweg.md#7-hardware--en-testchecklist);
- fase 4, Flipper-app: [flipper_app/pitoolbox_ui/README.md](flipper_app/pitoolbox_ui/README.md) ("Hardwaretest") en [docs/fase4-spike.md](docs/fase4-spike.md) (test 7; test 6 vervalt);
- fase 3, volledig: [docs/fase3-ble.md §9](docs/fase3-ble.md#9-hardware-testlijst).

### Fase 3

- [x] `ble-scan` vindt de Flipper; `ble-pair` vraagt de code van het Flipper-scherm en koppelt (2026-10-16)
- [x] `ble-test`: system_info en storage_list via BLE; noteer ping-latency en doorvoer (KiB/s) en vergelijk met `--compare-usb` (ping 98 ms vs 2 ms, download 1,2 vs 97 KiB/s; zie VOORTGANG)
- [x] Haalt BlueZ op de Pi een grote MTU, of blijft het bij 20 bytes per write (traag maar werkend)? (MTU 414 via D-Bus, writes tot 243 B)
- [ ] `ir_send` via BLE (bevestigen → zendt)
- [ ] `subghz_rx` met `transport=auto`: zonder kabel `unsupported_transport`, met kabel automatisch via USB
- [x] Link weg en terug: de volgende call verbindt opnieuw (`ui-droptest` 2x 20/20); de keepalive pingt door, ook met de app open (2026-10-17). Echt buiten bereik lopen is nog niet geprobeerd
- [ ] De service als gebruiker `pitoolbox` (groep `bluetooth`) kan BLE gebruiken zonder D-Bus-fouten

### Fase 2

- [ ] `pitoolbox-auth init`: de otpauth-URI werkt in je authenticator-app, en `timedatectl` toont `System clock synchronized: yes`
- [ ] `curl -si https://<domein>/mcp` → `401` met `resource_metadata` (van buiten je netwerk)
- [ ] `/.well-known/oauth-protected-resource/mcp` en `/.well-known/oauth-authorization-server` → JSON
- [ ] De connector toevoegen op claude.ai → loginpagina toont `claude.ai` → na inloggen zie je de tools
- [ ] Vanaf de telefoon: `status` aanroepen en een `ir_send` voorstellen → bevestigen in het gesprek → zendt
- [ ] Na 15 min nog steeds verbonden (refresh werkt); na `pitoolbox-auth revoke-all` moet je opnieuw inloggen
- [ ] `systemctl status pitoolbox-mcp cloudflared-pitoolbox`; `grep CapAmb /proc/$(pidof -s python3)/status` toont `0000000000003000`
- [ ] Werkt `wifi_scan`/`alfa_set_mode` onder systemd (ambient caps, geen root)?
- [ ] Langste tool (`subghz_rx` van 100 s) haalt de Cloudflare-timeout van 125 s

### Fase 1

Alles hieronder is alleen getest tegen nagebootste apparaten, behalve wat in [Status](#status) staat. Op de Pi zelf controleren:

**Flipper (USB)**
- [x] `system_info` geeft `firmware_origin_fork`, en het juiste CLI-profiel wordt gekozen (`momentum`)
- [x] `storage_list /ext` en `storage_read` van een klein `.ir`-bestand werken (2026-10-17: `/ext/infrared`, `Samsung.ir`)
- [x] Omschakelen RPC ↔ CLI en terug, meerdere keren achter elkaar (bijv. `system_info` → `rfid_read` → `system_info`)
- [ ] `subghz_rx` op 433.92 MHz met een eigen afstandsbediening: klopt `packet_count`, en worden pakketten goed gesplitst? (de splitsheuristiek is niet uit de bron geverifieerd)
- [ ] `rfid_read` met een eigen tag: protocol, hex en infotekst
- [ ] `nfc_read` met en zonder tag (gedrag na Ctrl+C zonder tag); weigert de Flipper als de NFC-app open staat?
- [ ] `ir_send` op je eigen tv: bevestiging → zendt → app sluit weer
- [x] `emergency_stop` tijdens een `subghz_rx` van 60 s: stopt binnen ~1 s (2026-10-17: noodstop 1 ms, rx 15 ms later afgebroken; daarna werken CLI en RPC gewoon)
- [ ] USB eruit en erin tijdens gebruik: de volgende call verbindt opnieuw
- [x] `pitoolbox-mcp` over stdio als echt proces met een MCP-client (SDK): `tools/list` volgt de allowlist, `status`/`system_info`/`storage_list`/`storage_read` via USB werken, `ir_send` buiten scope, een verzonnen `pending_id`, een tool buiten de allowlist en een pad met `..` worden geweigerd, `emergency_stop` werkt, alles staat in de audit-log (2026-10-17, 12/12)

**Alfa AWUS036AXML (mt7921u)**
- [ ] `alfa_set_mode monitor` / `managed`. Gooit NetworkManager roet in het eten? (oplossing: `install.sh --onderweg`, zie [docs/fase5-onderweg.md](docs/fase5-onderweg.md))
- [ ] `wifi_scan iw` (managed) en `wifi_scan airodump` (monitor), ook op 5 GHz
- [ ] `capture_start` op je eigen BSSID: bevat de pcap **alleen** dat BSSID? (`--bssid` is in de docs alleen als weergavefilter beschreven)
- [ ] `capture_stop` levert een geldige `.cap` op (opent in Wireshark)
- [ ] Rechten: werkt het met `CAP_NET_ADMIN`/`CAP_NET_RAW`, of is root nodig?

**Pi**
- [x] `status`: klopt `vcgencmd get_throttled` (gebruiker in de groep `video`), en worden uplink en schijf goed gerapporteerd? (als gebruiker kali; als service-gebruiker nog niet)
