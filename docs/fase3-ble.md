# Fase 3: Flipper Zero over Bluetooth LE

De Flipper hoeft niet meer met een USB-kabel aan de Pi te hangen: de MCP-server kan hem ook via Bluetooth Low Energy bedienen. Dit document beschrijft koppelen, testen, wat wel en niet werkt over BLE, en wat je op de hardware moet controleren.

```
Claude ──▶ pitoolbox-mcp ──┬── USB-serieel (CLI + RPC)     ◀── alle tools
                           └── BLE (alleen RPC) ──▶ BlueZ ──▶ Flipper
```

> **Wat is geverifieerd en wat niet.** Alles over de Flipper-firmware komt uit de officiële broncode (flipperzero-firmware, commit `3fb1d517`), alles over BlueZ uit de BlueZ-documentatie in de bluez-repo. Bij het schrijven was dit niet op hardware getest (gemarkeerd met *(niet geverifieerd)*). Inmiddels is BLE op een Pi 4 met Momentum getest: koppelen, system_info, storage en ping/doorvoer werken; zie [VOORTGANG.md](VOORTGANG.md) (hardwaretest) en de checklist onderaan voor wat nog open staat.

## 1. Op de Pi: voorwaarden

1. **BlueZ installeren en de service laten draaien** (Kali/Debian):

   ```bash
   sudo apt install bluez
   sudo systemctl enable --now bluetooth
   systemctl status bluetooth        # moet "active (running)" zijn
   ```

2. **Bluetooth niet geblokkeerd laten.** Een geblokkeerde radio ziet de adapter als "uit":

   ```bash
   rfkill list bluetooth             # "Soft blocked: no" en "Hard blocked: no"
   sudo rfkill unblock bluetooth
   bluetoothctl show                 # "Powered: yes"; anders: bluetoothctl power on
   ```

   (`bluetoothctl` gebruik je hier alleen met de hand om te controleren; pitoolbox zelf start nooit een shell of `bluetoothctl`, het praat rechtstreeks met BlueZ over D-Bus.)

