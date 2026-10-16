# pitoolbox

Software voor een Raspberry Pi 5 (Kali Linux ARM64) die een **Flipper Zero** en een **Alfa AWUS036AXML** aanstuurt via een LLM, voor je eigen lab en apparaten.

> **Status: fase 2.** Kern + MCP-server over stdio (lokaal) en streamable HTTP met OAuth 2.1 (remote, via een Cloudflare Tunnel naar de Claude-app). Flipper via USB. BLE, de Flipper-app en de agent-loop komen in latere fases.

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

De officiële Flipper-RPC ([flipperzero-protobuf](https://github.com/flipperdevices/flipperzero-protobuf)) kent alleen system, storage, app, gui, gpio, property en desktop. Ontvangen via Sub-GHz, NFC of RFID bestaat daar niet. Dezelfde USB-poort heeft ook een CLI. `FlipperDevice` schakelt automatisch tussen beide (`start_rpc_session` ↔ `stop_session`). De CLI-commando's en hun uitvoerformaten komen uit de firmwarebronnen en zijn vastgelegd per **firmwareprofiel**. Nu is alleen `official` beschikbaar (getest tegen de bron, firmware 1.5.x). Bij een andere firmware weigeren de CLI-tools tot er een profiel is. Je kunt een profiel forceren met `PITOOLBOX_FLIPPER_CLI_PROFILE`.

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
| `PITOOLBOX_CONFIRM_TTL_S` | `60` |
| `PITOOLBOX_TOOL_ALLOWLIST` | leeg = alle tools |
| `PITOOLBOX_PUBLIC_URL` | geen (verplicht voor `--transport http`), bijv. `https://toolbox.mijndomein.nl` |
| `PITOOLBOX_HTTP_HOST` / `PITOOLBOX_HTTP_PORT` | `127.0.0.1` / `8765` (alleen loopback toegestaan) |
| `PITOOLBOX_AUTH_DB` | `~/.local/state/pitoolbox/auth.sqlite` |
| `PITOOLBOX_ACCESS_TOKEN_TTL_S` / `PITOOLBOX_REFRESH_TOKEN_TTL_S` | `900` / `604800` |
| `PITOOLBOX_EXTRA_REDIRECT_URIS` | leeg (alleen tijdelijk voor de MCP Inspector) |

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

## Structuur

```
src/pitoolbox/
  app.py                      # bouwt ToolRunner uit Settings
  config.py
  core/
    scope.py policy.py audit.py errors.py proc.py
    devices/flipper.py        # FlipperDevice: USB nu, BLE later, zelfde interface
    devices/_flipper_rpc/     # framing, RPC-client, gegenereerde protobuf (pb/)
    devices/_flipper_cli.py   # vaste CLI-sjablonen + parsers per firmwareprofiel
    devices/wifi.py           # AlfaAdapter, CaptureManager, parsers
    devices/system.py         # get_throttled, uplink, schijf
    tools/                    # base, runner, flipper_tools, wifi_tools, system_tools
  frontends/mcp_server.py     # MCPServer (opvolger van FastMCP in mcp 2.x), stdio + HTTP
  auth/                       # OAuth 2.1: provider, SQLite-store, /login, crypto, CLI
deploy/                       # cloudflared-config, systemd-units, install.sh, env-voorbeeld
docs/fase2-remote.md          # installatie remote + troubleshooting
scripts/gen_proto.py          # protobuf opnieuw genereren
tests/
```

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
- [ ] `alfa_set_mode monitor` / `managed`. Gooit NetworkManager roet in het eten? (oplossing in fase 5: wlan1 unmanaged)
- [ ] `wifi_scan iw` (managed) en `wifi_scan airodump` (monitor), ook op 5 GHz
- [ ] `capture_start` op je eigen BSSID: bevat de pcap **alleen** dat BSSID? (`--bssid` is in de docs alleen als weergavefilter beschreven)
- [ ] `capture_stop` levert een geldige `.cap` op (opent in Wireshark)
- [ ] Rechten: werkt het met `CAP_NET_ADMIN`/`CAP_NET_RAW`, of is root nodig?

**Pi**
- [ ] `status`: klopt `vcgencmd get_throttled` (gebruiker in de groep `video`), en worden uplink en schijf goed gerapporteerd?

Bronnen en licenties: [THIRD_PARTY.md](THIRD_PARTY.md).
