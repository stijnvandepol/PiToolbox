"""Tests voor core/devices/_ble_pairing.py met een volledig nagebootste D-Bus-laag.

Er wordt nergens dbus-fast geimporteerd (alleen de laatste test, die zichzelf overslaat als het
pakket ontbreekt, zoals op Windows).
"""

from __future__ import annotations

import asyncio
import copy
import threading
import types
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from pitoolbox.core.devices import _ble_pairing as bp

ADAPTER = "/org/bluez/hci0"
ADDR = "AA:BB:CC:DD:EE:FF"
DEV = f"{ADAPTER}/dev_AA_BB_CC_DD_EE_FF"


def make_objects(
    *,
    device: bool = True,
    paired: bool = False,
    trusted: bool = False,
    powered: bool = True,
    adapter: bool = True,
    name: str = "Flipper Abc",
) -> bp.ManagedObjects:
    objects: bp.ManagedObjects = {"/": {}, "/org/bluez": {}}
    if adapter:
        objects[ADAPTER] = {bp.ADAPTER_IFACE: {"Address": "00:11:22:33:44:55", "Powered": powered}}
    if device:
        objects[DEV] = {
            bp.DEVICE_IFACE: {
                "Address": ADDR,
                "Alias": name,
                "Name": name,
                "Adapter": ADAPTER,
                "Paired": paired,
                "Bonded": paired,
                "Trusted": trusted,
                "Connected": False,
            }
        }
    return objects


class FakeBus:
    """Neemt calls op en laat tests fouten/gedrag per methode instellen."""

    def __init__(
        self,
        objects: bp.ManagedObjects,
        *,
        errors: dict[str, bp.BluezDBusError] | None = None,
        on_pair: Callable[[FakeBus], Awaitable[None]] | None = None,
        appear_after_polls: int | None = None,
    ) -> None:
        self.objects = objects
        self.errors = errors or {}
        self.on_pair = on_pair
        self.appear_after_polls = appear_after_polls
        self.polls = 0
        self.calls: list[tuple[str, str, str, list[Any]]] = []
        self.properties: list[tuple[str, str, str, Any]] = []
        self.exported: dict[str, bp.PairingAgent] = {}
        self.unexported: list[str] = []
        self.closed = False
        self._hidden = copy.deepcopy(objects.get(DEV)) if appear_after_polls is not None else None
        if appear_after_polls is not None:
            self.objects.pop(DEV, None)

    @property
    def agent(self) -> bp.PairingAgent:
        return self.exported[bp.AGENT_PATH]

    def members(self) -> list[str]:
        return [member for _path, _iface, member, _body in self.calls]

    async def call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Sequence[Any] = (),
    ) -> list[Any]:
        self.calls.append((path, interface, member, list(body)))
        if member in self.errors:
            raise self.errors[member]
        if member == "Pair":
            if self.on_pair is not None:
                await self.on_pair(self)
            props = self.objects[path][bp.DEVICE_IFACE]
            props["Paired"] = True
            props["Bonded"] = True
        return []

    async def get_managed_objects(self) -> bp.ManagedObjects:
        self.polls += 1
        if (
            self.appear_after_polls is not None
            and self.polls > self.appear_after_polls
            and self._hidden is not None
        ):
            self.objects[DEV] = self._hidden
        return copy.deepcopy(self.objects)

    async def set_property(
        self, path: str, interface: str, name: str, signature: str, value: Any
    ) -> None:
        self.properties.append((path, interface, name, value))
        if "Set" in self.errors:
            raise self.errors["Set"]
        self.objects[path][interface][name] = value

    async def get_property(self, path: str, interface: str, name: str) -> Any:
        self.calls.append((path, bp.PROPERTIES_IFACE, "Get", [interface, name]))
        if "Get" in self.errors:
            raise self.errors["Get"]
        return self.objects[path][interface][name]

    def export_agent(self, path: str, agent: bp.PairingAgent) -> None:
        self.exported[path] = agent

    def unexport_agent(self, path: str) -> None:
        self.unexported.append(path)

    async def close(self) -> None:
        self.closed = True


