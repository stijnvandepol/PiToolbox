#!/usr/bin/env bash
# pitoolbox - fase 2 + 5: installeer de service op de Pi (Kali ARM64, Pi 4 of 5).
#
# Gebruik (vanuit een checkout van de repo):  sudo deploy/install.sh [opties]
# Zonder opties: alleen de service (fase 2, stap 1-8 hieronder), precies zoals voorheen.
# Idempotent: opnieuw draaien werkt code, venv en units bij en overschrijft NOOIT je
# /etc/pitoolbox/pitoolbox.env, /etc/pitoolbox/scope.yaml, auth-database, audit-log of captures.
#
# Opties voor fase 5 "klaar voor onderweg" (zie docs/fase5-onderweg.md, `--help`):
#   --onderweg        ook: vaste interfacenamen (wlan0 = ingebouwd, wlan1 = Alfa) via systemd
#                     .link, NetworkManager laat de Alfa met rust, logrotate voor de audit-log,
#                     tmpfiles-regel voor de capture-map
#   --bluez-le        ook: korter BLE-verbindingsinterval in /etc/bluetooth/main.conf
#                     (optioneel, NIET getest)
#   --alleen-systeem  sla stap 1-8 (de service zelf) over, doe alleen de gekozen fase-5-stappen
#   --dry-run         schrijf niets, toon per bestand wat er zou veranderen (diff); impliceert
#                     --alleen-systeem en heeft geen root nodig
#   -y, --ja          niet vragen (voor elke fase-5-stap wordt anders om bevestiging gevraagd)
# Een systeembestand dat al bestaat en anders is, gaat eerst naar
# /var/backups/pitoolbox/<tijdstempel>/<volledig pad>. Een bestand dat al klopt, wordt niet aangeraakt.
# DESTDIR=/pad (alleen voor tests): fase-5-bestanden onder dat pad in plaats van /, zonder root,
# zonder chown en zonder iets op het draaiende systeem te controleren of te herladen.
#
# Wat dit script doet:
#   1. maakt systeemgebruikers/groepen 'pitoolbox' (in dialout + video + bluetooth) en 'cloudflared'
#   2. maakt /opt/pitoolbox (code), /etc/pitoolbox (0750), /var/lib/pitoolbox (0700, state),
#      /etc/cloudflared (0750 root:cloudflared)
#   3. kopieert de repo naar /opt/pitoolbox
#   4. installeert uv voor gebruiker pitoolbox als die het nog niet heeft (officiele installer)
#   5. `uv sync --frozen --no-dev` in /opt/pitoolbox
#   6. zet configbestanden neer (pitoolbox.env, scope.yaml) als ze nog niet bestaan
#   7. zet de systemd-units neer en doet `systemctl daemon-reload`
#   8. controleert of de systeemtools er zijn (iw, ip, airodump-ng, vcgencmd)
#   9. alleen met --onderweg / --bluez-le: de fase-5-systeemconfig (opties hierboven)
# Wat het NIET doet: enablen/starten van units, cloudflared installeren of inloggen,
# credentials aanraken, pitoolbox-auth init draaien.
#
# Bronnen: uv-installer https://docs.astral.sh/uv/getting-started/installation/ en
# https://docs.astral.sh/uv/reference/installer/ (UV_INSTALL_DIR, UV_NO_MODIFY_PATH);
# `uv sync --frozen / --no-dev` https://docs.astral.sh/uv/reference/cli/;
# useradd(8)/runuser(1) staan niet online gecontroleerd (niet geverifieerd, standaard op Debian/Kali).
#
# Omgevingsvariabelen (optioneel):
#   PITOOLBOX_SKIP_UV_INSTALL=1   installeer uv niet zelf (je moet het dan zelf beschikbaar maken
#                                 voor gebruiker pitoolbox, of PITOOLBOX_UV_BIN zetten)
#   PITOOLBOX_UV_BIN=/pad/uv      gebruik deze uv i.p.v. de standaardlocatie

set -euo pipefail

