# Voortgang en overdracht

Dit bestand is het startpunt voor een nieuwe (Claude-)sessie, bijvoorbeeld op de Pi zelf. Lees ook [README.md](../README.md).

## Werkwijze (afspraken met Stijn)

- Werk in fases. Na elke fase: tests draaien, README bijwerken, kort rapport (wat werkt, wat niet, wat met echte hardware getest moet worden) en dan **stoppen tot Stijn "door" zegt**.
- Python 3.11+, uv + venv (Kali blokkeert systeem-pip), pytest, type hints (mypy strict), ruff.
- Protocoldetails (BLE-UUID's, RPC-berichten, flow control, SDK-functies) komen uit officiële bronnen of repo's, nooit uit het geheugen. Noem de bron in een comment.
- **Geen generieke shell-tool, nergens.** Alleen benoemde acties met gevalideerde argumenten.
- Commit alleen als Stijn erom vraagt. Werkbranch: `fase-1` (alle fases tot nu toe staan erop).
- **LLM-provider-neutraal.** Claude is **niet** de standaardprovider. `llm/providers.py` (fase 4) moet meerdere providers ondersteunen, ook lokale (Ollama, llama.cpp, LM Studio, vLLM) en Chinese (DeepSeek, Qwen, Kimi, GLM, MiniMax). Het plan is één OpenAI-compatibele adapter plus native adapters. De default staat in de config, nooit hardcoded.

## Status per fase

| Fase | Status | Belangrijkste onderdelen |
|---|---|---|
| 1 Kern + lokale MCP (USB) | klaar, **niet op hardware getest** | scope/policy/audit/runner, Flipper RPC + CLI-sjablonen, Alfa-wifi, status/emergency_stop, MCP stdio |
| 2 Remote via Claude-app | klaar, **niet op hardware getest** | streamable HTTP op 127.0.0.1:8765/mcp, OAuth 2.1 (DCR, PKCE, wachtwoord + TOTP), cloudflared, systemd, [fase2-remote.md](fase2-remote.md) |
| 3 Flipper via BLE | klaar, **niet op hardware getest** | bleak-transport, flow control, transport auto/usb/ble, keepalive, `pitoolbox-flipper ble-*`, [fase3-ble.md](fase3-ble.md) |
| 4 Flipper als bediening | nog niet begonnen | **begint met een spike en rapport**, nog niet bouwen (zie hieronder) |
| 5 Klaar voor onderweg | deels vooruit gedaan | systemd-units bestaan al (fase 2); nog: udev-namen wlan0/wlan1, NetworkManager unmanaged, logrotatie, README van nul |

Tests: `uv run pytest` (712 passed, 2 skipped op Windows; de geskipte tests draaien alleen op Linux). Daarnaast `uv run ruff check src tests` en `uv run mypy`.

## Open punten die Stijn nog moet aanleveren

- **Flipper-firmware + versie.** Nu is alleen het CLI-profiel `official` aanwezig, en bij een andere fork weigeren Sub-GHz/NFC/RFID. Controleer met `system_info` → `firmware_origin_fork`.
- **Domein voor de tunnel.** Overal staat nog de placeholder `toolbox.example.nl` / `toolbox.example.com`.
- **Fase 4: welke LLM-providers als eerste**, en welke default.

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

Uitzoeken en rapporteren op Stijns firmware:
- Kan een FAP het seriële BLE-profiel (`ble_profile_serial`) starten, data ontvangen en daarna het standaardprofiel herstellen?
- Kan de Pi via RPC een app starten terwijl er al een FAP draait, en kan de Pi een FAP starten met een bestandspad als argument? Let op: `StartRequest` heeft een `args`-veld.
- Stel op basis daarvan het handoff-ontwerp voor: de FAP sluit, de Pi doet de RPC-actie en start de FAP opnieuw met het resultaat.