def factory(bus: FakeBus) -> bp.BusFactory:
    async def make() -> bp.BluezBus:
        return bus

    return make


async def pair(bus: FakeBus, provider: Callable[[], int] = lambda: 123456, **kw: Any) -> Any:
    kw.setdefault("poll_s", 0)
    kw.setdefault("discovery_timeout", 0.2)
    timeout = kw.pop("timeout", 5.0)
    return await bp.pair_async(ADDR, provider, timeout, bus_factory=factory(bus), **kw)


# --- koppelen: succes ---------------------------------------------------------------------


async def test_pair_success_flow() -> None:
    received: list[int] = []

    async def on_pair(bus: FakeBus) -> None:
        # BlueZ vraagt tijdens Pair() om de code van het Flipper-scherm.
        received.append(await bus.agent.request_passkey(DEV))

    bus = FakeBus(make_objects(), on_pair=on_pair)
    result = await pair(bus, provider=lambda: 42731)

    assert received == [42731]
    assert result.address == ADDR
    assert result.name == "Flipper Abc"
    assert result.paired and result.bonded and result.trusted
    assert not result.already_paired
    # Volgorde: agent registreren (KeyboardOnly) > default agent > Pair > afmelden.
    register = next(c for c in bus.calls if c[2] == "RegisterAgent")
    assert register[0] == bp.ROOT_PATH
    assert register[3] == [bp.AGENT_PATH, "KeyboardOnly"]
    members = bus.members()
    assert members.index("RegisterAgent") < members.index("RequestDefaultAgent")
    assert members.index("RequestDefaultAgent") < members.index("Pair")
    assert members.index("Pair") < members.index("UnregisterAgent")
    assert bus.properties == [(DEV, bp.DEVICE_IFACE, "Trusted", True)]
    assert bus.unexported == [bp.AGENT_PATH]
    assert bus.closed


async def test_pair_disconnects_after_success_to_free_the_single_ble_slot() -> None:
    bus = FakeBus(make_objects())
    bus.objects[DEV][bp.DEVICE_IFACE]["Connected"] = True
    await pair(bus)
    assert "Disconnect" in bus.members()


async def test_request_default_agent_refusal_is_not_fatal() -> None:
    bus = FakeBus(
        make_objects(),
        errors={"RequestDefaultAgent": bp.BluezDBusError("org.bluez.Error.Failed", "nee")},
    )
    result = await pair(bus)
    assert result.paired


async def test_trusted_failure_is_reported_not_raised() -> None:
    bus = FakeBus(make_objects(), errors={"Set": bp.BluezDBusError("org.bluez.Error.Failed")})
    result = await pair(bus)
    assert result.paired and not result.trusted


async def test_already_paired_skips_pairing_but_trusts() -> None:
    bus = FakeBus(make_objects(paired=True, trusted=False))
    result = await pair(bus)
    assert result.already_paired and result.trusted
    assert "Pair" not in bus.members()
    assert "RegisterAgent" not in bus.members()
    assert bus.closed


# --- koppelen: fouten --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        ("org.bluez.Error.AuthenticationFailed", "authenticatie mislukt"),
        ("org.bluez.Error.AuthenticationCanceled", "afgebroken"),
        ("org.bluez.Error.AuthenticationRejected", "geweigerd"),
        ("org.bluez.Error.AuthenticationTimeout", "time-out"),
        ("org.bluez.Error.ConnectionAttemptFailed", "Geen verbinding"),
        ("org.bluez.Error.AlreadyExists", "Al gekoppeld"),
        ("org.bluez.Error.InProgress", "loopt al"),
        ("org.bluez.Error.Failed", "algemene fout"),
        ("org.bluez.Error.SomethingNew", "org.bluez.Error.SomethingNew"),
    ],
)
async def test_pair_error_mapping_and_cleanup(error: str, fragment: str) -> None:
    bus = FakeBus(make_objects(), errors={"Pair": bp.BluezDBusError(error, "details")})
    with pytest.raises(bp.BlePairingError) as exc_info:
        await pair(bus)
    assert fragment in str(exc_info.value)
    assert exc_info.value.dbus_error == error
    # De agent is ook bij een fout altijd afgemeld en de bus gesloten; Trusted niet gezet.
    assert "UnregisterAgent" in bus.members()
    assert bus.unexported == [bp.AGENT_PATH]
    assert bus.closed
    assert bus.properties == []