APP_USER="pitoolbox"
TUNNEL_USER="cloudflared"
APP_DIR="/opt/pitoolbox"
CONF_DIR="/etc/pitoolbox"
STATE_DIR="/var/lib/pitoolbox"
CF_DIR="/etc/cloudflared"
UNIT_DIR="/etc/systemd/system"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"
UV_BIN_DIR="${STATE_DIR}/.local/bin"
UV_BIN="${PITOOLBOX_UV_BIN:-${UV_BIN_DIR}/uv}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WAARSCHUWING: %s\n' "$*" >&2; }
die()  { printf 'FOUT: %s\n' "$*" >&2; exit 1; }

# Voer een commando uit als gebruiker pitoolbox, met een eigen HOME en cache.
as_app() {
    runuser -u "${APP_USER}" -- env \
        HOME="${STATE_DIR}" \
        UV_CACHE_DIR="${STATE_DIR}/.cache/uv" \
        "$@"
}

# --- opties -----------------------------------------------------------------------------------
usage() {
    cat <<'EOF'
Gebruik: sudo deploy/install.sh [--onderweg] [--bluez-le] [--alleen-systeem] [--dry-run] [-y]

  (geen opties)     installeer/werk de service bij (fase 2), zoals altijd
  --onderweg        ook de fase-5-systeemconfig: .link-bestanden (wlan0/wlan1),
                    NetworkManager unmanaged voor de Alfa, logrotate, tmpfiles
  --bluez-le        ook MinConnectionInterval/MaxConnectionInterval in /etc/bluetooth/main.conf
                    (optioneel, niet getest; zie docs/fase3-ble.md "BLE sneller maken")
  --alleen-systeem  alleen de gekozen fase-5-stappen, de service zelf overslaan
  --dry-run         niets schrijven, alleen tonen wat er zou veranderen (geen root nodig)
  -y, --ja          niet om bevestiging vragen
  -h, --help        deze tekst

Details, controle en terugdraaien: docs/fase5-onderweg.md
EOF
}

DO_ONDERWEG=0
DO_BLUEZ=0
ALLEEN_SYSTEEM=0
DRY_RUN=0
JA=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --onderweg) DO_ONDERWEG=1 ;;
        --bluez-le) DO_BLUEZ=1 ;;
        --alleen-systeem) ALLEEN_SYSTEEM=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -y | --ja) JA=1 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "onbekende optie: $1" ;;
    esac
    shift
done

# Alleen voor tests: alle fase-5-doelpaden onder DESTDIR (zie de kop van dit script).
DESTDIR="${DESTDIR:-}"
DESTDIR="${DESTDIR%/}"
if [[ "${DRY_RUN}" -eq 1 || -n "${DESTDIR}" ]]; then
    ALLEEN_SYSTEEM=1
fi
if [[ "${ALLEEN_SYSTEEM}" -eq 1 && "${DO_ONDERWEG}" -eq 0 && "${DO_BLUEZ}" -eq 0 ]]; then
    die "--alleen-systeem/--dry-run/DESTDIR zonder --onderweg of --bluez-le doet niets"
fi

# --- fase 5: doelen en hulpfuncties -----------------------------------------------------------
LINK_DIR="${DESTDIR}/etc/systemd/network"
NM_CONF_DIR="${DESTDIR}/etc/NetworkManager/conf.d"
LOGROTATE_DIR="${DESTDIR}/etc/logrotate.d"
TMPFILES_DIR="${DESTDIR}/etc/tmpfiles.d"
BLUEZ_CONF="${DESTDIR}/etc/bluetooth/main.conf"
BACKUP_DIR="${DESTDIR}/var/backups/pitoolbox/$(date +%Y%m%d-%H%M%S)"
LINK_FILES=(10-pitoolbox-wlan0.link 11-pitoolbox-wlan1.link)
# Echte installatie: alles root:root. Met DESTDIR (tests, geen root) geen chown.
if [[ -n "${DESTDIR}" ]]; then
    OWNER_ARGS=()
else
    OWNER_ARGS=(-o root -g root)
fi

