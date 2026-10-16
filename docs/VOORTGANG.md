# Voortgang en overdracht

Dit bestand is het startpunt voor een nieuwe (Claude-)sessie, bijvoorbeeld op de Pi zelf. Lees ook [README.md](../README.md).

## Werkwijze (afspraken met Stijn)

- Werk in fases. Na elke fase: tests draaien, README bijwerken, kort rapport (wat werkt, wat niet, wat met echte hardware getest moet worden) en dan **stoppen tot Stijn "door" zegt**.
- Python 3.11+, uv + venv (Kali blokkeert systeem-pip), pytest, type hints (mypy strict), ruff.
- Protocoldetails (BLE-UUID's, RPC-berichten, flow control, SDK-functies) komen uit officiële bronnen of repo's, nooit uit het geheugen. Noem de bron in een comment.
- **Geen generieke shell-tool, nergens.** Alleen benoemde acties met gevalideerde argumenten.
- Commit alleen als Stijn erom vraagt. Werkbranch: `fase-1` (alle fases tot nu toe staan erop).
- **LLM-provider-neutraal.** Claude is **niet** de standaardprovider, en er is er helemaal geen (besluit van Stijn). `llm/providers.py` (fase 4) moet meerdere providers ondersteunen, ook lokale (Ollama, llama.cpp, LM Studio, vLLM) en Chinese (DeepSeek, Qwen, Kimi, GLM, MiniMax). Het plan is één OpenAI-compatibele adapter plus native adapters. De default staat in de config, nooit hardcoded.

## Status per fase

| Fase | Status | Belangrijkste onderdelen |
|---|---|---|
| 1 Kern + lokale MCP (USB) | klaar; **Flipper-deel op hardware getest** (USB, Momentum), Alfa nog niet (heeft root/caps nodig) | scope/policy/audit/runner, Flipper RPC + CLI-sjablonen, Alfa-wifi, status/emergency_stop, MCP stdio |
| 2 Remote via Claude-app | klaar, **niet op hardware getest** | streamable HTTP op 127.0.0.1:8765/mcp, OAuth 2.1 (DCR, PKCE, wachtwoord + TOTP), cloudflared, systemd, [fase2-remote.md](fase2-remote.md) |
| 3 Flipper via BLE | klaar; **op hardware getest** (koppelen, system_info, storage, ping/doorvoer); `ir_send` en reconnect-lijst nog open | bleak-transport, flow control, transport auto/usb/ble, keepalive, `pitoolbox-flipper ble-*`, [fase3-ble.md](fase3-ble.md) |
| 4 Flipper als bediening | **spike klaar** ([fase4-spike.md](fase4-spike.md), advies: DataExchange); **providerlaag klaar** (`llm/`: openai-compatible, anthropic, openai; geen default); **agent-loop + `pitoolbox-agent` klaar** (alleen een mens bevestigt, `confirm_action` onzichtbaar voor het model, grenzen per beurt, beurt-audit) | **bezig:** FAP + protocol. Nog: bridge FAP ↔ agent, adapters + agent één keer tegen de echte API's en Ollama testen |
| 5 Klaar voor onderweg | **deploybestanden klaar, niet op hardware getest** (root + reboot nodig); nog: README van nul | `install.sh --onderweg`: .link-namen wlan0/wlan1 (op driver/USB-id), NM unmanaged `driver:mt7921u`, logrotate audit-log, tmpfiles captures; optioneel `--bluez-le`; `--dry-run`, backups; [fase5-onderweg.md](fase5-onderweg.md) |

Tests: `uv run pytest` (965 passed, 1 skipped op de Pi; de skip is shellcheck als die niet op PATH staat). Daarnaast `uv run ruff check src tests` en `uv run mypy`.

## Open punten die Stijn nog moet aanleveren

- ~~Flipper-firmware + versie~~ → **Momentum `mntm-012`** (commit e1784e74, protobuf 0.25). Het profiel `momentum` is toegevoegd: dezelfde CLI als official, met een ruimer Sub-GHz-RX-bereik in de firmware. Het beleid blijft op het officiële bereik. Het profiel wordt automatisch gekozen. Via USB getest: `system_info`, `subghz_rx` (433,92 en 290 MHz), `rfid_read` en `nfc_scan` zonder tag, en het wisselen tussen RPC en CLI. **Nog testen met een echte tag of afstandsbediening:** RFID, NFC en het splitsen van Sub-GHz-pakketten.
- **Domein voor de tunnel.** Overal staat nog de placeholder `toolbox.example.nl` / `toolbox.example.com`.
- ~~Fase 4: welke LLM-providers als eerste~~ → besloten 2026-10-16: **geen default** (de agent weigert zonder config); als eerste openai-compatible (generiek), Anthropic native en OpenAI native.

## Hardwaretest op de Pi (2026-10-16)

- **Hardware:** het is een **Raspberry Pi 4 Model B** (niet de Pi 5 uit de docs). Python 3.14; `uv sync` en de tests (715) werken.
- **Bluetooth:** de Alfa heeft een eigen BT-controller (`hci0`, btusb), maar die start niet (`Opcode 0x0c03 failed: -110` in dmesg) en BlueZ ziet hem niet. Alles loopt via de ingebouwde `hci1` (BCM43455), die bleak automatisch kiest.
- **Flipper via BLE werkt** (fase 3, gedeeltelijk afgevinkt). Naam `A1tkethe`, adres `80:E1:26:20:F6:6B`, gekoppeld en bonded in BlueZ. Het adres staat in `~/.zshenv`; voor de service moet het nog in `/etc/pitoolbox/pitoolbox.env`.
  - Gerepareerde bug: `RpcClient.request` had één vaste deadline van 5 s voor het hele antwoord. Over BLE komt er ~1 indicatie per ~98 ms, dus DeviceInfo (~37 frames) haalde dat niet. Nu is het een inactiviteitstime-out per frame.
  - Metingen BLE vs USB: verbinden + system_info eerst 14,1 s, na de verbeteringen 7,8–10,9 s (USB 0,47 s); ping 98 ms vs 2 ms; download 1,2 KiB/s vs 97 KiB/s.
  - Verbeteringen: `PITOOLBOX_FLIPPER_BLE_ADAPTER`/`--adapter`; een gebonden Flipper verbindt zonder scan; de echte MTU (414) komt via D-Bus, omdat de cache van bleak op 23 bleef staan, dus writes zijn nu tot 243 bytes; een achtergebleven BlueZ-link wordt eerst verbroken; `ble-list` toont nu ook hernoemde Flippers.
  - Ping en doorvoer hangen aan het verbindingsinterval dat Linux kiest (30–50 ms). De Flipper vraagt nooit om een korter interval (`gap.c`). Sneller kan alleen met root, via `main.conf [LE]`: zie fase3-ble.md "BLE sneller maken" (**nog niet getest**, heeft sudo nodig).
  - Een hernoemde Flipper (geen prefix `Flipper`) vindt `ble-scan` gewoon, via de 16-bit service-UUID 0x3082 in de advertentie (geverifieerd).
- **Flipper via USB werkt** (`/dev/ttyACM0`): system_info en storage.
- **Wifi-interfaces:** `wlan1` = Alfa (`mt7921u`), `wlan0` = ingebouwde Broadcom, dus de standaardwaarden kloppen. NetworkManager beheert `wlan1` nog (fase 5).
- **TIJDELIJK:** `PITOOLBOX_UPLINK_IFACE=eth0` in `~/.zshenv`, omdat de Pi tijdens het ontwikkelen via ethernet aan het net hangt. Onderweg is de uplink `wlan0` (de standaard). **Weghalen vóór fase 5 / gebruik onderweg.** Dit niet in de code-default zetten.

## Eerste stappen op de Pi

1. `git clone https://github.com/stijnvandepol/PiToolbox && cd PiToolbox && git checkout fase-1`
2. `uv sync`, daarna `uv run pytest` (op Linux draaien ook de POSIX-/dbus-tests mee)
3. Werk de hardware-checklists af: fase 1 en 2 in de [README](../README.md), fase 3 in [fase3-ble.md](fase3-ble.md). Begin met de Flipper over USB (`system_info`) en de Alfa (`alfa_set_mode`, `wifi_scan`).
4. Noteer per checklistpunt wat werkt en wat niet. Daarmee repareren we eerst, en pas daarna gaat fase 4 van start.

## Belangrijke ontwerpbeslissingen (niet opnieuw ter discussie stellen)

- **Scope.** Captures alleen op een BSSID uit `scope.yaml`, op het kanaal dat daar staat. Een scan is passief en toont buren met een `in_scope`-vlag.
- **Transmit.** Buiten scope altijd geweigerd. Binnen scope krijg je een `pending_id`, en `confirm_action` werkt eenmalig, met een TTL van 60 s, gebonden aan de frontend (bij HTTP: per OAuth-client) en met een nieuwe scope-check.
- **Flipper.** Sub-GHz RX, NFC en RFID zitten niet in de RPC, dus die gaan via vaste CLI-sjablonen (alleen over USB). Over BLE is er alleen RPC, omdat de firmware bij het verbinden een RPC-sessie opent.
- **OAuth.** Redirect-URI's alleen exact `https://claude.ai/api/mcp/auth_callback` en `https://claude.com/api/mcp/auth_callback`. Uitzonderingen kunnen alleen tijdelijk, via `PITOOLBOX_EXTRA_REDIRECT_URIS`, voor de MCP Inspector.
- **Alfa AWUS036AXML.** MT7921AUN, in-kernel `mt7921u`. Monitor-mode via `iw` zonder de "active"-vlag (die laat de driver hangen, mt76 issue #839).
- **MCP SDK.** mcp 2.x: FastMCP heet nu `MCPServer`. De server geeft ons eigen pydantic-schema één-op-één door, en alle validatie gebeurt in de `ToolRunner`.

## Fase 4: wat er eerst moet gebeuren (spike, niet bouwen)

> **Gedaan (2026-10-16):** zie [fase4-spike.md](fase4-spike.md). Advies: de FAP blijft draaien en praat via `app_data_exchange` (DataExchange). De handoff waarbij de FAP sluit en opnieuw start, is alleen een fallback voor CLI-acties. De volgende stap is het hardwaretestplan in dat rapport, met de ingebouwde app RPC Debug. De oorspronkelijke vragen staan hieronder.

Uitzoeken en rapporteren op Stijns firmware:
- Kan een FAP het seriële BLE-profiel (`ble_profile_serial`) starten, data ontvangen en daarna het standaardprofiel herstellen?
- Kan de Pi via RPC een app starten terwijl er al een FAP draait, en kan de Pi een FAP starten met een bestandspad als argument? Let op: `StartRequest` heeft een `args`-veld.
- Stel op basis daarvan het handoff-ontwerp voor: de FAP sluit, de Pi doet de RPC-actie en start de FAP opnieuw met het resultaat.