async def test_pair_timeout_cancels_pairing_and_cleans_up() -> None:
    async def hang(bus: FakeBus) -> None:
        await asyncio.sleep(30)

    bus = FakeBus(make_objects(), on_pair=hang)
    with pytest.raises(bp.BlePairingError, match="time-out"):
        await pair(bus, timeout=0.05)
    members = bus.members()
    assert "CancelPairing" in members
    assert "UnregisterAgent" in members
    assert bus.closed


async def test_pair_cancellation_cancels_pairing_and_cleans_up() -> None:
    started = asyncio.Event()

    async def hang(bus: FakeBus) -> None:
        started.set()
        await asyncio.sleep(30)

    bus = FakeBus(make_objects(), on_pair=hang)
    task = asyncio.ensure_future(pair(bus, timeout=30))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    members = bus.members()
    assert "CancelPairing" in members
    assert "UnregisterAgent" in members
    assert bus.closed


async def test_register_agent_error_is_mapped_and_nothing_is_unregistered() -> None:
    bus = FakeBus(
        make_objects(), errors={"RegisterAgent": bp.BluezDBusError("org.bluez.Error.AlreadyExists")}
    )
    with pytest.raises(bp.BlePairingError, match="Agent registreren"):
        await pair(bus)
    assert "Pair" not in bus.members()
    assert "UnregisterAgent" not in bus.members()  # nooit geregistreerd
    assert bus.unexported == [bp.AGENT_PATH]
    assert bus.closed


async def test_invalid_address() -> None:
    bus = FakeBus(make_objects())
    with pytest.raises(bp.BlePairingError, match="Ongeldig Bluetooth-adres"):
        await bp.pair_async("geen-mac", lambda: 1, 1.0, bus_factory=factory(bus))


async def test_no_adapter_and_adapter_off() -> None:
    with pytest.raises(bp.BlePairingError, match="Geen Bluetooth-adapter"):
        await pair(FakeBus(make_objects(adapter=False, device=False)))
    with pytest.raises(bp.BlePairingError, match="staat uit"):
        await pair(FakeBus(make_objects(powered=False)))


# --- apparaat zoeken ---------------------------------------------------------------------


async def test_unknown_device_starts_and_stops_discovery() -> None:
    bus = FakeBus(make_objects(), appear_after_polls=2)
    result = await pair(bus)
    assert result.paired
    members = bus.members()
    assert members.index("StartDiscovery") < members.index("StopDiscovery")
    assert members.index("StopDiscovery") < members.index("RegisterAgent")


async def test_device_never_found_gives_clear_error_and_stops_discovery() -> None:
    bus = FakeBus(make_objects(device=False))
    with pytest.raises(bp.BlePairingError, match="niet gevonden"):
        await pair(bus, discovery_timeout=0.01)
    assert "StopDiscovery" in bus.members()
    assert "RegisterAgent" not in bus.members()
    assert bus.closed


async def test_discovery_in_progress_is_tolerated() -> None:
    bus = FakeBus(
        make_objects(),
        errors={"StartDiscovery": bp.BluezDBusError("org.bluez.Error.InProgress")},
        appear_after_polls=1,
    )
    result = await pair(bus)
    assert result.paired
    assert "StopDiscovery" not in bus.members()  # onze sessie bestond niet


