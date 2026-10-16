"""Statische en lokale controles van de deploybestanden (fase 2 en 5).

Er is geen root nodig en het systeem wordt nergens aangeraakt. install.sh draait alleen met
DESTDIR in tmp_path. NetworkManager en logrotate draaien alleen in hun eigen lees- of testmodus,
en alleen als ze geïnstalleerd zijn. De udev-properties in tests/fixtures/deploy/ zijn opgenomen
op de echte Pi 4.
"""

# S603/S607: de subprocessen hieronder zijn vaste argv-lijsten met vaste tools (bash, logrotate,
# NetworkManager, shellcheck) op bestanden uit de repo of tmp_path; geen invoer van buiten.
# ruff: noqa: S603, S607

from __future__ import annotations

import configparser
import fnmatch
import getpass
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from pitoolbox.core.audit import AuditLog

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
INSTALL_SH = DEPLOY / "install.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "deploy"
LINK_FILES = sorted((DEPLOY / "network").glob("*.link"))
NM_CONF = DEPLOY / "NetworkManager" / "90-pitoolbox-unmanaged.conf"
LOGROTATE = DEPLOY / "logrotate" / "pitoolbox"
TMPFILES = DEPLOY / "tmpfiles" / "pitoolbox.conf"
BLUEZ_SNIPPET = DEPLOY / "bluetooth" / "main.conf.le-snippet"
MCP_UNIT = DEPLOY / "systemd" / "pitoolbox-mcp.service"

linux_only = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None,
    reason="install.sh gebruikt bash en GNU-coreutils (Linux)",
)


# --- hulpfuncties -------------------------------------------------------------------------------


