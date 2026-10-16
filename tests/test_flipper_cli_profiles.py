"""CLI-profiel "momentum": sjablonen, parsers, automatische keuze en gedrag tegen de fake.

Momentum mntm-012 (e1784e74) heeft dezelfde CLI als official; alleen het geldige Sub-GHz-bereik
verschilt (zie de moduledocstring van _flipper_cli). De `*_momentum_*`-fixtures volgen de
decoderformaten uit de Momentum-bron; de "none"-fixtures van official komen byte-voor-byte
overeen met opnames van een echte Flipper met mntm-012.
"""

from __future__ import annotations

import pytest
from flipper_fakes import FAST_TIMING, FakeFlipperTransport, fixture_json, fixture_text

from pitoolbox.core.devices import _flipper_cli as cli
from pitoolbox.core.devices.flipper import FlipperDevice
from pitoolbox.core.errors import DeviceError, UnsupportedFirmware

MOMENTUM = cli.PROFILES["momentum"]


def crlf(name: str) -> str:
    return fixture_text(name).replace("\n", "\r\n").replace("---ETX---\r\n", "")


def momentum_fake(subghz_script: str = "subghz_rx_momentum_silent.txt") -> FakeFlipperTransport:
    return FakeFlipperTransport(
        device_info=fixture_json("device_info_momentum.json"),
        subghz_ranges=cli.MOMENTUM_SUBGHZ_RANGES_HZ,
        subghz_script=subghz_script,
    )


def make_device(fake: FakeFlipperTransport, profile: str = "auto") -> FlipperDevice:
    return FlipperDevice(lambda: fake, profile, timing=FAST_TIMING)


# --- profiel en keuze --------------------------------------------------------------------


def test_momentum_differs_from_official_only_in_subghz_range() -> None:
    official = cli.PROFILES["official"]
    assert MOMENTUM.name == "momentum"
    assert MOMENTUM.subghz_ranges == cli.MOMENTUM_SUBGHZ_RANGES_HZ
    for attr in (
        "main_prompt",
        "nfc_prompt",
        "subghz_device",
        "rfid_read_command",
        "nfc_enter_command",
        "nfc_scan_command",
        "nfc_exit_command",
        "nfc_busy_marker",
    ):
        assert getattr(MOMENTUM, attr) == getattr(official, attr), attr
    # Gedeelde parsers, geen kopieën.
    assert MOMENTUM.parse_subghz_rx is cli.parse_subghz_rx
    assert MOMENTUM.parse_rfid_read is cli.parse_rfid_read
    assert MOMENTUM.parse_nfc_scanner is cli.parse_nfc_scanner


@pytest.mark.parametrize("fork", ["Momentum", "momentum", " MOMENTUM "])
def test_auto_selects_momentum_from_fork(fork: str) -> None:
    info = fixture_json("device_info_momentum.json") | {"firmware_origin_fork": fork}
    assert cli.select_profile("auto", info) is MOMENTUM
    assert cli.select_profile("", info) is MOMENTUM


def test_forced_momentum_and_unknown_forks() -> None:
    assert cli.select_profile("Momentum", None) is MOMENTUM
    assert cli.select_profile("momentum", {"firmware_origin_fork": "Official"}) is MOMENTUM
    # Een andere fork zonder profiel blijft geweigerd (geen gok op "lijkt op Momentum").
    for fork in ("Unleashed", "RogueMaster", "Xtreme"):
        with pytest.raises(UnsupportedFirmware, match="momentum"):
            cli.select_profile("auto", {"firmware_origin_fork": fork})


# --- sjabloon ----------------------------------------------------------------------------


def test_momentum_subghz_command_template() -> None:
    assert MOMENTUM.subghz_rx_command(433_920_000) == "subghz rx 433920000 0"
    for bad in (True, "433920000 1", 433.92e6, None):
        with pytest.raises(DeviceError):
            MOMENTUM.subghz_rx_command(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "freq",
    [281_000_000, 290_000_000, 361_000_000, 378_000_000, 481_000_000, 749_000_000, 962_000_000],
)
def test_momentum_accepts_extended_rx_range(freq: int) -> None:
    assert MOMENTUM.subghz_rx_command(freq) == f"subghz rx {freq} 0"


@pytest.mark.parametrize(
    "freq",
    [0, 280_999_999, 361_000_001, 377_999_999, 481_000_001, 748_999_999, 962_000_001],
)
def test_momentum_rejects_outside_hal_range(freq: int) -> None:
    with pytest.raises(DeviceError, match="buiten"):
        MOMENTUM.subghz_rx_command(freq)


def test_official_still_rejects_momentum_only_frequency() -> None:
    with pytest.raises(DeviceError, match="buiten"):
        cli.PROFILES["official"].subghz_rx_command(290_000_000)


# --- parsers op Momentum-uitvoer ---------------------------------------------------------


def test_parse_momentum_silent_capture() -> None:
    # Kop en slot zoals op een echte mntm-012 opgenomen (inclusief Load_keystore-regels).
    parsed = cli.parse_subghz_rx(crlf("subghz_rx_momentum_silent.txt"))
    assert parsed.tuned_frequency_hz == 433_919_830
    assert parsed.packets == []
    assert parsed.packet_count == 0