# --- agent ------------------------------------------------------------------------------


def make_agent(
    provider: Callable[[], int] = lambda: 1, timeout: float | None = 2.0
) -> bp.PairingAgent:
    return bp.PairingAgent(DEV, provider, provider_timeout=timeout)


async def test_agent_returns_provided_passkey() -> None:
    agent = make_agent(lambda: 123456)
    assert await agent.request_passkey(DEV) == 123456
    assert agent.passkey_requested


@pytest.mark.parametrize("value", [0, 999_999])
async def test_agent_accepts_range_edges(value: int) -> None:
    assert await make_agent(lambda: value).request_passkey(DEV) == value


@pytest.mark.parametrize("value", [-1, 1_000_000, True, "123456", None])
async def test_agent_rejects_invalid_passkey(value: Any) -> None:
    agent = make_agent(lambda: value)
    with pytest.raises(bp.AgentRejected) as exc_info:
        await agent.request_passkey(DEV)
    assert exc_info.value.error_name == bp.ERR_REJECTED


async def test_agent_cancels_when_provider_fails() -> None:
    def broken() -> int:
        raise EOFError

    with pytest.raises(bp.AgentRejected) as exc_info:
        await make_agent(broken).request_passkey(DEV)
    assert exc_info.value.error_name == bp.ERR_CANCELED


async def test_agent_cancels_on_timeout_without_blocking_shutdown() -> None:
    release = threading.Event()

    def slow() -> int:
        release.wait(5)
        return 1

    try:
        with pytest.raises(bp.AgentRejected) as exc_info:
            await make_agent(slow, timeout=0.05).request_passkey(DEV)
        assert exc_info.value.error_name == bp.ERR_CANCELED
    finally:
        release.set()


async def test_agent_rejects_other_devices_and_unexpected_requests() -> None:
    agent = make_agent()
    other = f"{ADAPTER}/dev_11_22_33_44_55_66"
    with pytest.raises(bp.AgentRejected):
        await agent.request_passkey(other)
    assert not agent.passkey_requested
    for call in (
        lambda: agent.request_authorization(other),
        lambda: agent.authorize_service(other, "0000180f-0000-1000-8000-00805f9b34fb"),
        lambda: agent.display_passkey(other, 1, 0),
        lambda: agent.request_confirmation(DEV, 123456),
        lambda: agent.request_pin_code(DEV),
        lambda: agent.display_pin_code(DEV, "0000"),
    ):
        with pytest.raises(bp.AgentRejected) as exc_info:
            call()
        assert exc_info.value.error_name == bp.ERR_REJECTED


def test_agent_accepts_informational_requests_for_target() -> None:
    agent = make_agent()
    agent.display_passkey(DEV, 123456, 3)
    agent.request_authorization(DEV)
    agent.authorize_service(DEV, "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000")


def test_agent_cancel_and_release_set_flags() -> None:
    agent = make_agent()
    agent.cancel()
    agent.release()
    assert agent.cancelled and agent.released


# --- ontkoppelen en lijsten --------------------------------------------------------------


async def test_unpair_removes_device_via_adapter() -> None:
    bus = FakeBus(make_objects(paired=True))
    assert await bp.unpair_async(ADDR, bus_factory=factory(bus)) is True
    assert bus.calls == [(ADAPTER, bp.ADAPTER_IFACE, "RemoveDevice", [DEV])]
    assert bus.closed


async def test_unpair_unknown_device_returns_false() -> None:
    bus = FakeBus(make_objects(device=False))
    assert await bp.unpair_async(ADDR, bus_factory=factory(bus)) is False
    assert bus.calls == []
    assert bus.closed


