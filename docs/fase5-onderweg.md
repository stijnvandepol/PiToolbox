# Fase 5: klaar voor onderweg

Onderweg hangt de Pi niet meer aan ethernet. De ingebouwde wifi is dan de uplink, de Alfa doet monitor mode, en de service draait weken achter elkaar. Deze fase legt vier dingen vast:

| Onderdeel | Bestand in de repo | Komt in | Waarom |
|---|---|---|---|
| Vaste interfacenamen | `deploy/network/10-pitoolbox-wlan0.link`, `11-pitoolbox-wlan1.link` | `/etc/systemd/network/` | ingebouwd = `wlan0` (uplink), Alfa = `wlan1`, ongeacht de volgorde bij het opstarten |
| NetworkManager laat de Alfa met rust | `deploy/NetworkManager/90-pitoolbox-unmanaged.conf` | `/etc/NetworkManager/conf.d/` | NM zet monitor mode niet terug en scant niet tussendoor |
| Rotatie van de audit-log | `deploy/logrotate/pitoolbox` | `/etc/logrotate.d/` | de log groeit niet onbeperkt, en er gaat geen regel verloren |
| Beleid voor de capture-map | `deploy/tmpfiles/pitoolbox.conf` | `/etc/tmpfiles.d/` | map bestaat altijd met 0700; standaard wordt er niets opgeruimd |
| Optioneel, **niet getest**: sneller BLE | `deploy/bluetooth/main.conf.le-snippet` | in `/etc/bluetooth/main.conf` | korter verbindingsinterval voor de Flipper (fase3-ble.md, "BLE sneller maken") |

Alles wordt geïnstalleerd door `deploy/install.sh`, maar alleen als je het vraagt: zonder opties doet het script precies wat het in fase 2 deed.

