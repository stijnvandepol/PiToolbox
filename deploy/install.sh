#!/usr/bin/env bash
# pitoolbox - fase 2: installeer de service op de Pi (Kali ARM64).
#
# Gebruik (vanuit een checkout van de repo):  sudo deploy/install.sh
# Idempotent: opnieuw draaien werkt code, venv en units bij en overschrijft NOOIT je
# /etc/pitoolbox/pitoolbox.env, /etc/pitoolbox/scope.yaml, auth-database, audit-log of captures.
#
# Wat dit script doet:
#   1. maakt systeemgebruikers/groepen 'pitoolbox' (in dialout + video) en 'cloudflared'
#   2. maakt /opt/pitoolbox (code), /etc/pitoolbox (0750), /var/lib/pitoolbox (0700, state),
#      /etc/cloudflared (0750 root:cloudflared)
#   3. kopieert de repo naar /opt/pitoolbox
#   4. installeert uv voor gebruiker pitoolbox als die het nog niet heeft (officiele installer)
#   5. `uv sync --frozen --no-dev` in /opt/pitoolbox
#   6. zet de systemd-units neer en doet `systemctl daemon-reload`
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
# dialout: Flipper-serial (/dev/ttyACM*); video: vcgencmd (/dev/vcio). Dit staat ook als
# SupplementaryGroups= in de unit; lidmaatschap hier is nodig voor handmatig testen als die gebruiker.
for grp in dialout video; do
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