def test_parse_momentum_session_with_db_header() -> None:
    parsed = cli.parse_subghz_rx(crlf("subghz_rx_momentum_session.txt"))
    assert parsed.packet_count == 2
    assert [p.split("\n")[0] for p in parsed.packets] == ["Princeton 24bit", "CAME Atomo 62b"]
    assert parsed.packets[0].endswith("Te:390us  GT:Te*31")
    assert parsed.packets[1].endswith("Btn_Cnt:0x05")  # reset-punten erachter weg
    assert not any("Load_keystore" in p for p in parsed.packets)


def test_parse_momentum_weather_station_name_on_own_line() -> None:
    parsed = cli.parse_subghz_rx(crlf("subghz_rx_momentum_weather.txt"))
    assert parsed.packet_count == 2
    assert len(parsed.packets) == 2
    assert parsed.packets[0].split("\n")[-1] == "Te:390us  GT:Te*31"
    assert parsed.packets[1].split("\n")[:2] == ["Nexus-TH", "36bit   Ch: 1"]
    assert parsed.packets[1].endswith("Hum: 45%")


def test_parse_glued_packets_keeps_firmware_count() -> None:
    # came_atomo.c eindigt zonder "\r\n": de volgende kop plakt eraan. Niet te splitsen,
    # maar packet_count komt uit "Packets received" en blijft dus kloppen.
    parsed = cli.parse_subghz_rx(crlf("subghz_rx_momentum_glued.txt"))
    assert parsed.packet_count == 2
    assert len(parsed.packets) == 1
    assert "Btn_Cnt:0x05..Princeton 24bit" in parsed.packets[0].split("\n")


def test_hex_data_line_is_not_taken_for_a_header() -> None:
    body = (
        "Listening at frequency: 433919830 device: 0. Press CTRL+C to stop\r\n"
        "Somfy Keytis 80b\r\n1A2B3C4D5E6F7A8B9012B\r\nSn:0x0ABCDE \r\n"
        "\r\nPackets received 1\r\n"
    )
    parsed = cli.parse_subghz_rx(body)
    assert len(parsed.packets) == 1
    assert parsed.packets[0].split("\n")[1] == "1A2B3C4D5E6F7A8B9012B"


def test_parse_momentum_frequency_error_uses_official_range_text() -> None:
    # subghz_cli.c (mntm-012) print SUBGHZ_FREQUENCY_RANGE_STR, ook al is het HAL-bereik ruimer.
    err = (
        "Frequency must be in 299999755...348000000 or 386999938...464000000 or "
        "778999847...928000000 range, not 200000000\r\n"
    )
    with pytest.raises(DeviceError, match="Frequency must be in"):
        MOMENTUM.parse_subghz_rx(err)


def test_momentum_rfid_and_nfc_none_captures() -> None:
    # Opgenomen op mntm-012 zonder tag; gelijk aan de official-fixtures.
    rfid = MOMENTUM.parse_rfid_read(
        "Reading RFID...\r\nPress Ctrl+C to abort\r\nReading stopped\r\n"
    )
    assert rfid.found is False
    nfc = MOMENTUM.parse_nfc_scanner("Press Ctrl+C to abort\r\n\nProtocols detected: \r\n")
    assert nfc.found is False
    assert crlf("rfid_read_none.txt") == (
        "Reading RFID...\r\nPress Ctrl+C to abort\r\nReading stopped\r\n"
    )


@pytest.mark.parametrize("protocol", ["EM4100/32", "PAC/Stanley", "Radio Key"])
def test_momentum_rfid_protocol_names(protocol: str) -> None:
    # Namen uit lib/lfrfid/protocols (mntm-012), met '/' en spaties.
    text = f"Press Ctrl+C to abort\r\n{protocol} 0A0B0C0D\r\nReading stopped\r\n"
    parsed = MOMENTUM.parse_rfid_read(text)
    assert (parsed.protocol, parsed.data_hex) == (protocol, "0A0B0C0D")


# --- FlipperDevice tegen een nagebootste Momentum-Flipper --------------------------------


def test_device_auto_selects_momentum_and_runs_subghz() -> None:
    fake = momentum_fake(subghz_script="subghz_rx_momentum_session.txt")
    device = make_device(fake)
    result = device.subghz_rx(290_000_000, 0.1)  # alleen geldig in Momentum
    assert device.status()["profile"] == "momentum"
    assert "system_device_info_request" in fake.rpc_log
    assert "subghz rx 290000000 0" in fake.cli_log
    assert result["packet_count"] == 2
    assert result["tuned_frequency_hz"] == 433_919_830  # uit de fixture
    assert fake.idle_at_prompt


def test_device_momentum_rfid_and_nfc_roundtrip() -> None:
    fake = momentum_fake()
    device = make_device(fake)
    assert device.rfid_read(0.1)["protocol"] == "EM4100"
    nfc = device.nfc_scan(0.1)
    assert nfc["found"] is True
    assert fake.cli_log[-3:] == ["nfc", "scanner -t", "exit"]
    assert device.status()["profile"] == "momentum"
    assert fake.shell == "main"


def test_device_with_official_profile_refuses_momentum_only_frequency() -> None:
    fake = momentum_fake()
    device = make_device(fake, profile="official")
    with pytest.raises(DeviceError, match="buiten"):
        device.subghz_rx(290_000_000, 0.1)
    assert not any(line.startswith("subghz") for line in fake.cli_log)
