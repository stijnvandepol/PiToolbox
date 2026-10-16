"""Parsers voor iw / airodump-ng uitvoer met realistische fixtures."""

from __future__ import annotations

import pytest
from wifi_fakes import fixture

from pitoolbox.core.devices.wifi import (
    freq_to_channel,
    parse_airodump_csv,
    parse_iw_dev_info,
    parse_iw_scan,
)


@pytest.mark.parametrize(
    ("mhz", "channel"),
    [
        (2412, 1),
        (2437, 6),
        (2472, 13),
        (2484, 14),
        (5180, 36),
        (5745, 149),
        (5885, 177),
        (5935, 2),
        (5955, 1),
        (6415, 93),
        (1234, None),
        (2413, None),
    ],
)
def test_freq_to_channel(mhz: int, channel: int | None) -> None:
    assert freq_to_channel(mhz) == channel


def test_iw_dev_info_managed_and_monitor() -> None:
    m = parse_iw_dev_info(fixture("iw_dev_info_managed.txt"))
    assert m["iface"] == "wlan1"
    assert m["mode"] == "managed"
    assert m["channel"] == 6
    assert m["freq_mhz"] == 2437
    assert m["addr"] == "00:C0:CA:AA:BB:CC"
    mon = parse_iw_dev_info(fixture("iw_dev_info_monitor.txt"))
    assert mon["mode"] == "monitor"
    assert mon["channel"] == 36


def test_iw_dev_info_without_channel() -> None:
    info = parse_iw_dev_info("Interface wlan1\n\tifindex 4\n\ttype managed\n\twiphy 1\n")
    assert info["mode"] == "managed"
    assert info["channel"] is None


def test_iw_dev_info_garbage() -> None:
    assert parse_iw_dev_info("")["mode"] is None


@pytest.fixture
def scan() -> dict[str, dict[str, object]]:
    return {n["bssid"]: n for n in parse_iw_scan(fixture("iw_scan.txt"))}


def test_iw_scan_count_and_bssid_normalized(scan: dict[str, dict[str, object]]) -> None:
    assert len(scan) == 8
    assert "AA:BB:CC:11:22:33" in scan  # kleine letters in de bron -> hoofdletters
    assert "DE:AD:BE:EF:00:01" in scan


def test_iw_scan_wpa2_24ghz(scan: dict[str, dict[str, object]]) -> None:
    n = scan["AA:BB:CC:11:22:33"]
    assert n["ssid"] == "HomeNet"
    assert n["hidden"] is False
    assert n["freq_mhz"] == 2437
    assert n["channel"] == 6
    assert n["signal_dbm"] == -45.0
    assert n["security"] == "wpa2"
    assert n["associated"] is False


def test_iw_scan_hidden_ssid_and_float_freq(scan: dict[str, dict[str, object]]) -> None:
    n = scan["DE:AD:BE:EF:00:01"]
    assert n["hidden"] is True
    assert n["ssid"] == ""
    assert n["freq_mhz"] == 2412  # bron had "2412.0"
    assert isinstance(n["freq_mhz"], int)
    assert n["channel"] == 1
    # lege SSID-regel ('SSID: ') telt ook als verborgen
    assert scan["10:20:30:40:50:64"]["hidden"] is True


def test_iw_scan_wpa3_5ghz_primary_channel(scan: dict[str, dict[str, object]]) -> None:
    n = scan["AA:BB:CC:44:55:66"]
    assert n["security"] == "wpa3"
    assert n["freq_mhz"] == 5180
    assert n["channel"] == 36  # uit 'HT operation: primary channel', geen DS Parameter set
    assert n["signal_dbm"] == -58.0


def test_iw_scan_open_wep_transition_mixed(scan: dict[str, dict[str, object]]) -> None:
    open_net = scan["10:20:30:40:50:60"]
    assert open_net["security"] == "open"
    assert open_net["ssid"] == "CoffeeShop Free"
    assert open_net["freq_mhz"] == 2462
    assert scan["10:20:30:40:50:61"]["security"] == "wep"
    assert scan["10:20:30:40:50:62"]["security"] == "wpa2/wpa3"
    mixed = scan["10:20:30:40:50:63"]
    assert mixed["security"] == "wpa/wpa2"
    assert mixed["ssid"] == "Legacy Mix"  # \x20 uitgeschreven
    assert mixed["associated"] is True


def test_iw_scan_channel_from_freq_when_no_ie() -> None:
    text = "BSS 00:11:22:33:44:55(on wlan1)\n\tfreq: 2447\n\tsignal: -50.00 dBm\n\tSSID: X\n"
    (n,) = parse_iw_scan(text)
    assert n["channel"] == 8


def test_iw_scan_empty() -> None:
    assert parse_iw_scan("") == []


def test_airodump_csv_sections() -> None:
    parsed = parse_airodump_csv(fixture("airodump-01.csv"))
    aps = {a["bssid"]: a for a in parsed["aps"]}
    assert len(aps) == 5
    home = aps["AA:BB:CC:11:22:33"]
    assert home["ssid"] == "HomeNet"
    assert home["channel"] == 6
    assert home["privacy"] == "WPA2"
    assert home["security"] == "wpa2"
    assert home["signal_dbm"] == -45
    assert home["beacons"] == 25
    assert aps["AA:BB:CC:44:55:66"]["security"] == "wpa3/wpa2"
    assert aps["10:20:30:40:50:60"]["security"] == "open"
    assert aps["10:20:30:40:50:60"]["ssid"] == "CoffeeShop Free"
    hidden = aps["DE:AD:BE:EF:00:01"]
    assert hidden["hidden"] is True
    assert hidden["ssid"] == ""
    # ESSID met komas wordt via ID-length correct uitgeknipt; Power -1 = onbekend
    comma = aps["10:20:30:40:50:70"]
    assert comma["ssid"] == "Foo, Bar, X"
    assert comma["signal_dbm"] is None


def test_airodump_csv_stations() -> None:
    parsed = parse_airodump_csv(fixture("airodump-01.csv"))
    sts = {s["mac"]: s for s in parsed["stations"]}
    assert len(sts) == 2
    assoc = sts["11:22:33:44:55:66"]
    assert assoc["bssid"] == "AA:BB:CC:11:22:33"
    assert assoc["packets"] == 10
    assert assoc["signal_dbm"] == -50
    assert assoc["probes"] == ["HomeNet"]
    free = sts["66:55:44:33:22:11"]
    assert free["bssid"] is None
    assert free["probes"] == ["Hotspot", "Phone wifi"]


def test_airodump_csv_empty_and_partial() -> None:
    assert parse_airodump_csv("") == {"aps": [], "stations": []}
    only_header = "\r\nBSSID, First time seen, Last time seen, channel\r\n"
    assert parse_airodump_csv(only_header) == {"aps": [], "stations": []}