async def test_unpair_error_mapped() -> None:
    bus = FakeBus(
        make_objects(), errors={"RemoveDevice": bp.BluezDBusError("org.bluez.Error.Failed", "x")}
    )
    with pytest.raises(bp.BlePairingError, match="Ontkoppelen"):
        await bp.unpair_async(ADDR, bus_factory=factory(bus))
    assert bus.closed


async def test_paired_devices_filters_prefix_and_state() -> None:
    objects = make_objects(paired=True)
    objects[f"{ADAPTER}/dev_11"] = {
        bp.DEVICE_IFACE: {"Address": "11:22:33:44:55:66", "Alias": "Flipper Zzz", "Paired": False}
    }
    objects[f"{ADAPTER}/dev_22"] = {
        bp.DEVICE_IFACE: {"Address": "22:22:33:44:55:66", "Alias": "Headset", "Paired": True}
    }
    bus = FakeBus(objects)
    devices = await bp.paired_devices_async("flipper", bus_factory=factory(bus))
    assert [(d.address, d.name, d.paired, d.bonded, d.trusted, d.connected) for d in devices] == [
        (ADDR, "Flipper Abc", True, True, False, False)
    ]
    everything = await bp.list_devices_async("", only_paired=False, bus_factory=factory(bus))
    assert len(everything) == 3


def test_sync_wrappers_use_the_bus_factory() -> None:
    bus = FakeBus(make_objects(paired=True))
    devices = bp.paired_devices("Flipper", bus_factory=factory(bus))
    assert [d.address for d in devices] == [ADDR]
    assert bp.unpair(ADDR, bus_factory=factory(FakeBus(make_objects()))) is True


# --- echte dbus-fast-laag ----------------------------------------------------------------


async def test_real_bus_refuses_clearly_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bp, "sys", types.SimpleNamespace(platform="win32"))
    with pytest.raises(bp.BlePairingError, match="alleen op Linux"):
        await bp.pair_async(ADDR, lambda: 1, 1.0)
    with pytest.raises(bp.BlePairingError, match="alleen op Linux"):
        await bp.unpair_async(ADDR)


def test_agent_interface_matches_bluez_signatures() -> None:
    """Alleen met dbus-fast (Linux): de gepubliceerde methoden volgen Agent.rst."""
    pytest.importorskip("dbus_fast")
    iface = bp._build_agent_interface(make_agent())
    assert iface.name == "org.bluez.Agent1"
    xml = iface.introspect().to_xml()
    methods = {
        m.get("name"): (
            [a.get("type") for a in m.findall("arg") if a.get("direction") == "in"],
            [a.get("type") for a in m.findall("arg") if a.get("direction") == "out"],
        )
        for m in xml.findall("method")
    }
    assert methods["RequestPasskey"] == (["o"], ["u"])
    assert methods["RequestConfirmation"] == (["o", "u"], [])
    assert methods["DisplayPasskey"] == (["o", "u", "q"], [])
    assert methods["AuthorizeService"] == (["o", "s"], [])
    assert methods["RequestPinCode"] == (["o"], ["s"])
    assert methods["Cancel"] == ([], [])
    assert methods["Release"] == ([], [])


# --- adapterkeuze ------------------------------------------------------------------------

ADAPTER1 = "/org/bluez/hci1"
DEV1 = f"{ADAPTER1}/dev_AA_BB_CC_DD_EE_FF"


def two_adapters(*, hci1_powered: bool = True, paired_on_hci1: bool = False) -> bp.ManagedObjects:
    """hci0 en hci1, met hetzelfde apparaat onder allebei (BlueZ houdt ze per adapter apart)."""
    objects = make_objects(paired=False)
    objects[ADAPTER1] = {
        bp.ADAPTER_IFACE: {"Address": "00:11:22:33:44:66", "Powered": hci1_powered}
    }
    props = dict(objects[DEV][bp.DEVICE_IFACE])
    props.update(Adapter=ADAPTER1, Paired=paired_on_hci1, Bonded=paired_on_hci1)
    objects[DEV1] = {bp.DEVICE_IFACE: props}
    return objects


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("  ", None), ("hci0", "hci0"), (" hci12 ", "hci12")],
)
def test_normalize_adapter_accepts_hci_names(value: str | None, expected: str | None) -> None:
    assert bp.normalize_adapter(value) == expected