# Vraag om bevestiging. Met --ja of --dry-run altijd "ja"; zonder terminal stoppen we liever
# dan stil iets te doen of over te slaan.
bevestig() {
    if [[ "${JA}" -eq 1 || "${DRY_RUN}" -eq 1 ]]; then
        return 0
    fi
    [[ -t 0 ]] || die "geen terminal om te vragen ('$1'); gebruik --ja"
    local antwoord=""
    read -r -p "    $1 [j/N] " antwoord
    [[ "${antwoord}" =~ ^[jJyY] ]]
}

# Kopie van een bestaand doelbestand naar BACKUP_DIR, met het volledige pad eronder. Bewust buiten
# /etc: een backup in /etc/logrotate.d of conf.d zou zelf weer worden ingelezen.
backup() {
    local rel="${1#"${DESTDIR}"}"
    install -d -m 0700 "${BACKUP_DIR}$(dirname -- "${rel}")"
    cp -p -- "$1" "${BACKUP_DIR}${rel}"
    info "backup: ${BACKUP_DIR}${rel}"
}

# Zet bronbestand $1 neer als $2 (0644). Exitcode 0 = (zou) gewijzigd, 1 = klopte al.
plaats() {
    local src="$1" dest="$2"
    [[ -f "${src}" ]] || die "bronbestand ontbreekt: ${src}"
    if [[ -f "${dest}" && ! -L "${dest}" ]] && cmp -s -- "${src}" "${dest}"; then
        info "ongewijzigd: ${dest}"
        return 1
    fi
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        if [[ -e "${dest}" || -L "${dest}" ]]; then
            info "zou vervangen (met backup): ${dest}"
            diff -u -- "${dest}" "${src}" | sed 's/^/        /' || true
        else
            info "zou aanmaken: ${dest}"
        fi
        return 0
    fi
    if [[ -e "${dest}" || -L "${dest}" ]]; then
        backup "${dest}"
    fi
    install -d -m 0755 "${OWNER_ARGS[@]}" "$(dirname -- "${dest}")"
    install -m 0644 "${OWNER_ARGS[@]}" -- "${src}" "${dest}"
    info "geplaatst: ${dest}"
    return 0
}

# Waarde van sleutel $3 in sectie [$2] van ini-bestand $1 (laatste wint, leeg = niet gezet).
# Commentaarregels (#) tellen niet mee.
conf_waarde() {
    awk -v sec="$2" -v key="$3" '
        /^[[:space:]]*\[/ {
            s = $0; gsub(/^[[:space:]]*\[|\][[:space:]]*$/, "", s); insec = (s == sec); next
        }
        insec && $0 ~ ("^[[:space:]]*" key "[[:space:]]*=") {
            v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); sub(/[[:space:]]+$/, "", v); val = v
        }
        END { if (val != "") print val }
    ' "$1"
}

app_gebruiker_bestaat() { getent passwd "${APP_USER}" >/dev/null 2>&1; }

# 5a. Vaste interfacenamen via systemd .link (bron en motivatie: deploy/network/*.link).
fase5_namen() {
    log "Fase 5: vaste interfacenamen (ingebouwd = wlan0, Alfa = wlan1)"
    bevestig "systemd .link-bestanden voor wlan0/wlan1 plaatsen?" || { info "overgeslagen"; return 0; }
    local f gewijzigd=0
    for f in "${LINK_FILES[@]}"; do
        if plaats "${SRC_DIR}/deploy/network/${f}" "${LINK_DIR}/${f}"; then
            gewijzigd=1
        fi
    done
    if [[ "${DRY_RUN}" -eq 1 || -n "${DESTDIR}" ]]; then
        return 0
    fi
    if command -v udevadm >/dev/null 2>&1; then
        udevadm control --reload || warn "udevadm control --reload mislukt"
        # Controle zonder iets te hernoemen: welke .link past udev nu toe op elke wifi-interface?
        local dev naam uitvoer
        for dev in /sys/class/net/*; do
            naam="${dev##*/}"
            [[ -d "${dev}/wireless" || -d "${dev}/phy80211" ]] || continue
            uitvoer="$(udevadm test-builtin net_setup_link "${dev}" 2>&1 || true)"
            if grep -q "pitoolbox-wlan" <<<"${uitvoer}"; then
                info "${naam}: $(grep -o '[0-9]*-pitoolbox-wlan[01]\.link' <<<"${uitvoer}" | head -n1)"
            else
                warn "${naam}: geen pitoolbox-.link van toepassing (zie docs/fase5-onderweg.md)"
            fi
        done
    else
        warn "udevadm niet gevonden; controleer na een herstart met 'ip link'"
    fi
    # De udev-hook van initramfs-tools kopieert /etc/systemd/network/*.link naar de initramfs
    # (/usr/share/initramfs-tools/hooks/udev). Zonder bijwerken kan die een oude versie gebruiken.
    if [[ "${gewijzigd}" -eq 1 ]] && command -v update-initramfs >/dev/null 2>&1; then
        if bevestig "initramfs bijwerken (update-initramfs -u -k all, duurt even)?"; then
            update-initramfs -u -k all
        else
            warn "initramfs niet bijgewerkt: doe dit voor de herstart (update-initramfs -u -k all)"
        fi
    fi
}