3. **Gebruiker in de groep `bluetooth`.** Debian (en dus Kali) laat in `/usr/share/dbus-1/system.d/bluetooth.conf` de groep `bluetooth` met BlueZ praten (`<policy group="bluetooth"> <allow send_destination="org.bluez"/>`, patch `bluetooth.conf.patch` in de [Debian-bluez-repo](https://salsa.debian.org/bluetooth-team/bluez)). De upstream-config staat het voor iedereen toe (`<policy context="default">`, [src/bluetooth.conf](https://github.com/bluez/bluez/blob/master/src/bluetooth.conf)), maar de groep is de bedoelde route.

   ```bash
   sudo usermod -aG bluetooth "$USER"   # daarna uit- en inloggen (voor ble-pair en ble-test)
   ```

   De service zelf krijgt de groep via `SupplementaryGroups=` in de systemd-unit; die groep moet dus bestaan (het pakket `bluez` maakt hem aan). Verder is er aan de hardening niets veranderd: bleak praat alleen via een AF_UNIX-verbinding met de system bus en opent zelf geen Bluetooth-sockets, dus `RestrictAddressFamilies` blokkeert niets (commentaar in `deploy/systemd/pitoolbox-mcp.service`).

4. **De ingebouwde Bluetooth van de Pi (4 of 5).** De Pi 5 heeft Bluetooth 5.0 / BLE aan boord: de Infineon CYW43455 doet Wi-Fi en Bluetooth in een chip ([Raspberry Pi, "Introducing Raspberry Pi 5"](https://www.raspberrypi.com/news/introducing-raspberry-pi-5/)). De Pi 4 heeft dezelfde chipfamilie (BCM43455); op de test-Pi 4 is dat `hci1`. Een aparte dongle is niet nodig. De chip deelt de radio met de Wi-Fi van de Pi; een officiële bron over onderlinge storing is niet gevonden, dus meet het zelf (zie 4, "Interpreteren") als je veel Wi-Fi-verkeer hebt.

## 2. Aan de Flipper-kant

- **Bluetooth aan:** Settings → Bluetooth → `Bluetooth: ON` (bron: `bt_settings_scene_start.c`). Zonder dat adverteert de Flipper niet en vind je hem niet.
- **Adverteren:** de Flipper adverteert snel (interval 80-100 ms) en schakelt na 60 s naar een lager vermogen; hij blijft vindbaar maar de scan duurt dan langer (`gap.c`: `FAST_ADV_TIMEOUT`/`INITIAL_ADV_TIMEOUT`). Na een verbroken verbinding begint hij uit zichzelf weer te adverteren (`gap.c`, disconnect-event).
- **Een BLE-verbinding tegelijk.** Is de Flipper al verbonden met je telefoon of de officiële Flipper-app, dan kan de Pi niet verbinden. Sluit de app of zet Bluetooth op de telefoon uit. *(Dat de Flipper maar één centrale tegelijk neemt, is afgeleid uit de GAP-code met één `connection_handle`; niet op hardware bevestigd.)*
- **Koppelen vraagt een code.** De seriële service eist een geauthenticeerde link (MITM). De Flipper stelt `IO_CAP_DISPLAY_ONLY` met `MITM_PROTECTION_REQUIRED` in (`gap.c`) en toont bij het koppelen "Pairing code" met 6 cijfers (`bt.c`). Die code voer jij op de Pi in. Op het codescherm verbergt Back het scherm; start dan opnieuw.
- **Alles vergeten:** Settings → Bluetooth → `Unpair All Devices` wist de koppelingen aan Flipper-kant.

## 3. Koppelen in stappen

Het hulpprogramma heet `pitoolbox-flipper` (`/opt/pitoolbox/.venv/bin/pitoolbox-flipper`; in een dev-checkout `uv run pitoolbox-flipper`).

```bash
# 1. Zoeken (Flipper: Bluetooth aan, niet verbonden met een telefoon)
pitoolbox-flipper ble-scan
#   ADRES              NAAM            RSSI     GEKOPPELD
#   AA:BB:CC:DD:EE:FF  Flipper Abcdef  -52 dBm  nee

# 2. Koppelen: de Flipper toont "Pairing code 123456"; typ die hier in
pitoolbox-flipper ble-pair AA:BB:CC:DD:EE:FF

# 3. De regel die ble-pair afdrukt in /etc/pitoolbox/pitoolbox.env zetten
#    PITOOLBOX_FLIPPER_BLE_ADDRESS=AA:BB:CC:DD:EE:FF
sudo systemctl restart pitoolbox-mcp

# 4. Meten
pitoolbox-flipper ble-test --address AA:BB:CC:DD:EE:FF
```

Wat `ble-pair` doet (BlueZ-docs: [Agent](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Agent.rst), [AgentManager](https://github.com/bluez/bluez/blob/master/doc/org.bluez.AgentManager.rst), [Device](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Device.rst)):

1. zoekt het apparaat bij BlueZ (kent hij het nog niet, dan korte discovery van maximaal 10 s);
2. registreert tijdelijk een eigen pairing-agent (`RegisterAgent(…, "KeyboardOnly")`, `RequestDefaultAgent`);
3. roept `Device1.Pair()` aan; BlueZ vraagt dan de agent via `RequestPasskey` om de code; de agent geeft de 6 cijfers die jij intikt terug;
4. zet `Trusted = true`, verbreekt de verbinding die het koppelen openliet (de Flipper staat maar één verbinding toe) en meldt de agent altijd af, ook bij een fout.

De agent weigert alles wat hij niet verwacht (andere apparaten, numerieke vergelijking, legacy-PIN) met `org.bluez.Error.Rejected`. Waarom `KeyboardOnly`: de Flipper kan alleen tonen, dus doet BlueZ "Passkey Entry, initiator voert in"; dat leidt tot `RequestPasskey` en nooit tot `RequestConfirmation`. *(Dit volgt uit de IO-capability-mapping van de Bluetooth Core Spec; de tabel zelf is niet in deze sessie nagelezen, de Flipper- en BlueZ-kant wel.)*

Andere commando's:

| Commando | Doel |
|---|---|
| `ble-list [--prefix P]` | gekoppelde Flippers volgens BlueZ (gekoppeld/bonded/trusted/verbonden) |
| `ble-unpair <adres>` | koppeling + bonding uit BlueZ verwijderen (`Adapter1.RemoveDevice`) |
| `ble-pair <adres> --force` | eerst de bestaande koppeling verwijderen, dan opnieuw koppelen |
| `ble-test [--address A] [--file /ext/…] [--rounds N] [--read-rounds N] [--compare-usb] [--json]` | zie hieronder |

Voorloopnullen horen bij de code (`042731`). De invoer wordt gecontroleerd (alleen 6 cijfers, drie pogingen). BlueZ wacht maximaal 60 s op het antwoord van de agent (`REQUEST_TIMEOUT` in `src/agent.c`); `--timeout` (standaard 90 s) is de totale tijd voor het koppelen.

## 4. De test: `ble-test`

`ble-test` forceert het transport op BLE (`flipper_transport="ble"`) en meet:

1. **verbinden + system_info:** de tijd van de eerste aanroep, dus BLE-verbinding en start van de RPC-sessie samen (de device-klasse verbindt lui bij het eerste gebruik);
2. **items in `/ext`** (`storage_list("/ext")`): bewijst dat de SD-kaart bereikbaar is;
3. **latency:** `--rounds` (standaard 10) keer een kleine RPC-aanroep, met min/mediaan/max in ms. Heeft het device een publieke `ping()`, dan is dat de meting (`rpc_ping`), anders `storage_list /ext`;
4. **downloadsnelheid:** `storage_read` van een bestand, standaard het grootste bestand van hoogstens 256 KiB in `/ext/infrared` of `/ext/subghz`, of `--file`; uitkomst in B/s en KiB/s over `--read-rounds` keer lezen;
5. met `--compare-usb` dezelfde metingen over USB ernaast (komt er geen USB-Flipper, dan wordt dat gemeld en overgeslagen);
6. `--json` geeft alles machineleesbaar (ook de afzonderlijke metingen).

Voorbeeld van de samenvatting:

```
pitoolbox BLE-test (AA:BB:CC:DD:EE:FF)

Meting                                                 BLE                      USB
verbinden + system_info                                2.31 s                   0.18 s
items in /ext                                          9                        9
latency min/med/max (storage_list /ext, n=10)          96 / 118 / 240 ms        7 / 8 / 12 ms
download /ext/subghz/x.sub (48213 B)                   9.4 KiB/s (9625 B/s)     310.0 KiB/s (317440 B/s)
```

(De getallen hierboven zijn een voorbeeld van het formaat, geen meting.)

### Interpreteren

- **Er is geen officieel doorvoercijfer** van de Flipper over BLE om tegen te toetsen. Gebruik `--compare-usb` als eigen referentie en vergelijk BLE met USB op dezelfde Pi en Flipper.
- **Latency** wordt vooral bepaald door het BLE-verbindingsinterval. De Flipper stuurt antwoorden als indicaties en wacht na elk pakket op de bevestiging (`bt.c`, `bt_rpc_send_bytes_callback`). Elk RPC-frame kost dus ongeveer twee intervallen. Het interval kiest de Pi: Linux gebruikt standaard 30–50 ms (`net/bluetooth/hci_core.c`). De Flipper vraagt vanaf Linux nooit om een korter interval, want `gap_verify_connection_parameters` in `gap.c` draait alleen als de link nog niet beveiligd is. Gemeten op een Pi 4 met Momentum mntm-012: ~98 ms per frame, 1,2 KiB/s. Sneller kan alleen met root, zie [BLE sneller maken](#ble-sneller-maken-root-eenmalig). Een grote spreiding (min/max ver uit elkaar) wijst op her-onderhandeling of storing.
- **Schrijfgrootte:** pitoolbox leest de echte MTU via D-Bus (op de Flipper 414), omdat de cache van bleak bij een gebonden apparaat op 23 blijft staan. Een write is dus maximaal 243 bytes (`BLE_SVC_SERIAL_CHAR_VALUE_LEN_MAX`).
- **Doorvoer** bepaalt hoe groot een bestand mag zijn voor een tool-aanroep: een bestand van 256 KiB duurt bij 10 KiB/s ruim 25 s. Houd rekening met de time-outs van de tools; lees over BLE liever kleine bestanden.
- **Verbinden kost seconden** (verbinden, dienstontdekking, start van de RPC-sessie) en zit alleen in de eerste meting; latency en download meten daarna op een bestaande verbinding.
- Zijn alle metingen bij elke run slecht, probeer dan eens de Wi-Fi van de Pi tijdelijk stil te leggen of een ander kanaal; de radio wordt gedeeld, een officiële bron over de invloed is niet gevonden.

## 5. Transport: `auto`, `usb`, `ble`

`PITOOLBOX_FLIPPER_TRANSPORT` (`config.py`):

| Waarde | Gedrag |
|---|---|
| `auto` (standaard) | eerst USB; is er geen USB-Flipper, dan BLE (met `PITOOLBOX_FLIPPER_BLE_ADDRESS` of naamprefix) |
| `usb` | alleen USB; BLE wordt nooit geprobeerd |
| `ble` | alleen BLE; USB wordt genegeerd |

Zet voor BLE altijd `PITOOLBOX_FLIPPER_BLE_ADDRESS`: verbinden op een vast adres is sneller en voorspelbaarder dan scannen op naam (`PITOOLBOX_FLIPPER_BLE_NAME_PREFIX`, standaard `Flipper`). Zijn USB en BLE beide aanwezig, dan wint in `auto` USB: dat is het enige transport met alle tools.

## 6. Belangrijke beperking: over BLE alleen RPC-tools

Over BLE werken **alleen** de tools die het Flipper-RPC-protocol gebruiken: `system_info`, `storage_*` (list/read) en `ir_send`. **`subghz_rx`, `nfc_read` en `rfid_read` werken alleen over USB.** Over BLE geven ze de fout `unsupported_transport`; de status-tool meldt welk transport actief is.

Waarom: bij een BLE-verbinding opent de firmware meteen een RPC-sessie en koppelt de seriële BLE-service daaraan (`bt.c`: bij `GapEventTypeConnected` met het seriële profiel `rpc_session_open(bt->rpc, RpcOwnerBle)`). Er bestaat over BLE geen CLI-prompt; `subghz rx`, `lfrfid read` en de NFC-scanner zijn CLI-commando's (`subghz_cli.c`, `lfrfid_cli.c`, `nfc_cli.c`) en alleen via de USB-CDC-console bereikbaar. Dat is een eigenschap van de firmware, niet van pitoolbox.

Daarbij: er is maar één RPC-sessie tegelijk. Staat er al een RPC-sessie open (bijvoorbeeld de officiële app of een USB-RPC-sessie), dan logt de Flipper "RPC is busy, failed to open new session" en doet de BLE-verbinding niets. Houd USB en BLE dus niet tegelijk in RPC-gebruik.

## 7. Keepalive en herverbinden

- `PITOOLBOX_FLIPPER_KEEPALIVE_S` (standaard 20): is de BLE-verbinding zolang idle, dan stuurt de transportlaag een RPC-ping, zodat de verbinding niet wegvalt; `0` zet dit uit. Alleen BLE gebruikt dit.
- Valt de verbinding weg (Flipper uit, buiten bereik, Bluetooth uitgezet, app ertussen), dan opent de eerstvolgende tool-aanroep de verbinding opnieuw; de Flipper adverteert na een verbroken verbinding vanzelf weer (`gap.c`). Het precieze gedrag (aantal pogingen, wachttijden) staat in `core/devices/_flipper_ble.py` en moet op de hardware worden bevestigd. *(niet geverifieerd)*
- Een tool die tijdens het wegvallen draait, geeft een fout (bijvoorbeeld `device_not_connected` of `device_timeout`) in plaats van te blijven hangen. *(niet geverifieerd)*

### BLE sneller maken (root, eenmalig)

**Nog niet getest.** Laat de Pi zelf een korter interval kiezen. De Flipper staat minimaal 7,5 ms toe (`serial_profile.c`). Probeer het eerst tijdelijk:

```bash
echo 6  | sudo tee /sys/kernel/debug/bluetooth/hci1/conn_min_interval   # eenheid 1,25 ms → 7,5 ms
echo 12 | sudo tee /sys/kernel/debug/bluetooth/hci1/conn_max_interval   # 15 ms
uv run pitoolbox-flipper ble-test
```

Blijvend: zet in `/etc/bluetooth/main.conf` onder `[LE]` de regels `MinConnectionInterval=6` en `MaxConnectionInterval=12`, en draai daarna `sudo systemctl restart bluetooth`. Parameters die BlueZ per apparaat heeft opgeslagen gaan voor. Controleer dat met `sudo grep -A4 ConnectionParameters /var/lib/bluetooth/<adapter-MAC>/<flipper-MAC>/info`. Bestaat die sectie, verwijder hem dan terwijl bluetooth gestopt is. De verwachting is ~15–30 ms per frame, maar dat is nog niet gemeten. Let op: wifi en BT delen de radio van de BCM43455. Wordt wifi merkbaar trager, neem dan `MaxConnectionInterval=24`. `sudo deploy/install.sh --alleen-systeem --bluez-le` zet de twee regels er met backup in (BlueZ kent geen `main.conf.d`), zie [fase5-onderweg.md](fase5-onderweg.md).

## 8. Problemen oplossen

| Symptoom | Oorzaak en oplossing |
|---|---|
| `ble-scan` vindt niets | Bluetooth aan op de Flipper? Niet verbonden met een telefoon/app? Adapter aan (`rfkill`, `bluetoothctl show` → `Powered: yes`)? Vlakbij beginnen; na 60 s adverteert de Flipper langzamer, dus langer scannen: `--timeout 20`. |
| "BlueZ (org.bluez) draait niet" | `sudo systemctl enable --now bluetooth`. |
| "De D-Bus-policy weigert toegang" / "Geen rechten" | Gebruiker niet in de groep `bluetooth` (opnieuw inloggen na `usermod`). |
| "adapter staat uit" | `sudo rfkill unblock bluetooth`; `bluetoothctl power on`. |
| Koppelen: "authenticatie mislukt" | Code verkeerd, of er staat een oude koppeling aan één kant (Flipper vergeten of Pi vergeten). Aan beide kanten wissen: `ble-unpair <adres>` en op de Flipper `Unpair All Devices`; daarna `ble-pair` opnieuw. Kortste route: `ble-pair <adres> --force` plus `Unpair All Devices`. |
| Koppelen: time-out / "geen code ingevoerd" | De code verschijnt pas als het koppelen begint; typ hem direct in. BlueZ wacht 60 s op de agent. Verdween het scherm (Back), start `ble-pair` opnieuw. |
| "De Flipper vroeg geen code" | Er was geen beveiligingsverzoek: staat Bluetooth aan, en is de Flipper niet bezet door een telefoon? |
| "Geen verbinding met de Flipper" (`ConnectionAttemptFailed`) | Buiten bereik, of een andere centrale (de Flipper-app op een telefoon) houdt de enige verbinding vast. |
| Gekoppeld, maar verbinden lukt later niet meer | Bond aan één kant weg (firmware-update, `Unpair All Devices` op de Flipper). Opnieuw koppelen: `ble-pair --force`. De Flipper kan opnieuw bonden (`ACI_GAP_BOND_LOST` → `aci_gap_allow_rebond` in `gap.c`), maar BlueZ houdt de oude sleutel tot je `ble-unpair` doet. |
| BLE-verbinding is er, maar RPC doet niets | "RPC is busy": er is al een RPC-sessie (zie 6). Sluit de app of de USB-RPC-sessie. |
| Tool geeft `unsupported_transport` | Dat tool is CLI-gebaseerd (6): verbind USB of zet `PITOOLBOX_FLIPPER_TRANSPORT=usb`. |
| Pairing werkt alleen op Linux | De pairing-commando's praten met BlueZ en geven op Windows/macOS een duidelijke melding; ontwikkel/test ze op de Pi. |
| Doorvoer slecht | Zie 4, "Interpreteren" en "BLE sneller maken"; vergelijk met `--compare-usb`. |
| "Bluetooth-adapter hciN bestaat niet in BlueZ" | Verkeerde `PITOOLBOX_FLIPPER_BLE_ADAPTER`/`--adapter`. Kijk met `bluetoothctl list`. Een USB-wifikaart met BT (zoals de MT7921 van de Alfa) kan een `hciN` hebben die niet opstart (`dmesg`: `Opcode 0x0c03 failed`). Gebruik dan de ingebouwde adapter. |
| Log: "was nog verbonden in BlueZ; eerst verbreken" | Een vorig proces is gecrasht, of een ander proces (bijv. de service naast `ble-test`) had de link. Pitoolbox verbreekt die link voor een verse RPC-sessie. Het andere proces verbindt bij zijn volgende call opnieuw. |

## 9. Hardware-testlijst

Op een echte Pi 4 of 5 (Kali) met een echte Flipper, in deze volgorde:

- [ ] `systemctl status bluetooth` is actief; `bluetoothctl show` meldt `Powered: yes`.
- [ ] Gebruiker en `pitoolbox` zitten in `bluetooth`; `systemctl start pitoolbox-mcp` start zonder fout (groep bestaat).
- [ ] `ble-scan` toont de Flipper met RSSI.
- [ ] `ble-pair <adres>`: de Flipper toont de code, de agent vraagt erom, na invoer meldt de CLI succes. Controleer dat `RequestPasskey` echt wordt aangeroepen (blijft de vraag uit, herzie dan de capability `KeyboardOnly`; `btmon` toont de SMP-uitwisseling met de IO-capabilities).
- [ ] Een foute code geeft "authenticatie mislukt" en de CLI keert terug zonder te hangen; een tweede `ble-pair` direct erna werkt weer (de agent was dus afgemeld).
- [ ] `ble-list` toont gekoppeld, bonded en trusted; na het koppelen is de Flipper **niet** meer verbonden (de CLI verbreekt hem).
- [ ] `ble-test --address <adres>`: verbinden, `system_info`, `/ext`-lijst en download slagen; noteer verbinden, latency (min/med/max) en KiB/s.
- [ ] `ble-test --compare-usb` met USB aangesloten: de USB-kolom verschijnt en is sneller; geen conflict door twee verbindingen tegelijk (anders: eerst BLE, dan USB meten).
- [ ] Een RPC-sessie die al op USB staat: wat doet BLE dan ("RPC is busy")?
- [ ] De service met `PITOOLBOX_FLIPPER_TRANSPORT=ble`: `system_info`, `storage_list`, `storage_read` en `ir_send` werken via Claude; `subghz_rx`, `nfc_read` en `rfid_read` geven `unsupported_transport`.
- [ ] Keepalive: 5 minuten niets doen en daarna `system_info`; de verbinding staat nog (of wordt netjes hersteld). Probeer ook `PITOOLBOX_FLIPPER_KEEPALIVE_S=0`.
- [ ] Herverbinden: Flipper Bluetooth uit/aan, of buiten bereik en terug; de volgende tool-aanroep slaagt.
- [ ] Wegvallen midden in een `storage_read`: nette fout, geen hangende service.
- [ ] Hardening: `journalctl -u pitoolbox-mcp` zonder `Permission denied` of `AF_`-fouten bij BLE; `systemd-analyze security pitoolbox-mcp` ongewijzigd acceptabel.
- [ ] Optioneel: `ble-test` met veel Wi-Fi-verkeer op `wlan0`/`wlan1`, om storing te beoordelen.

## Bronnen

- BlueZ: [org.bluez.Agent](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Agent.rst), [AgentManager](https://github.com/bluez/bluez/blob/master/doc/org.bluez.AgentManager.rst), [Device](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Device.rst), [Adapter](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst), [bluetooth.conf](https://github.com/bluez/bluez/blob/master/src/bluetooth.conf), [agent.c](https://github.com/bluez/bluez/blob/master/src/agent.c) (`REQUEST_TIMEOUT` 60 s).
- Debian: [bluetooth.conf.patch](https://salsa.debian.org/bluetooth-team/bluez/-/raw/master/debian/patches/bluetooth.conf.patch), [bluez.postinst](https://salsa.debian.org/bluetooth-team/bluez/-/raw/master/debian/bluez.postinst) (maakt de groep `bluetooth`).
- dbus-fast: <https://dbus-fast.readthedocs.io>; bleak 3.0.2 (`bleak/backends/bluezdbus/`).
- Flipper (flipperzero-firmware @ `3fb1d517`): `applications/services/bt/bt_service/bt.c`, `targets/f7/ble_glue/gap.c`, `targets/f7/ble_glue/services/serial_service.c`, `applications/settings/bt_settings_app/scenes/`.
- Raspberry Pi 5: <https://www.raspberrypi.com/news/introducing-raspberry-pi-5/>.
