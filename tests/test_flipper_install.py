"""FAP installeren via de storage-RPC (`FlipperDevice.install_app_file`) en de FAP-controle.

Echte `FlipperDevice` en `RpcClient` op de `FakeFlipperTransport`, die storage_write/md5sum/
mkdir/rename nabootst zoals Momentum mntm-012 `rpc_storage.c` (zie flipper_fakes.py).
"""

from __future__ import annotations

import hashlib

import pytest
from flipper_fakes import FAST_TIMING, FakeFap, FakeFlipperTransport, make_fap

from pitoolbox.core.devices._fap import FapError, check_compatible, inspect_fap
from pitoolbox.core.devices.flipper import STORAGE_CHUNK, AppLocked, FlipperDevice
from pitoolbox.core.errors import DeviceError

APP = "/ext/apps/Tools/pitoolbox_ui.fap"
PART = APP + ".part"


def _device(fake: FakeFlipperTransport) -> FlipperDevice:
    return FlipperDevice(lambda: fake, timing=FAST_TIMING)


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


@pytest.fixture
def fake() -> FakeFlipperTransport:
    return FakeFlipperTransport(start_in_rpc=True, kind="ble")


# --- FAP-controle ---------------------------------------------------------------------------


def test_inspect_fap_reads_manifest() -> None:
    info = inspect_fap(make_fap(name="PiToolbox", api=(87, 1), target=7))
    assert (info.name, info.api, info.target, info.version) == ("PiToolbox", "87.1", 7, "1.0")


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"", "bytes"),
        (b"MZ" + b"\0" * 100, "geen ELF"),
        (make_fap(machine=62), "niet ARM"),
        (make_fap(with_meta=False), ".fapmeta"),
        (make_fap(body=300 * 1024), "bytes"),
        (make_fap()[:60], "buiten het bestand"),
    ],
)
def test_inspect_fap_rejects_bad_files(data: bytes, fragment: str) -> None:
    with pytest.raises(FapError, match=fragment):
        inspect_fap(data)


def test_compatibility_with_firmware_api_and_target() -> None:
    info = inspect_fap(make_fap(api=(87, 1), target=7))
    ok = {"firmware_api_major": "87", "firmware_api_minor": "1", "firmware_target": "7"}
    assert check_compatible(info, ok) == []
    assert "App Too Old/New" in check_compatible(info, {**ok, "firmware_api_major": "86"})[0]
    assert "nieuwer" in check_compatible(info, {**ok, "firmware_api_minor": "0"})[0]
    assert "target" in check_compatible(info, {**ok, "firmware_target": "18"})[0]
    assert check_compatible(info, {}) == []  # niets te controleren


# --- installeren ----------------------------------------------------------------------------


def test_install_writes_in_512_byte_chunks_verifies_and_renames(
    fake: FakeFlipperTransport,
) -> None:
    data = make_fap(body=5000)
    seen: list[tuple[int, int]] = []
    device = _device(fake)
    result = device.install_app_file(APP, data, progress=lambda d, t: seen.append((d, t)))
    assert fake.files[APP] == data
    assert PART not in fake.files
    assert {"/ext/apps", "/ext/apps/Tools"} <= fake.dirs
    assert max(fake.write_chunks) == STORAGE_CHUNK == 512
    assert sum(fake.write_chunks) == len(data)
    assert seen[-1] == (len(data), len(data)) and len(seen) == len(fake.write_chunks)
    assert result.md5 == _md5(data) and not result.skipped and result.size == len(data)
    kinds = fake.rpc_log
    assert kinds.index("storage_rename_request") > kinds.index("storage_write_request")
    assert kinds.count("storage_md5sum_request") == 3  # bestaand, .part, eindresultaat
    assert "app_lock_status_request" in kinds
    device.close()


def test_install_skips_identical_file_and_force_rewrites(fake: FakeFlipperTransport) -> None:
    data = make_fap()
    fake.files[APP] = data
    device = _device(fake)
    assert device.install_app_file(APP, data).skipped
    assert fake.write_chunks == []
    result = device.install_app_file(APP, data, skip_if_same=False)
    assert not result.skipped and fake.write_chunks
    device.close()


def test_install_replaces_older_version(fake: FakeFlipperTransport) -> None:
    fake.files[APP] = make_fap(body=100)
    new = make_fap(body=2000)
    _device(fake).install_app_file(APP, new)
    assert fake.files[APP] == new


def test_install_refused_while_an_app_is_open(fake: FakeFlipperTransport) -> None:
    fake.user_app_open = True
    with pytest.raises(AppLocked):
        _device(fake).install_app_file(APP, make_fap())
    assert fake.write_chunks == []


def test_install_refused_while_our_app_runs(fake: FakeFlipperTransport) -> None:
    fake.attach_fap(FakeFap())
    fake.files[APP] = make_fap(body=10)
    device = _device(fake)
    device.start_rpc_app(APP)
    with pytest.raises(AppLocked):
        device.install_app_file(APP, make_fap())
    assert fake.write_chunks == []
    device.close()


@pytest.mark.parametrize(
    "path",
    [
        "/ext/apps/Tools/../x.fap",
        "/int/apps/x.fap",
        "/ext/apps/x.fap",
        "/ext/apps/Tools/x.txt",
        "/ext/infrared/tv.ir",
    ],
)
def test_install_only_to_app_paths(fake: FakeFlipperTransport, path: str) -> None:
    with pytest.raises(ValueError, match="app-pad"):
        _device(fake).install_app_file(path, make_fap())
    assert fake.rpc_log == []


def test_install_only_the_pitoolbox_fap(fake: FakeFlipperTransport) -> None:
    with pytest.raises(ValueError, match="niet de PiToolbox-app"):
        _device(fake).install_app_file(APP, make_fap(name="Snake"))
    with pytest.raises(FapError):
        _device(fake).install_app_file(APP, b"\x7fELF" + b"\0" * 100)
    assert fake.rpc_log == []


def test_write_error_stops_sending_and_keeps_old_app(fake: FakeFlipperTransport) -> None:
    old = make_fap(body=10)
    fake.files[APP] = old
    data = make_fap(body=5000)
    fake.fail_write_at = 3
    with pytest.raises(DeviceError, match="ERROR_STORAGE_INTERNAL"):
        _device(fake).install_app_file(APP, data)
    # Na de fout geen blokken meer: een volgend blok zou een nieuw (leeg) bestand beginnen.
    assert len(fake.write_chunks) == 3
    assert fake.files[APP] == old
    assert PART not in fake.files  # opgeruimd


def test_md5_mismatch_keeps_old_app(fake: FakeFlipperTransport) -> None:
    old = make_fap(body=10)
    fake.files[APP] = old
    fake.corrupt_writes = True
    with pytest.raises(DeviceError, match="MD5"):
        _device(fake).install_app_file(APP, make_fap(body=2000))
    assert fake.files[APP] == old
    assert PART not in fake.files