# 5b. NetworkManager laat de Alfa met rust (bron: deploy/NetworkManager/90-pitoolbox-unmanaged.conf).
fase5_networkmanager() {
    log "Fase 5: NetworkManager beheert de Alfa (driver mt7921u) niet meer"
    bevestig "NetworkManager-drop-in plaatsen?" || { info "overgeslagen"; return 0; }
    local f=90-pitoolbox-unmanaged.conf
    plaats "${SRC_DIR}/deploy/NetworkManager/${f}" "${NM_CONF_DIR}/${f}" || true
    if [[ "${DRY_RUN}" -eq 1 || -n "${DESTDIR}" ]]; then
        return 0
    fi
    if command -v NetworkManager >/dev/null 2>&1; then
        # --print-config leest en voegt alle configbestanden samen, zonder de daemon te raken.
        if NetworkManager --print-config 2>/dev/null | grep -q 'unmanaged-devices=.*driver:mt7921u'; then
            info "NetworkManager --print-config: unmanaged-devices bevat driver:mt7921u"
        else
            warn "driver:mt7921u staat niet in 'NetworkManager --print-config'; controleer conf.d"
        fi
    else
        warn "NetworkManager niet gevonden; de drop-in doet dan niets"
    fi
}

# 5c. logrotate voor de audit-log + tmpfiles-regel voor captures.
fase5_logrotate() {
    log "Fase 5: logrotate (audit-log) en tmpfiles (capture-map)"
    bevestig "logrotate- en tmpfiles-bestand plaatsen?" || { info "overgeslagen"; return 0; }
    plaats "${SRC_DIR}/deploy/logrotate/pitoolbox" "${LOGROTATE_DIR}/pitoolbox" || true
    plaats "${SRC_DIR}/deploy/tmpfiles/pitoolbox.conf" "${TMPFILES_DIR}/pitoolbox.conf" || true
    if [[ "${DRY_RUN}" -eq 1 || -n "${DESTDIR}" ]]; then
        return 0
    fi
    if ! app_gebruiker_bestaat; then
        warn "gebruiker ${APP_USER} bestaat niet: logrotate en tmpfiles falen tot de service is geinstalleerd"
        return 0
    fi
    # -d: alleen lezen en tonen, niets roteren (logrotate(8)).
    local uitvoer
    if uitvoer="$(logrotate -d "${LOGROTATE_DIR}/pitoolbox" 2>&1)" && ! grep -q '^error' <<<"${uitvoer}"; then
        info "logrotate -d: config is geldig"
    else
        warn "logrotate -d meldt een fout:"
        grep '^error' <<<"${uitvoer}" | sed 's/^/        /' >&2 || true
    fi
    if systemd-tmpfiles --create "${TMPFILES_DIR}/pitoolbox.conf"; then
        info "systemd-tmpfiles --create: capture-map in orde"
    else
        warn "systemd-tmpfiles --create mislukt"
    fi
}

