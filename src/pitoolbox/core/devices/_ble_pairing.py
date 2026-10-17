"""BLE-koppeling (pairing) met BlueZ over de system D-Bus, zonder `bluetoothctl`.

Waarom een eigen agent: de Flipper dwingt een geauthenticeerde (MITM) link af en toont tijdens het
koppelen een 6-cijferige code op zijn scherm ("Pairing code\\n%06lu"). De host moet die code
INVOEREN. Dat gaat via een BlueZ-pairing-agent; die registreren we tijdelijk, alleen voor de duur
van de koppeling.

Bronnen (alles hieronder is daaruit afgeleid):

* Agent-API: https://github.com/bluez/bluez/blob/master/doc/org.bluez.Agent.rst
  (interface org.bluez.Agent1; `uint32 RequestPasskey(object device)` geeft 0..999999 terug en mag
  org.bluez.Error.Rejected / org.bluez.Error.Canceled geven; `RequestConfirmation(object device,
  uint32 passkey)`, `DisplayPasskey(object device, uint32 passkey, uint16 entered)`,
  `AuthorizeService(object device, string uuid)`, `RequestAuthorization(object device)`,
  `RequestPinCode`, `DisplayPinCode`, `Cancel()`, `Release()`).
* AgentManager: https://github.com/bluez/bluez/blob/master/doc/org.bluez.AgentManager.rst
  (`RegisterAgent(object agent, string capability)`, `UnregisterAgent(object)`,
  `RequestDefaultAgent(object)` op /org/bluez; capability "KeyboardOnly" is een geldige waarde).
* Device: https://github.com/bluez/bluez/blob/master/doc/org.bluez.Device.rst
  (`Pair()`, `CancelPairing()`, `Disconnect()`, properties Paired, Bonded, Trusted (readwrite),
  Connected, Adapter, Name, Alias; foutlijst van Pair() staat in de docstring van `_ERRORS`).
* Adapter: https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst
  (`StartDiscovery()`, `StopDiscovery()`, `RemoveDevice(object device)` verwijdert het device
  inclusief bonding-informatie). Objectpad van een adapter: `/org/bluez/hciX` ("Object path
  [variable prefix]/{hci0,hci1,...}"); bleak 3.0.2 bouwt het pad net zo uit de naam
  (bleak/backends/bluezdbus/scanner.py `start`, client.py `_get_adapter_path`).
* dbus-fast: https://dbus-fast.readthedocs.io (aio.MessageBus, ServiceInterface, @dbus_method,
  DBusError). Zelfde patroon als bleak 3.0.2: .venv/Lib/site-packages/bleak/backends/bluezdbus/
  (manager.py: GetManagedObjects; client.py: Pair/RemoveDevice; advertisement_monitor.py:
  ServiceInterface met @method).
* Waarom KeyboardOnly: de Flipper stelt in targets/f7/ble_glue/gap.c (@3fb1d517) bij pairing-
  methode GapPairingPinCodeShow IO_CAP_DISPLAY_ONLY + MITM_PROTECTION_REQUIRED in. Volgens de
  Bluetooth Core Spec (Vol 3, Part H, 2.3.5.1, IO-capabilities-mapping) is dat met een
  KeyboardOnly-initiator "Passkey Entry: responder toont, initiator voert in": BlueZ roept dan
  `RequestPasskey` aan en nooit `RequestConfirmation`. De spec-tabel zelf is hier niet
  geverifieerd (alleen de Flipper-broncode en de BlueZ-docs); op de hardware-testlijst.

dbus-fast is alleen op Linux geinstalleerd (bleak-afhankelijkheid, marker sys_platform ==
'linux'); daarom wordt het pas bij gebruik geimporteerd (importlib, zodat mypy/ruff ook op Windows
schoon zijn). Alle logica praat tegen het kleine `BluezBus`-protocol, dat tests volledig faken.

Opbouw: `_bluez_dbus` (namen, foutmeldingen, `BluezBus`, opzoekingen), `_bluez_agent` (de agent),
`_bluez_fastbus` (de echte bus op dbus-fast), `_bluez_devices` (bekende apparaten, ontkoppelen,
verbreken) en hier het koppelen zelf. Andere code importeert alles uit deze module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from pitoolbox.core.devices._bluez_agent import AgentRejected, PairingAgent
from pitoolbox.core.devices._bluez_dbus import (
    ADAPTER_IFACE,
    AGENT_CAPABILITY,
    AGENT_IFACE,
    AGENT_MANAGER_IFACE,
    AGENT_PATH,
    BLUEZ_SERVICE,
    DEVICE_IFACE,
    ERR_CANCELED,
    ERR_REJECTED,
    PROPERTIES_IFACE,
    ROOT_PATH,
    BlePairingError,
    BluezBus,
    BluezDBusError,
    BusFactory,
    ManagedObjects,
    _adapter_arg,
    _call,
    _mac,
    adapter_path,
    describe_bluez_error,
    find_adapter,
    find_device,
    normalize_adapter,
)
from pitoolbox.core.devices._bluez_devices import (
    PairedDevice,
    _as_paired_device,
    disconnect,
    disconnect_async,
    list_devices,
    list_devices_async,
    paired_devices,
    paired_devices_async,
    unpair,
    unpair_async,
)
from pitoolbox.core.devices._bluez_fastbus import connect_bus

__all__ = [
    "ADAPTER_IFACE",
    "AGENT_CAPABILITY",
    "AGENT_IFACE",
    "AGENT_MANAGER_IFACE",
    "AGENT_PATH",
    "BLUEZ_SERVICE",
    "DEVICE_IFACE",
    "ERR_CANCELED",
    "ERR_REJECTED",
    "PROPERTIES_IFACE",
    "ROOT_PATH",
    "AgentRejected",
    "BlePairingError",
    "BluezBus",
    "BluezDBusError",
    "BusFactory",
    "ManagedObjects",
    "PairResult",
    "PairedDevice",
    "PairingAgent",
    "adapter_path",
    "connect_bus",
    "describe_bluez_error",
    "disconnect",
    "disconnect_async",
    "find_adapter",
    "find_device",
    "list_devices",
    "list_devices_async",
    "normalize_adapter",
    "pair",
    "pair_async",
    "paired_devices",
    "paired_devices_async",
    "unpair",
    "unpair_async",
]

log = logging.getLogger(__name__)


# --- resultaattypes ----------------------------------------------------------------------


@dataclass(frozen=True)
class PairResult:
    address: str
    name: str | None
    device_path: str
    already_paired: bool
    paired: bool
    bonded: bool
    trusted: bool


async def _resolve_device(
    bus: BluezBus,
    mac: str,
    *,
    discovery_timeout: float,
    poll_s: float,
    adapter_name: str | None = None,
) -> tuple[str, str]:
    """Geef (adapterpad, devicepad); start zo nodig een korte discovery."""
    objects = await bus.get_managed_objects()
    adapter = find_adapter(objects, adapter_name)
    path = find_device(objects, mac, adapter)
    if path is not None:
        return adapter, path

    log.info("apparaat %s onbekend bij BlueZ: korte discovery (max %.0f s)", mac, discovery_timeout)
    started = False
    try:
        await _call(bus, "Discovery starten", adapter, ADAPTER_IFACE, "StartDiscovery")
        started = True
    except BlePairingError as exc:
        # InProgress: een andere client scant al; dan kijken we gewoon mee.
        if exc.dbus_error != "org.bluez.Error.InProgress":
            raise
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + discovery_timeout
        while True:
            await asyncio.sleep(poll_s)
            path = find_device(await bus.get_managed_objects(), mac, adapter)
            if path is not None:
                return adapter, path
            if loop.time() >= deadline:
                break
    finally:
        if started:
            with contextlib.suppress(BlePairingError):
                await _call(bus, "Discovery stoppen", adapter, ADAPTER_IFACE, "StopDiscovery")
    raise BlePairingError(
        f"Apparaat {mac} niet gevonden. Zet Bluetooth aan op de Flipper (Settings > Bluetooth), "
        "houd hem dichtbij en draai eerst 'ble-scan'."
    )


async def _set_trusted(bus: BluezBus, device_path: str) -> bool:
    try:
        await bus.set_property(device_path, DEVICE_IFACE, "Trusted", "b", True)
    except BluezDBusError as exc:
        log.warning("Trusted zetten mislukt: %s", describe_bluez_error(exc))
        return False
    return True


async def _cancel_pairing(bus: BluezBus, device_path: str) -> None:
    with contextlib.suppress(BluezDBusError):
        await bus.call(device_path, DEVICE_IFACE, "CancelPairing")


# --- publieke async-API ------------------------------------------------------------------


async def pair_async(
    address: str,
    passkey_provider: Callable[[], int],
    timeout: float = 90.0,
    *,
    bus_factory: BusFactory | None = None,
    discovery_timeout: float = 10.0,
    poll_s: float = 0.5,
    disconnect_after: bool = True,
    adapter: str | None = None,
) -> PairResult:
    """Koppel met `address`. `passkey_provider` levert de code van het Flipper-scherm.

    `timeout` geldt voor de Pair()-aanroep, dus inclusief de tijd dat de gebruiker de code
    intikt. Na afloop (ook bij een fout) is de agent altijd afgemeld. `adapter` (`hciN`) kiest
    de Bluetooth-adapter; None = de eerste ingeschakelde.
    """
    mac = _mac(address)
    adapter = _adapter_arg(adapter)
    bus = await connect_bus(bus_factory)
    exported = False
    registered = False
    try:
        _adapter, device_path = await _resolve_device(
            bus,
            mac,
            discovery_timeout=min(discovery_timeout, timeout),
            poll_s=poll_s,
            adapter_name=adapter,
        )
        props = (await bus.get_managed_objects())[device_path][DEVICE_IFACE]

        if props.get("Paired", False):
            trusted = bool(props.get("Trusted", False)) or await _set_trusted(bus, device_path)
            return PairResult(
                address=mac,
                name=_as_paired_device(props).name,
                device_path=device_path,
                already_paired=True,
                paired=True,
                bonded=bool(props.get("Bonded", False)),
                trusted=trusted,
            )

        agent = PairingAgent(device_path, passkey_provider, provider_timeout=timeout)
        bus.export_agent(AGENT_PATH, agent)
        exported = True
        await _call(
            bus,
            "Agent registreren",
            ROOT_PATH,
            AGENT_MANAGER_IFACE,
            "RegisterAgent",
            "os",
            [AGENT_PATH, AGENT_CAPABILITY],
        )
        registered = True
        # Als default agent bedienen we ook door de Flipper zelf gestarte beveiliging (een
        # Security Request); een eigen agent geldt volgens AgentManager.rst alleen voor
        # aanroepen van deze applicatie. Mag het niet ("special permission"), dan is dat niet
        # fataal.
        try:
            await bus.call(ROOT_PATH, AGENT_MANAGER_IFACE, "RequestDefaultAgent", "o", [AGENT_PATH])
        except BluezDBusError as exc:
            log.warning("RequestDefaultAgent geweigerd: %s", describe_bluez_error(exc))

        try:
            await asyncio.wait_for(bus.call(device_path, DEVICE_IFACE, "Pair"), timeout)
        except BluezDBusError as exc:
            message = describe_bluez_error(exc, "Koppelen mislukt")
            if agent.cancelled:
                message += " (BlueZ trok het verzoek in)."
            raise BlePairingError(message, dbus_error=exc.name) from exc
        except TimeoutError as exc:
            await _cancel_pairing(bus, device_path)
            hint = (
                "geen code ingevoerd"
                if agent.passkey_requested
                else "de Flipper vroeg geen code (staat Bluetooth aan en is hij niet verbonden "
                "met een telefoon?)"
            )
            raise BlePairingError(
                f"Koppelen mislukt: time-out na {timeout:.0f} s ({hint}).",
                dbus_error="timeout",
            ) from exc
        except asyncio.CancelledError:
            await _cancel_pairing(bus, device_path)
            raise

        trusted = await _set_trusted(bus, device_path)
        final = (await bus.get_managed_objects()).get(device_path, {}).get(DEVICE_IFACE, props)
        if disconnect_after and final.get("Connected", False):
            # De Flipper staat maar een BLE-verbinding toe: laat hem vrij voor de service.
            with contextlib.suppress(BluezDBusError):
                await bus.call(device_path, DEVICE_IFACE, "Disconnect")
        return PairResult(
            address=mac,
            name=_as_paired_device(final).name,
            device_path=device_path,
            already_paired=False,
            paired=bool(final.get("Paired", True)),
            bonded=bool(final.get("Bonded", False)),
            trusted=trusted,
        )
    finally:
        if registered:
            # DoesNotExist (al afgemeld na Release()) en andere fouten negeren we hier bewust.
            with contextlib.suppress(Exception):
                await bus.call(ROOT_PATH, AGENT_MANAGER_IFACE, "UnregisterAgent", "o", [AGENT_PATH])
        if exported:
            with contextlib.suppress(Exception):
                bus.unexport_agent(AGENT_PATH)
        with contextlib.suppress(Exception):
            await bus.close()


# --- synchrone wrappers voor de CLI ------------------------------------------------------


def pair(
    address: str,
    passkey_provider: Callable[[], int],
    timeout: float = 90.0,
    *,
    bus_factory: BusFactory | None = None,
    adapter: str | None = None,
) -> PairResult:
    """Synchrone variant van `pair_async` (niet aanroepen vanuit een draaiende event loop)."""
    return asyncio.run(
        pair_async(address, passkey_provider, timeout, bus_factory=bus_factory, adapter=adapter)
    )