def _ini(path: Path) -> configparser.ConfigParser:
    """Ini-achtige config (systemd, NM): hoofdlettergevoelige sleutels, geen interpolatie."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(path.read_text(encoding="utf-8"), source=str(path))
    return parser


def _env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (DEPLOY / "pitoolbox.env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def _unit_values(path: Path, key: str) -> list[str]:
    """Alle waarden van `key=` in een systemd-unit (sleutels mogen herhaald worden)."""
    pattern = re.compile(rf"^{re.escape(key)}=(.*)$")
    return [
        m.group(1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if (m := pattern.match(line))
    ]


def _udev_props(name: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in (FIXTURES / f"udev_{name}.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            props[key] = value
    return props


# Alleen de [Match]-sleutels die onze .link-bestanden gebruiken; een andere sleutel laat de test
# falen, zodat dit model niet stilletjes achterloopt op de bestanden.
_SUPPORTED_MATCH = {"Driver", "Type", "Property"}


def _globs_match(spec: str, value: str | None) -> bool:
    """systemd.link: een lijst shell-globs; '!' vooraan keert de hele test om."""
    invert = spec.startswith("!")
    hit = value is not None and any(fnmatch.fnmatchcase(value, g) for g in spec.lstrip("!").split())
    return hit != invert


def _link_matches(link: configparser.ConfigParser, props: dict[str, str]) -> bool:
    match = link["Match"]
    assert set(match) <= _SUPPORTED_MATCH, f"onbekende Match-sleutels: {set(match)}"
    if "Driver" in match and not _globs_match(match["Driver"], props.get("ID_NET_DRIVER")):
        return False
    # Type komt uit DEVTYPE, anders uit ARPHRD (ethernet = "ether"), zie systemd.link(5).
    if "Type" in match and not _globs_match(match["Type"], props.get("DEVTYPE", "ether")):
        return False
    if "Property" in match:
        spec = match["Property"]
        invert = spec.startswith("!")
        pairs = [p.partition("=") for p in shlex.split(spec.lstrip("!"))]
        hit = all(props.get(key) == value for key, _, value in pairs)
        if hit == invert:
            return False
    return True


def _applied_name(props: dict[str, str]) -> str | None:
    """Naam uit het eerste passende .link-bestand (alfabetisch), zoals udev het doet."""
    for path in LINK_FILES:
        link = _ini(path)
        if _link_matches(link, props):
            return link["Link"]["Name"]
    return None


# --- systemd .link ------------------------------------------------------------------------------


def test_link_files_exist_and_sort_before_distro_defaults() -> None:
    assert [p.name for p in LINK_FILES] == ["10-pitoolbox-wlan0.link", "11-pitoolbox-wlan1.link"]
    for path in LINK_FILES:
        # systemd.link(5): prefix kleiner dan 70, anders kunnen standaardbestanden voorgaan.
        assert int(path.name.split("-", 1)[0]) < 70


@pytest.mark.parametrize("path", LINK_FILES, ids=lambda p: p.name)
def test_link_file_structure(path: Path) -> None:
    link = _ini(path)
    assert set(link.sections()) == {"Match", "Link"}
    assert set(link["Link"]) == {"Name"}
    name = link["Link"]["Name"]
    # Regels voor Name= uit systemd.link(5).
    assert 1 <= len(name) <= 15
    assert not set(name) & set(":/%") and not name.isdigit()
    assert name in path.name
    # Niet op MAC of kernelnaam matchen (MAC-randomisatie, kernelnamen zijn onstabiel).
    assert not {"MACAddress", "PermanentMACAddress", "OriginalName"} & set(link["Match"])


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [("wlan0", "wlan0"), ("wlan1", "wlan1"), ("eth0", None)],
)
def test_link_files_match_recorded_pi_devices(fixture: str, expected: str | None) -> None:
    assert _applied_name(_udev_props(fixture)) == expected


def test_link_names_follow_hardware_not_kernel_order() -> None:
    """Krijgt de Alfa van de kernel eerst wlan0 (bijv. eerder ingeplugd), dan blijft hij wlan1."""
    alfa = _udev_props("wlan1") | {"INTERFACE": "wlan0"}
    builtin = _udev_props("wlan0") | {"INTERFACE": "wlan1"}
    assert _applied_name(alfa) == "wlan1"
    assert _applied_name(builtin) == "wlan0"


def test_usb_brcmfmac_dongle_does_not_get_builtin_name() -> None:
    dongle = _udev_props("wlan0") | {"ID_BUS": "usb"}
    assert _applied_name(dongle) is None


def test_link_names_match_code_defaults() -> None:
    from pitoolbox.config import Settings

    settings = Settings()  # code-defaults, zonder omgevingsvariabelen
    env = _env_example()
    assert _applied_name(_udev_props("wlan0")) == settings.uplink_iface
    assert _applied_name(_udev_props("wlan0")) == env["PITOOLBOX_UPLINK_IFACE"]
    assert _applied_name(_udev_props("wlan1")) == settings.alfa_iface
    assert _applied_name(_udev_props("wlan1")) == env["PITOOLBOX_ALFA_IFACE"]


# --- NetworkManager -----------------------------------------------------------------------------


def _nm_unmanaged_specs() -> list[str]:
    keyfile = _ini(NM_CONF)["keyfile"]
    assert set(keyfile) == {"unmanaged-devices+"}  # += voegt toe i.p.v. te overschrijven
    return [s.strip() for s in re.split(r"[,;]", keyfile["unmanaged-devices+"]) if s.strip()]


def test_nm_conf_unmanages_only_the_alfa_driver() -> None:
    specs = _nm_unmanaged_specs()
    alfa_driver = _ini(DEPLOY / "network" / "11-pitoolbox-wlan1.link")["Match"]["Driver"]
    assert specs == [f"driver:{alfa_driver}"]
    # Nooit de uplink of alles: dan ben je onderweg je netwerk kwijt.
    assert not any(s == "*" or "wlan0" in s or "brcmfmac" in s for s in specs)


@pytest.mark.skipif(shutil.which("NetworkManager") is None, reason="NetworkManager niet aanwezig")
def test_nm_conf_merges_with_existing_unmanaged_devices(tmp_path: Path) -> None:
    """NetworkManager zelf leest de drop-in (alleen --print-config; raakt de daemon niet)."""
    (tmp_path / "50-ander.conf").write_text(
        "[keyfile]\nunmanaged-devices=interface-name:foo0\n", encoding="utf-8"
    )
    shutil.copy(NM_CONF, tmp_path / NM_CONF.name)
    out = subprocess.run(
        ["NetworkManager", "--print-config", f"--config-dir={tmp_path}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    line = next(x for x in out.splitlines() if x.startswith("unmanaged-devices="))
    assert "interface-name:foo0" in line
    assert "driver:mt7921u" in line


# --- logrotate en tmpfiles ----------------------------------------------------------------------


def _logrotate_stanza() -> tuple[str, list[str]]:
    lines = [
        ln.strip()
        for ln in LOGROTATE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    header, *body, closing = lines
    assert header.endswith("{") and closing == "}"
    return header.removesuffix("{").strip(), body


def test_logrotate_targets_service_audit_log() -> None:
    path, _ = _logrotate_stanza()
    assert path == _env_example()["PITOOLBOX_AUDIT_LOG"]
    # Het pad moet schrijfbaar zijn voor de service (ReadWritePaths in de unit).
    rw = _unit_values(MCP_UNIT, "ReadWritePaths")
    assert any(path.startswith(p.rstrip("/") + "/") for p in rw)


def test_logrotate_never_truncates_and_rotates_as_service_user() -> None:
    _, body = _logrotate_stanza()
    directives = {d.split()[0]: d.split()[1:] for d in body}
    # AuditLog opent per write opnieuw: hernoemen is veilig, copytruncate kan regels verliezen.
    assert "copytruncate" not in directives
    assert "copy" not in directives
    assert "nocreate" in directives
    user = _unit_values(MCP_UNIT, "User")[0]
    group = _unit_values(MCP_UNIT, "Group")[0]
    assert directives["su"] == [user, group]
    assert int(directives["rotate"][0]) >= 12 or directives["rotate"] == ["-1"]
    # dateformat moet lexicaal sorteren (jaar eerst) en uniek per rotatie zijn (%s).
    assert directives["dateformat"][0].startswith("-%Y%m%d") and "%s" in directives["dateformat"][0]


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("logrotate") is None, reason="logrotate niet aanwezig"
)
def test_logrotate_rotation_loses_no_audit_lines(tmp_path: Path) -> None:
    """Echte logrotate-run op een kopie (eigen pad, eigen gebruiker, eigen statusbestand)."""
    log_dir = tmp_path / "state"
    log_dir.mkdir(mode=0o700)
    audit_path = log_dir / "audit.jsonl"
    conf = LOGROTATE.read_text(encoding="utf-8")
    conf = conf.replace(_env_example()["PITOOLBOX_AUDIT_LOG"], str(audit_path))
    import grp  # alleen POSIX

    me, my_group = getpass.getuser(), grp.getgrgid(os.getgid()).gr_name
    conf = conf.replace("su pitoolbox pitoolbox", f"su {me} {my_group}")
    (tmp_path / "logrotate.conf").write_text(conf, encoding="utf-8")

    audit = AuditLog(audit_path)
    audit.write(tool="voor", ok=True)
    subprocess.run(
        ["logrotate", "-f", "-s", str(tmp_path / "status"), str(tmp_path / "logrotate.conf")],
        check=True,
        capture_output=True,
        timeout=30,
    )
    rotated = sorted(log_dir.glob("audit.jsonl-*"))
    assert len(rotated) == 1  # delaycompress: de eerste rotatie blijft leesbaar
    assert '"tool": "voor"' in rotated[0].read_text(encoding="utf-8")
    assert not audit_path.exists()  # nocreate: de service maakt hem zelf weer aan

    audit.write(tool="na", ok=True)  # zelfde AuditLog-object, zoals in de draaiende service
    assert '"tool": "na"' in audit_path.read_text(encoding="utf-8")
    assert '"tool": "na"' not in rotated[0].read_text(encoding="utf-8")


def test_tmpfiles_keeps_captures_and_matches_service() -> None:
    entries = [
        ln.split()
        for ln in TMPFILES.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert len(entries) == 1
    kind, path, mode, user, group, age = entries[0]
    assert kind == "d"
    assert path == _env_example()["PITOOLBOX_CAPTURES_DIR"]
    assert mode == "0700"
    assert [user] == _unit_values(MCP_UNIT, "User") and [group] == _unit_values(MCP_UNIT, "Group")
    assert age == "-"  # standaard nooit automatisch captures weggooien


# --- BlueZ --------------------------------------------------------------------------------------


def test_bluez_snippet_is_valid_le_section() -> None:
    snippet = _ini(BLUEZ_SNIPPET)
    assert snippet.sections() == ["LE"]
    le = snippet["LE"]
    assert set(le) == {"MinConnectionInterval", "MaxConnectionInterval"}
    lo, hi = int(le["MinConnectionInterval"]), int(le["MaxConnectionInterval"])
    # Bereik uit parse_le_config (BlueZ src/main.c): 0x0006..0x0C80; min <= max.
    assert 0x0006 <= lo <= hi <= 0x0C80


# --- systemd-units ------------------------------------------------------------------------------


def test_units_reference_existing_entry_points_and_paths() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    exec_start = _unit_values(MCP_UNIT, "ExecStart")[0].split()
    assert exec_start[0] == "/opt/pitoolbox/.venv/bin/pitoolbox-mcp"
    assert Path(exec_start[0]).name in scripts
    assert _unit_values(MCP_UNIT, "EnvironmentFile") == ["/etc/pitoolbox/pitoolbox.env"]
    assert _unit_values(MCP_UNIT, "WorkingDirectory") == ["/var/lib/pitoolbox"]
    # Alles waar de service naar schrijft, ligt in ReadWritePaths.
    rw = _unit_values(MCP_UNIT, "ReadWritePaths")
    env = _env_example()
    for key in ("PITOOLBOX_AUDIT_LOG", "PITOOLBOX_CAPTURES_DIR", "PITOOLBOX_AUTH_DB"):
        assert any(env[key].startswith(p.rstrip("/") + "/") for p in rw), key
    for doc in _unit_values(MCP_UNIT, "Documentation"):
        if doc.startswith("file:///opt/pitoolbox/"):
            assert (ROOT / doc.removeprefix("file:///opt/pitoolbox/")).is_file()


# --- install.sh ---------------------------------------------------------------------------------


def _install(dest: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DESTDIR": str(dest), "LC_ALL": "C"}
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )


def _main_conf(dest: Path, text: str) -> Path:
    path = dest / "etc" / "bluetooth" / "main.conf"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


@linux_only
def test_install_sh_syntax() -> None:
    subprocess.run(["bash", "-n", str(INSTALL_SH)], check=True, timeout=30)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck niet geïnstalleerd")
def test_install_sh_shellcheck() -> None:
    subprocess.run(["shellcheck", str(INSTALL_SH)], check=True, timeout=60)


def test_install_sh_references_existing_repo_files() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    refs = set(re.findall(r"\$\{SRC_DIR\}/([\w./-]+)", text))
    refs |= {f"deploy/network/{n}" for n in re.findall(r"\b\d\d-pitoolbox-wlan[01]\.link\b", text)}
    assert "deploy/systemd/pitoolbox-mcp.service" in refs
    for ref in refs:
        if ref.endswith("/"):
            continue
        path = ROOT / ref
        assert path.exists(), ref
    for name in ("10-pitoolbox-wlan0.link", "11-pitoolbox-wlan1.link"):
        assert (DEPLOY / "network" / name).is_file()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in text


@linux_only
def test_install_sh_rejects_unknown_option(tmp_path: Path) -> None:
    result = _install(tmp_path, "--onbekend")
    assert result.returncode != 0
    assert "onbekende optie" in result.stderr


@linux_only
def test_install_sh_system_only_needs_a_phase5_step(tmp_path: Path) -> None:
    result = _install(tmp_path, "--alleen-systeem", "--ja")
    assert result.returncode != 0
    assert not any(tmp_path.iterdir())


@linux_only
def test_install_sh_asks_and_refuses_without_terminal(tmp_path: Path) -> None:
    result = _install(tmp_path, "--onderweg")  # geen --ja en stdin is geen terminal
    assert result.returncode != 0
    assert "--ja" in result.stderr
    assert not (tmp_path / "etc").exists()


@linux_only
def test_install_sh_dry_run_writes_nothing(tmp_path: Path) -> None:
    main_conf = _main_conf(tmp_path, "[General]\n\n[LE]\n#MinConnectionInterval=\n")
    before = main_conf.read_text(encoding="utf-8")
    result = _install(tmp_path, "--dry-run", "--onderweg", "--bluez-le")
    assert result.returncode == 0, result.stderr
    assert "zou aanmaken" in result.stdout and "+MinConnectionInterval=6" in result.stdout
    assert sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    ) == ["etc/bluetooth/main.conf"]
    assert main_conf.read_text(encoding="utf-8") == before


@linux_only
def test_install_sh_onderweg_installs_idempotently_with_backups(tmp_path: Path) -> None:
    expected = {
        "etc/systemd/network/10-pitoolbox-wlan0.link": DEPLOY / "network/10-pitoolbox-wlan0.link",
        "etc/systemd/network/11-pitoolbox-wlan1.link": DEPLOY / "network/11-pitoolbox-wlan1.link",
        "etc/NetworkManager/conf.d/90-pitoolbox-unmanaged.conf": NM_CONF,
        "etc/logrotate.d/pitoolbox": LOGROTATE,
        "etc/tmpfiles.d/pitoolbox.conf": TMPFILES,
    }
    # Een afwijkende, al bestaande versie moet eerst een backup krijgen.
    old_nm = tmp_path / "etc/NetworkManager/conf.d/90-pitoolbox-unmanaged.conf"
    old_nm.parent.mkdir(parents=True)
    old_nm.write_text("[keyfile]\nunmanaged-devices=interface-name:wlan1\n", encoding="utf-8")

    first = _install(tmp_path, "--onderweg", "--ja")
    assert first.returncode == 0, first.stderr
    for rel, src in expected.items():
        installed = tmp_path / rel
        assert installed.read_bytes() == src.read_bytes(), rel
        assert installed.stat().st_mode & 0o777 == 0o644, rel
    backups = list((tmp_path / "var/backups/pitoolbox").rglob("90-pitoolbox-unmanaged.conf"))
    assert len(backups) == 1
    assert "interface-name:wlan1" in backups[0].read_text(encoding="utf-8")
    # Backups staan buiten /etc, zodat NM/logrotate ze niet zelf inlezen.
    assert not list((tmp_path / "etc").rglob("*bak*"))

    second = _install(tmp_path, "--onderweg", "--ja")
    assert second.returncode == 0, second.stderr
    assert "geplaatst" not in second.stdout and "backup" not in second.stdout
    assert second.stdout.count("ongewijzigd") == len(expected)


@linux_only
def test_install_sh_bluez_inserts_under_le_once(tmp_path: Path) -> None:
    main_conf = _main_conf(
        tmp_path,
        "[General]\n#Name = BlueZ\n\n[LE]\n#MinConnectionInterval=\n#MaxConnectionInterval=\n"
        "\n[GATT]\n#Cache = always\n",
    )
    original = main_conf.read_text(encoding="utf-8")
    assert _install(tmp_path, "--bluez-le", "--ja").returncode == 0

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string(main_conf.read_text(encoding="utf-8"))
    snippet = _ini(BLUEZ_SNIPPET)["LE"]
    assert dict(parser["LE"]) == dict(snippet)
    assert set(parser.sections()) == {"General", "LE", "GATT"}
    backups = list((tmp_path / "var/backups/pitoolbox").rglob("main.conf"))
    assert [b.read_text(encoding="utf-8") for b in backups] == [original]

    after_first = main_conf.read_text(encoding="utf-8")
    again = _install(tmp_path, "--bluez-le", "--ja")
    assert again.returncode == 0
    assert main_conf.read_text(encoding="utf-8") == after_first
    assert len(list((tmp_path / "var/backups/pitoolbox").rglob("main.conf"))) == 1


@linux_only
def test_install_sh_bluez_appends_le_section_when_missing(tmp_path: Path) -> None:
    main_conf = _main_conf(tmp_path, "[General]\nName = pi\n")
    assert _install(tmp_path, "--bluez-le", "--ja").returncode == 0
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string(main_conf.read_text(encoding="utf-8"))
    assert (
        parser["LE"]["MinConnectionInterval"] == _ini(BLUEZ_SNIPPET)["LE"]["MinConnectionInterval"]
    )
    assert parser["General"]["Name"] == "pi"


@linux_only
def test_install_sh_bluez_keeps_users_own_values(tmp_path: Path) -> None:
    text = "[LE]\nMinConnectionInterval = 24\n"
    main_conf = _main_conf(tmp_path, text)
    result = _install(tmp_path, "--bluez-le", "--ja")
    assert result.returncode == 0
    assert "NIET aangepast" in result.stderr
    assert main_conf.read_text(encoding="utf-8") == text
    assert not (tmp_path / "var").exists()