@pytest.mark.parametrize(
    "value", ["hci", "HCI0", "hci-1", "/org/bluez/hci0", "hci0/dev_x", "hci1234", "wlan0"]
)
def test_normalize_adapter_rejects_everything_else(value: str) -> None:
    with pytest.raises(ValueError, match="verwacht hciN"):
        bp.normalize_adapter(value)


def test_find_adapter_by_name_and_clear_errors() -> None:
    objects = two_adapters()
    assert bp.find_adapter(objects) == ADAPTER  # eerste die aan staat
    assert bp.find_adapter(objects, "hci1") == ADAPTER1
    with pytest.raises(bp.BlePairingError, match=r"hci7 bestaat niet.*bekend: hci0, hci1"):
        bp.find_adapter(objects, "hci7")
    with pytest.raises(bp.BlePairingError, match="hci1 staat uit"):
        bp.find_adapter(two_adapters(hci1_powered=False), "hci1")
    with pytest.raises(bp.BlePairingError, match="bekend: geen"):
        bp.find_adapter(make_objects(adapter=False, device=False), "hci0")


async def test_pair_on_a_chosen_adapter_uses_that_device_object() -> None:
    bus = FakeBus(two_adapters())
    result = await pair(bus, adapter="hci1")
    assert result.device_path == DEV1
    assert ("Pair" in bus.members()) and all(
        path == DEV1 for path, _i, member, _b in bus.calls if member == "Pair"
    )


async def test_pair_with_unknown_or_invalid_adapter_fails_before_any_action() -> None:
    bus = FakeBus(make_objects())
    with pytest.raises(bp.BlePairingError, match="hci3 bestaat niet"):
        await pair(bus, adapter="hci3")
    assert "RegisterAgent" not in bus.members()
    assert bus.closed
    with pytest.raises(bp.BlePairingError, match="verwacht hciN"):
        await pair(FakeBus(make_objects()), adapter="../hci0")


async def test_list_and_unpair_respect_the_adapter() -> None:
    bus = FakeBus(two_adapters(paired_on_hci1=True))
    on_hci1 = await bp.list_devices_async("", bus_factory=factory(bus), adapter="hci1")
    assert [d.address for d in on_hci1] == [ADDR]
    on_hci0 = await bp.list_devices_async("", bus_factory=factory(bus), adapter="hci0")
    assert on_hci0 == []  # daar is hij niet gekoppeld

    bus = FakeBus(two_adapters())
    assert await bp.unpair_async(ADDR, bus_factory=factory(bus), adapter="hci1") is True
    assert bus.calls == [(ADAPTER1, bp.ADAPTER_IFACE, "RemoveDevice", [DEV1])]
    with pytest.raises(bp.BlePairingError, match="hci5 bestaat niet"):
        await bp.unpair_async(ADDR, bus_factory=factory(FakeBus(make_objects())), adapter="hci5")


async def test_list_devices_finds_a_renamed_flipper_by_service_uuid() -> None:
    objects = make_objects(paired=True, name="A1tkethe")  # hernoemd: geen "Flipper"-prefix
    objects[DEV][bp.DEVICE_IFACE]["UUIDs"] = [
        "00001800-0000-1000-8000-00805f9b34fb",
        "8FE5B3D5-2E7F-4A98-2A48-7ACC60FE0000",
    ]
    bus = FakeBus(objects)
    assert await bp.list_devices_async("Flipper", bus_factory=factory(bus)) == []
    found = await bp.list_devices_async(
        "Flipper",
        bus_factory=factory(bus),
        service_uuids={"8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"},
    )
    assert [d.name for d in found] == ["A1tkethe"]