# 5d. Optioneel, niet getest: BLE-verbindingsinterval in main.conf (geen drop-in mogelijk).
fase5_bluez() {
    log "Fase 5 (optioneel, niet getest): BLE-verbindingsinterval in ${BLUEZ_CONF}"
    local snippet="${SRC_DIR}/deploy/bluetooth/main.conf.le-snippet"
    if [[ ! -f "${BLUEZ_CONF}" ]]; then
        warn "${BLUEZ_CONF} bestaat niet (bluez niet geinstalleerd?); overgeslagen"
        return 0
    fi
    local key want have plan=()
    for key in MinConnectionInterval MaxConnectionInterval; do
        want="$(conf_waarde "${snippet}" LE "${key}")"
        [[ "${want}" =~ ^[0-9]+$ ]] || die "geen geldige ${key} in ${snippet}"
        have="$(conf_waarde "${BLUEZ_CONF}" LE "${key}")"
        if [[ -z "${have}" ]]; then
            plan+=("${key}=${want}")
        elif [[ "${have}" == "${want}" ]]; then
            info "${key}=${have} staat er al"
        else
            # Een eigen keuze van de gebruiker overschrijven we nooit.
            warn "${key} staat al op ${have} (pitoolbox wil ${want}); main.conf NIET aangepast"
            return 0
        fi
    done
    if [[ ${#plan[@]} -eq 0 ]]; then
        info "ongewijzigd: ${BLUEZ_CONF}"
        return 0
    fi
    bevestig "${plan[*]} toevoegen onder [LE] in ${BLUEZ_CONF}?" || { info "overgeslagen"; return 0; }
    local tmp_conf
    tmp_conf="$(mktemp)"
    # Direct na de kop [LE] invoegen; bestaat die sectie niet, dan achteraan een nieuwe [LE].
    awk -v lines="$(printf '%s\n' "${plan[@]}")" \
        -v marker="# pitoolbox (fase 5, deploy/install.sh --bluez-le): zie docs/fase3-ble.md" '
        { print }
        !done && /^[[:space:]]*\[LE\][[:space:]]*$/ { print marker; print lines; done = 1 }
        END { if (!done) { print ""; print "[LE]"; print marker; print lines } }
    ' "${BLUEZ_CONF}" >"${tmp_conf}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        info "zou wijzigen (met backup): ${BLUEZ_CONF}"
        diff -u -- "${BLUEZ_CONF}" "${tmp_conf}" | sed 's/^/        /' || true
        rm -f -- "${tmp_conf}"
        return 0
    fi
    backup "${BLUEZ_CONF}"
    install -m "$(stat -c '%a' "${BLUEZ_CONF}")" "${OWNER_ARGS[@]}" -- "${tmp_conf}" "${BLUEZ_CONF}"
    rm -f -- "${tmp_conf}"
    info "aangepast: ${BLUEZ_CONF}"
    info "activeren (verbreekt BLE-verbindingen): sudo systemctl restart bluetooth"
}

fase5() {
    for cmd in install cmp diff awk date; do
        command -v "${cmd}" >/dev/null 2>&1 || die "commando '${cmd}' ontbreekt"
    done
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "DRY-RUN: er wordt niets geschreven"
    fi
    if [[ "${DO_ONDERWEG}" -eq 1 ]]; then
        fase5_namen
        fase5_networkmanager
        fase5_logrotate
    fi
    if [[ "${DO_BLUEZ}" -eq 1 ]]; then
        fase5_bluez
    fi
    if [[ "${DRY_RUN}" -eq 0 && -z "${DESTDIR}" && "${DO_ONDERWEG}" -eq 1 ]]; then
        cat <<EOF

Fase 5: herstart de Pi (sudo reboot) om de interfacenamen en NetworkManager te activeren.
Controle na de herstart en terugdraaien: docs/fase5-onderweg.md
EOF
    fi
}

if [[ "${ALLEEN_SYSTEEM}" -eq 1 ]]; then
    if [[ "${DRY_RUN}" -eq 0 && -z "${DESTDIR}" && "${EUID}" -ne 0 ]]; then
        die "draai dit met sudo (of gebruik --dry-run)"
    fi
    fase5
    exit 0
fi

# --- 0. controles -----------------------------------------------------------------------------
[[ "${EUID}" -eq 0 ]] || die "draai dit met sudo: sudo deploy/install.sh"
for cmd in useradd groupadd usermod getent install tar runuser systemctl; do
    command -v "${cmd}" >/dev/null 2>&1 || die "commando '${cmd}' ontbreekt"
done
[[ -f "${SRC_DIR}/pyproject.toml" && -f "${SRC_DIR}/uv.lock" ]] \
    || die "geen pyproject.toml/uv.lock in ${SRC_DIR}: draai dit vanuit een volledige checkout"
[[ -f "${SRC_DIR}/deploy/systemd/pitoolbox-mcp.service" ]] || die "units ontbreken in ${SRC_DIR}/deploy"

# --- 1. gebruikers en groepen -----------------------------------------------------------------
log "Gebruikers en groepen"
if ! getent group "${APP_USER}" >/dev/null; then
    groupadd --system "${APP_USER}"
    info "groep ${APP_USER} aangemaakt"
fi
if ! getent passwd "${APP_USER}" >/dev/null; then
    # Systeemgebruiker zonder login; home = state-map (hier staan uv, cache en state).
    useradd --system --gid "${APP_USER}" --home-dir "${STATE_DIR}" \
        --shell /usr/sbin/nologin --comment "pitoolbox service" "${APP_USER}"
    info "gebruiker ${APP_USER} aangemaakt"
else
    info "gebruiker ${APP_USER} bestaat al"
fi
# dialout: Flipper-serial (/dev/ttyACM*); video: vcgencmd (/dev/vcio); bluetooth: BlueZ over D-Bus
# (fase 3). Dit staat ook als SupplementaryGroups= in de unit; lidmaatschap hier is nodig voor
# handmatig testen als die gebruiker.
# Een groep die in SupplementaryGroups= staat maar niet bestaat laat de unit falen. Normaal maakt
# het bluez-pakket 'bluetooth' aan; zonder bluez maken we hem hier leeg aan, zodat een
# installatie met alleen USB ook start.
if ! getent group bluetooth >/dev/null; then
    warn "groep 'bluetooth' bestaat niet (bluez niet geïnstalleerd?); maak hem aan zodat de unit start"
    groupadd --system bluetooth
fi
for grp in dialout video bluetooth; do
    if getent group "${grp}" >/dev/null; then
        usermod --append --groups "${grp}" "${APP_USER}"
    else
        warn "groep '${grp}' bestaat niet; controleer de Flipper/vcgencmd-rechten zelf"
    fi
done

if ! getent group "${TUNNEL_USER}" >/dev/null; then
    groupadd --system "${TUNNEL_USER}"
fi
if ! getent passwd "${TUNNEL_USER}" >/dev/null; then
    useradd --system --gid "${TUNNEL_USER}" --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --comment "cloudflared (pitoolbox tunnel)" "${TUNNEL_USER}"
    info "gebruiker ${TUNNEL_USER} aangemaakt"
else
    info "gebruiker ${TUNNEL_USER} bestaat al"
fi

# --- 2. mappen --------------------------------------------------------------------------------
log "Mappen"
install -d -m 0755 -o "${APP_USER}" -g "${APP_USER}" "${APP_DIR}"
# Config: root-eigendom met groepleesrecht, zodat de service zijn scope niet kan overschrijven.
install -d -m 0750 -o root -g "${APP_USER}" "${CONF_DIR}"
# State: alleen de service (auth-db, audit-log, captures).
install -d -m 0700 -o "${APP_USER}" -g "${APP_USER}" "${STATE_DIR}"
install -d -m 0700 -o "${APP_USER}" -g "${APP_USER}" "${STATE_DIR}/captures"
# Plek voor tunnelconfig + credentials (leg je zelf neer, zie docs/fase2-remote.md).
install -d -m 0750 -o root -g "${TUNNEL_USER}" "${CF_DIR}"
info "${APP_DIR}  ${CONF_DIR}  ${STATE_DIR}  ${CF_DIR}"

# --- 3. code kopieren -------------------------------------------------------------------------
log "Code naar ${APP_DIR}"
if [[ "${SRC_DIR}" == "${APP_DIR}" ]]; then
    info "bron is al ${APP_DIR}, overslaan"
else
    # Geen .git, geen venv, geen lokale config of data. Verwijdert geen oude bestanden.
    tar -C "${SRC_DIR}" \
        --exclude='./.git' --exclude='./.venv' --exclude='__pycache__' \
        --exclude='.pytest_cache' --exclude='.mypy_cache' --exclude='.ruff_cache' \
        --exclude='*.env' --exclude='./scope.yaml' --exclude='./captures' \
        --exclude='audit*.jsonl' -cf - . \
        | tar -C "${APP_DIR}" --no-same-owner -xf -
    chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
    info "gekopieerd van ${SRC_DIR}"
fi

# --- 4. uv ------------------------------------------------------------------------------------
log "uv voor gebruiker ${APP_USER}"
if [[ -x "${UV_BIN}" ]]; then
    info "uv gevonden: ${UV_BIN}"
elif [[ "${PITOOLBOX_SKIP_UV_INSTALL:-0}" == "1" ]]; then
    die "uv ontbreekt (${UV_BIN}) en PITOOLBOX_SKIP_UV_INSTALL=1 staat aan"
else
    info "uv ontbreekt. Dit script gaat nu:"
    info "  1. het officiele installatiescript downloaden van ${UV_INSTALL_URL}"
    info "  2. de SHA-256 ervan tonen (zodat je het kunt vergelijken/inspecteren)"
    info "  3. het uitvoeren ALS gebruiker ${APP_USER}, met UV_INSTALL_DIR=${UV_BIN_DIR}"
    info "     en UV_NO_MODIFY_PATH=1 (geen shell-profielen aanpassen)"
    command -v curl >/dev/null 2>&1 || die "curl ontbreekt (sudo apt install curl)"
    tmp="$(mktemp -d)"
    trap 'rm -rf "${tmp}"' EXIT
    curl -LsSf -o "${tmp}/uv-install.sh" "${UV_INSTALL_URL}"
    info "sha256: $(sha256sum "${tmp}/uv-install.sh" | cut -d' ' -f1)"
    chmod 0755 "${tmp}"
    chmod 0644 "${tmp}/uv-install.sh"
    install -d -m 0755 -o "${APP_USER}" -g "${APP_USER}" "${STATE_DIR}/.local" "${UV_BIN_DIR}"
    runuser -u "${APP_USER}" -- env \
        HOME="${STATE_DIR}" UV_INSTALL_DIR="${UV_BIN_DIR}" UV_NO_MODIFY_PATH=1 \
        sh "${tmp}/uv-install.sh"
    [[ -x "${UV_BIN}" ]] || die "uv-installatie mislukt: ${UV_BIN} niet gevonden"
    # ~/.local zelf niet wereld-leesbaar laten staan (STATE_DIR is al 0700, dit is extra netjes).
    chmod 0700 "${STATE_DIR}/.local"
fi
info "$(as_app "${UV_BIN}" --version)"

# --- 5. venv ----------------------------------------------------------------------------------
log "uv sync --frozen --no-dev (kan op de Pi een paar minuten duren)"
# --frozen: precies de versies uit uv.lock, geen herberekening. --no-dev: geen test/lint-tools.
# Is de systeem-Python te oud (< 3.11), dan mag uv er zelf een downloaden (standaardgedrag; niet
# geverifieerd waar uv die dan opslaat - onder HOME=${STATE_DIR}).
( cd "${APP_DIR}" && as_app "${UV_BIN}" sync --frozen --no-dev )
for ep in pitoolbox-mcp pitoolbox-auth; do
    if [[ -x "${APP_DIR}/.venv/bin/${ep}" ]]; then
        info "ok: ${APP_DIR}/.venv/bin/${ep}"
    else
        warn "${APP_DIR}/.venv/bin/${ep} ontbreekt (staat het entry point in pyproject.toml?)"
    fi
done

# --- 6. configbestanden (alleen als ze nog niet bestaan) --------------------------------------
log "Configuratie"
if [[ ! -e "${CONF_DIR}/pitoolbox.env" ]]; then
    install -m 0600 -o "${APP_USER}" -g "${APP_USER}" \
        "${SRC_DIR}/deploy/pitoolbox.env.example" "${CONF_DIR}/pitoolbox.env"
    info "${CONF_DIR}/pitoolbox.env aangemaakt uit het voorbeeld: PAS PITOOLBOX_PUBLIC_URL AAN"
else
    info "${CONF_DIR}/pitoolbox.env bestaat al, niet aangeraakt"
fi
# Voorbeeld ter referentie naast het echte bestand (zonder geheimen).
install -m 0640 -o root -g "${APP_USER}" \
    "${SRC_DIR}/deploy/pitoolbox.env.example" "${CONF_DIR}/pitoolbox.env.example"
if [[ ! -e "${CONF_DIR}/scope.yaml" ]]; then
    install -m 0640 -o root -g "${APP_USER}" "${SRC_DIR}/scope.example.yaml" "${CONF_DIR}/scope.yaml"
    info "${CONF_DIR}/scope.yaml aangemaakt uit scope.example.yaml: VUL JE EIGEN APPARATEN IN"
else
    info "${CONF_DIR}/scope.yaml bestaat al, niet aangeraakt"
fi
# Controleer eigendom en rechten ook als het bestand er al stond.
chown "${APP_USER}:${APP_USER}" "${CONF_DIR}/pitoolbox.env"
chmod 0600 "${CONF_DIR}/pitoolbox.env"

# --- 7. systemd-units -------------------------------------------------------------------------
log "systemd-units"
install -m 0644 -o root -g root "${SRC_DIR}/deploy/systemd/pitoolbox-mcp.service" "${UNIT_DIR}/"
install -m 0644 -o root -g root "${SRC_DIR}/deploy/systemd/cloudflared-pitoolbox.service" "${UNIT_DIR}/"
# Voorbeeld-tunnelconfig ter referentie (de echte config met je UUID maak je zelf).
install -m 0644 -o root -g root \
    "${SRC_DIR}/deploy/cloudflared/config.yml" "${CF_DIR}/pitoolbox.yml.example"
systemctl daemon-reload
info "units geinstalleerd; niets is geenabled of gestart"

# --- 8. controle op systeemtools --------------------------------------------------------------
for t in iw ip airodump-ng vcgencmd; do
    command -v "${t}" >/dev/null 2>&1 \
        || warn "'${t}' niet gevonden (sudo apt install iw iproute2 aircrack-ng libraspberrypi-bin)"
done

# --- 9. fase 5 (alleen met --onderweg en/of --bluez-le) ---------------------------------------
if [[ "${DO_ONDERWEG}" -eq 1 || "${DO_BLUEZ}" -eq 1 ]]; then
    fase5
fi

cat <<EOF

Klaar. Volgende stappen (zie docs/fase2-remote.md voor de details):

  1. cloudflared installeren (officiele apt-repo), daarna als jouw gebruiker:
       cloudflared tunnel login
       cloudflared tunnel create pitoolbox
       cloudflared tunnel route dns pitoolbox <jouw-hostnaam>
  2. tunnelconfig en credentials neerleggen:
       sudo cp ${CF_DIR}/pitoolbox.yml.example ${CF_DIR}/pitoolbox.yml   # UUID + hostnaam invullen
       sudo install -o root -g ${TUNNEL_USER} -m 0640 ~/.cloudflared/<UUID>.json ${CF_DIR}/
  3. ${CONF_DIR}/pitoolbox.env: zet PITOOLBOX_PUBLIC_URL op https://<jouw-hostnaam>
     ${CONF_DIR}/scope.yaml: vul je eigen BSSID's en IR-bestanden in
  4. wachtwoord + TOTP instellen (als gebruiker ${APP_USER}, zodat de database juist eigendom heeft):
       sudo -u ${APP_USER} bash -c 'set -a; . ${CONF_DIR}/pitoolbox.env; set +a; exec ${APP_DIR}/.venv/bin/pitoolbox-auth init'
  5. starten:
       sudo systemctl enable --now pitoolbox-mcp cloudflared-pitoolbox
       journalctl -u pitoolbox-mcp -u cloudflared-pitoolbox -f

EOF