Getest is: de bestanden zelf (pytest: `tests/test_deploy.py`), install.sh in een testmap (`DESTDIR`), `--dry-run` op de Pi, NetworkManager die de drop-in inleest (`--print-config`), en een echte logrotate-run op een kopie. **Niet getest** is alles waarvoor root of een herstart nodig is. Dat staat in de [checklist](#7-hardware-en-testchecklist).

## 1. Ontwerpkeuzes (met bronnen)

### Interfacenamen: systemd `.link`, geen udev-regel

- udev(7) zegt bij `NAME=` zelf: "See systemd.link(5) for a higher-level mechanism for setting the interface name". De [Debian-wiki](https://wiki.debian.org/NetworkInterfaceNames) noemt `.link`-bestanden "the canonical way" voor eigen namen. udev past ze toe via de builtin `net_setup_link` (`80-net-setup-link.rules`). systemd-networkd is daar niet voor nodig: NetworkManager blijft het netwerk beheren.
- **Matchen op driver en bus, niet op MAC.** NetworkManager randomiseert MAC-adressen (op deze Pi is `addr_assign_type` 3). De ingebouwde wifi: `Driver=brcmfmac`, `Type=wlan` en niet-USB. De Alfa: `Driver=mt7921u`, `Type=wlan` en USB-id `0e8d:7961`. De waarden zijn opgenomen met `udevadm info` op de Pi 4 en staan als fixture in `tests/fixtures/deploy/`.
- Er is maar één `.link`-bestand per interface van toepassing: het eerste in alfabetische volgorde. De prefixen 10/11 liggen onder 70 (aanbeveling uit systemd.link(5)), dus ze komen vóór `73-usb-net-by-mac.link` en `99-default.link`. Kali op de Pi maskeert die twee in `/etc` en zet `net.ifnames=0` aan. Dat laatste schakelt alleen `NamePolicy=` uit, en `Name=` werkt dan juist wel (systemd.link(5), bij `Name=`).
- **Bekend risico.** systemd.link(5) waarschuwt dat een naam uit de naamruimte van de kernel (`eth0`, `wlan0`) kan "racen" met de kernel. We doen het toch, omdat de code standaard `wlan0`/`wlan1` gebruikt en die defaults blijven. In de praktijk gaat het alleen mis als beide adapters op exact hetzelfde moment verschijnen. Controleer het na een paar herstarts (stap 4), en lees [sectie 6](#6-als-de-namen-toch-verwisselen) als het misgaat.
- De initramfs bevat een kopie van `/etc/systemd/network/*.link` (de udev-hook van initramfs-tools, `/usr/share/initramfs-tools/hooks/udev`), en de Pi gebruikt een initramfs (`auto_initramfs=1`). Daarom draait install.sh na een wijziging `update-initramfs -u -k all` (de wiki noemt dit "initrd skew").

### NetworkManager: `unmanaged-devices+=driver:mt7921u`

- NetworkManager.conf(5), `[keyfile] unmanaged-devices`: zo'n device is "strictly unmanaged" en kan niet per ongeluk via `nmcli device set wlan1 managed yes` worden teruggezet. De keyfile-plugin is altijd actief, ook naast `ifupdown` (Kali: `plugins=ifupdown,keyfile`).
- Op **driver**, niet op `interface-name:wlan1`. Raken de namen ooit verwisseld, dan zou `interface-name:wlan1` de uplink uitschakelen. Met de driver blijft de ingebouwde wifi altijd managed.
- `+=` voegt toe aan een lijst die een ander bestand al zette, in plaats van die te overschrijven (man-page, "FILE FORMAT"). Getest met `NetworkManager --print-config --config-dir=…` op NM 1.58.1.

### logrotate: hernoemen, geen `copytruncate`

- `AuditLog.write` (`src/pitoolbox/core/audit.py`) opent het bestand **bij elke regel opnieuw** met `open("a")` en sluit het daarna. Na het hernoemen schrijft de volgende call vanzelf in een nieuw bestand, en een write die op dat moment loopt, komt nog compleet in het oude bestand. Er is geen signaal en geen herstart nodig.
- `copytruncate` is dus onnodig, en voor een audit-log zelfs fout. logrotate(8) zegt: "there is a very small time slice between copying the file and truncating it, so some logging data might be lost".
- `nocreate`: de service maakt het nieuwe bestand zelf aan, met `UMask=0077` uit de unit (0600, eigenaar `pitoolbox`). Zo maken logrotate en de service het bestand niet tegelijk aan.
- `su pitoolbox pitoolbox`: de map is van een niet-root-gebruiker, en logrotate(8) raadt voor dat geval `su` aan.
- Bewaren: maandelijks, eerder bij meer dan 50 MiB, met 24 oude bestanden, gecomprimeerd (het nieuwste pas een rondje later). Oudere bestanden worden bewust verwijderd. Wil je alles bewaren, zet dan `rotate -1`.

### Captures: nooit automatisch weg

pcap's zijn je eigen meetgegevens, en stil weggooien is erger dan een volle schijf. `status` toont de vrije ruimte. logrotate past hier niet: het roteert losse bestanden, geen sessiemappen, en het zou een pcap midden in een capture hernoemen. De tmpfiles-regel (`d … 0700 pitoolbox pitoolbox -`) zorgt er alleen voor dat de map bij elke boot bestaat met de juiste rechten. De leeftijd `-` betekent: nooit opruimen (tmpfiles.d(5)). Automatisch opruimen kan wel: zet een leeftijd (bijv. `30d`) in `/etc/tmpfiles.d/pitoolbox.conf`. Zet nooit een leeftijd op `/var/lib/pitoolbox` zelf, want daar staan de auth-database en de audit-log.

### BlueZ: geen drop-in mogelijk

BlueZ 5.87 (`bluetoothd --version`) leest alleen `/etc/bluetooth/main.conf`, of het pad uit `bluetoothd -f` (bluetoothd(8)). `load_config()` in [src/main.c](https://github.com/bluez/bluez/blob/master/src/main.c) bouwt alleen `<configdir>/main.conf`, en een `main.conf.d` bestaat niet. Daarom past `--bluez-le` het bestand zelf aan. Dat gaat zo:

- Eerst maakt het een backup.
- Daarna voegt het de twee regels direct onder `[LE]` in.
- Staat er al een eigen waarde, dan wordt die nooit overschreven.

dpkg ziet `main.conf` daarna als gewijzigd conffile en vraagt bij een bluez-update wat je wilt houden. BlueZ accepteert 6–3200 (`parse_le_config`, eenheid 1,25 ms).

## 2. Voorbereiding

1. Haal de **tijdelijke** ontwikkelinstelling weg: `PITOOLBOX_UPLINK_IFACE=eth0` in `~/.zshenv` (zie VOORTGANG.md). Onderweg is de uplink `wlan0`, en dat is de standaard.
2. Zet in `/etc/pitoolbox/pitoolbox.env` (als root, het bestand is 0600):
   - `PITOOLBOX_ALFA_IFACE=wlan1`
   - `PITOOLBOX_UPLINK_IFACE=wlan0`
   - `PITOOLBOX_FLIPPER_BLE_ADDRESS=<adres>` (het adres staat nu alleen in `~/.zshenv`)
   - eventueel `PITOOLBOX_FLIPPER_BLE_ADAPTER=hci1`. De BT van de Alfa (`hci0`) start niet; zie fase3-ble.md.
3. Zorg dat de Pi een wifi-profiel voor onderweg heeft op `wlan0`, bijvoorbeeld de hotspot van je telefoon: `nmcli device wifi connect <SSID> ifname wlan0 --ask`. Zonder dat profiel heb je na het loskoppelen van ethernet geen netwerk.
4. Werk je via SSH over `wlan0`, houd dan rekening met een korte onderbreking bij de herstart.

## 3. Installeren

Vanuit de checkout van de repo op de Pi:

```bash
# 1. Eerst kijken wat er zou veranderen (schrijft niets, geen root nodig):
deploy/install.sh --dry-run --onderweg
deploy/install.sh --dry-run --bluez-le            # optioneel, toont de diff van main.conf

# 2a. Alleen de fase-5-systeemconfig (de service staat er al):
sudo deploy/install.sh --alleen-systeem --onderweg

# 2b. Of alles in één keer (service bijwerken + fase 5):
sudo deploy/install.sh --onderweg

# 3. Optioneel en niet getest: sneller BLE (zie fase3-ble.md, "BLE sneller maken"):
sudo deploy/install.sh --alleen-systeem --bluez-le

# 4. Activeren:
sudo reboot
```

Het script vraagt bij elke stap om bevestiging (`-y`/`--ja` slaat dat over; zonder terminal en zonder `--ja` stopt het). Een bestand dat al klopt, laat het staan. Een bestand dat anders is, gaat eerst naar `/var/backups/pitoolbox/<tijdstempel>/<volledig pad>`. Na het plaatsen controleert het script:

- welk `.link`-bestand udev per wifi-interface zou toepassen (`udevadm test-builtin net_setup_link`, hernoemt niets);
- of NetworkManager de drop-in inleest (`NetworkManager --print-config`);
- of logrotate de config accepteert (`logrotate -d`).

Daarnaast maakt het de capture-map aan (`systemd-tmpfiles --create`) en werkt het de initramfs bij.

## 4. Controleren (na de herstart)

**Interfacenamen**

```bash
ip -br link
for i in wlan0 wlan1; do echo "$i: $(basename "$(readlink -f /sys/class/net/$i/device/driver)")"; done
# verwacht: wlan0: brcmfmac   wlan1: mt7921u
udevadm info -q property /sys/class/net/wlan1 | grep ID_NET_LINK_FILE
# verwacht: ID_NET_LINK_FILE=/etc/systemd/network/11-pitoolbox-wlan1.link
journalctl -b -u systemd-udevd | grep -i -E 'wlan|rename|link'   # geen fouten over hernoemen
```

Herhaal dit een paar keer: met de Alfa al ingeplugd tijdens het opstarten, en met de Alfa pas na het opstarten ingeplugd.

**NetworkManager**

```bash
NetworkManager --print-config | grep unmanaged-devices        # bevat driver:mt7921u
nmcli -f GENERAL.DEVICE,GENERAL.DRIVER,GENERAL.STATE device show wlan1   # 10 (unmanaged)
nmcli device status                                           # wlan0 managed/verbonden
```

Daarna in pitoolbox: `alfa_set_mode monitor`, een minuut wachten en `iw dev wlan1 info` → `type monitor` (NM zet hem niet meer terug). Daarna `wifi_scan` met `airodump` en `capture_start` op je eigen BSSID.

**Audit-log en captures** (de map is 0700 pitoolbox, dus sudo):

```bash
sudo logrotate -d /etc/logrotate.d/pitoolbox                  # geen "error:"
sudo logrotate -v -f /etc/logrotate.d/pitoolbox               # eenmalig forceren als test
sudo ls -l /var/lib/pitoolbox/                                # audit.jsonl-JJJJMMDD-<epoch>, audit.jsonl weg
# roep één tool aan (bijv. status), daarna:
sudo stat -c '%a %U:%G %n' /var/lib/pitoolbox/audit.jsonl /var/lib/pitoolbox/captures
# verwacht: 600 pitoolbox:pitoolbox ...audit.jsonl en 700 pitoolbox:pitoolbox ...captures
systemctl list-timers logrotate.timer                         # draait dagelijks
```

**BlueZ (alleen met `--bluez-le`, niet getest)**

```bash
grep -A3 '^\[LE\]' /etc/bluetooth/main.conf
sudo systemctl restart bluetooth                              # verbreekt BLE-verbindingen
sudo cat /sys/kernel/debug/bluetooth/hci1/conn_min_interval /sys/kernel/debug/bluetooth/hci1/conn_max_interval
uv run pitoolbox-flipper ble-test                             # vergelijk met ~98 ms / 1,2 KiB/s
```

Blijft het interval oud, kijk dan of BlueZ per apparaat `ConnectionParameters` heeft opgeslagen (fase3-ble.md).

## 5. Terugdraaien

```bash
# Interfacenamen
sudo rm /etc/systemd/network/10-pitoolbox-wlan0.link /etc/systemd/network/11-pitoolbox-wlan1.link
sudo update-initramfs -u -k all
# NetworkManager
sudo rm /etc/NetworkManager/conf.d/90-pitoolbox-unmanaged.conf
# logrotate en tmpfiles (geroteerde audit-bestanden blijven staan)
sudo rm /etc/logrotate.d/pitoolbox /etc/tmpfiles.d/pitoolbox.conf
# BlueZ: zet de backup terug (of haal de drie pitoolbox-regels onder [LE] weg)
ls /var/backups/pitoolbox/
sudo cp -p /var/backups/pitoolbox/<tijdstempel>/etc/bluetooth/main.conf /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
sudo reboot
```

Had je al een eigen versie van een van deze bestanden, zet die dan terug uit `/var/backups/pitoolbox/<tijdstempel>/` (zelfde pad onder `/etc`).

## 6. Als de namen toch verwisselen

Symptoom: `wlan1` heeft driver `brcmfmac`, of udev meldt in de journal dat hernoemen mislukt ("File exists"). **Gebruik pitoolbox dan niet:** de code beschermt de uplink op naam, dus `alfa_set_mode` zou de ingebouwde wifi omzetten. NetworkManager houdt wel de goede adapter (die matcht op driver). Oplossing, zoals systemd.link(5) aanraadt: namen buiten de naamruimte van de kernel.

1. Pas `Name=` in de twee bestanden in `/etc/systemd/network/` aan, bijv. naar `uplink0` en `alfa0`.
2. Zet in `/etc/pitoolbox/pitoolbox.env` `PITOOLBOX_UPLINK_IFACE=uplink0` en `PITOOLBOX_ALFA_IFACE=alfa0`.
3. Draai `sudo update-initramfs -u -k all` en daarna `sudo reboot`.

Aanbeveling voor later (code, niet in deze fase): laat `AlfaAdapter` vóór monitor mode controleren dat de driver van de interface `mt7921u` is (`/sys/class/net/<if>/device/driver`). Dan helpt een verwisseling nooit meer de uplink om zeep.

## 7. Hardware- en testchecklist

- [ ] `deploy/install.sh --dry-run --onderweg` op de Pi: toont 5 nieuwe bestanden, schrijft niets
- [ ] `sudo deploy/install.sh --alleen-systeem --onderweg`: de controles melden `wlan0: 10-pitoolbox-wlan0.link` en `wlan1: 11-pitoolbox-wlan1.link`, NM leest de drop-in, `logrotate -d` zonder fout, initramfs bijgewerkt
- [ ] Tweede run: alles "ongewijzigd", geen nieuwe backups
- [ ] Na reboot: `wlan0` = brcmfmac, `wlan1` = mt7921u, `ID_NET_LINK_FILE` klopt; 5× herhaald, ook met de Alfa vóór en ná het opstarten ingeplugd
- [ ] Alfa eruit en erin tijdens gebruik: komt terug als `wlan1`, blijft unmanaged
- [ ] `nmcli` toont `wlan1` unmanaged en `wlan0` verbonden. Ethernet eruit: de Pi is bereikbaar via `wlan0` (hotspot)
- [ ] `alfa_set_mode monitor` blijft staan (geen NM-reset); `wifi_scan airodump` en `capture_start`/`capture_stop` werken onder systemd
- [ ] `status` meldt uplink `wlan0` (geen `eth0` meer; `~/.zshenv` opgeschoond)
- [ ] Geforceerde logrotate: oude regels staan in het geroteerde bestand, de volgende tool-call maakt `audit.jsonl` opnieuw aan met 0600 pitoolbox; geen regel kwijt
- [ ] `logrotate.timer` actief; na een maand (of met `maxsize`) een `.gz` naast het nieuwste geroteerde bestand
- [ ] Captures blijven na een herstart en na `systemd-tmpfiles --clean` gewoon staan
- [ ] Optioneel `--bluez-le`: `conn_min_interval`/`conn_max_interval` = 6/12 na `systemctl restart bluetooth`; meet `ble-test` en noteer of de wifi van de Pi merkbaar trager wordt
- [ ] Terugdraaien (sectie 5) op een testmoment: na een reboot weer de oude situatie
- [ ] Een paar uur onderweg op een powerbank: `status` → geen onderspanning (`get_throttled`)

## Bronnen

- systemd.link(5), udev(7), udevadm(8), tmpfiles.d(5): man-pages van systemd 262 op de Pi; online <https://www.freedesktop.org/software/systemd/man/latest/systemd.link.html>.
- Debian-wiki, NetworkInterfaceNames ("canonical .link approach", "initrd skew"): <https://wiki.debian.org/NetworkInterfaceNames>.
- NetworkManager.conf(5): man-page van NetworkManager 1.58.1 op de Pi; online <https://networkmanager.dev/docs/api/latest/NetworkManager.conf.html>.
- logrotate(8): man-page van logrotate 3.22.0 op de Pi.
- BlueZ `src/main.c` (`load_config`, `parse_le_config`): <https://github.com/bluez/bluez/blob/master/src/main.c>; bluetoothd(8) van BlueZ 5.87.
- Gedrag van de audit-log: `src/pitoolbox/core/audit.py` (`AuditLog.write`).
