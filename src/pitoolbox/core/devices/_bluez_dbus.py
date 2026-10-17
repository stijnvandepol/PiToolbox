"""BlueZ over de system D-Bus: namen, foutmeldingen, het `BluezBus`-protocol en opzoekingen.

Alle logica praat tegen het kleine `BluezBus`-protocol (tests faken het volledig); de echte
implementatie op dbus-fast staat in `_bluez_fastbus`. Bronnen (BlueZ-docs): zie _ble_pairing.py.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from pitoolbox.core.errors import DeviceError
from pitoolbox.core.scope import normalize_mac

if TYPE_CHECKING:
    from pitoolbox.core.devices._bluez_agent import PairingAgent

BLUEZ_SERVICE = "org.bluez"
ROOT_PATH = "/org/bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# Object-pad van onze tijdelijke agent ("freely definable", Agent.rst).
AGENT_PATH = "/nl/pitoolbox/PairingAgent"
# Zie de module-docstring: de Flipper toont de code, wij voeren hem in.
AGENT_CAPABILITY = "KeyboardOnly"

ERR_REJECTED = "org.bluez.Error.Rejected"
ERR_CANCELED = "org.bluez.Error.Canceled"

# {object path: {interface: {property: waarde}}} zoals GetManagedObjects (varianten uitgepakt).
ManagedObjects = dict[str, dict[str, dict[str, Any]]]

# Adapternamen zoals BlueZ/de kernel ze geven (hci0, hci1, ...); zie de module-docstring.
_ADAPTER_NAME_RE = re.compile(r"hci[0-9]{1,3}")


def normalize_adapter(value: str | None) -> str | None:
    """Controleer een adapternaam (`hciN`); leeg of None = de standaardadapter van BlueZ.

    Raises:
        ValueError: geen geldige naam (er wordt nooit een willekeurig D-Bus-pad doorgegeven).
    """
    if value is None or not value.strip():
        return None
    name = value.strip()
    if not _ADAPTER_NAME_RE.fullmatch(name):
        raise ValueError(f"ongeldige Bluetooth-adapter {value!r} (verwacht hciN, bijv. hci1)")
    return name


def adapter_path(adapter: str) -> str:
    return f"{ROOT_PATH}/{adapter}"


# --- fouten ------------------------------------------------------------------------------


class BlePairingError(DeviceError):
    """Koppelen/ontkoppelen mislukt; `str(exc)` is een bruikbare Nederlandse melding."""

    code = "ble_pairing_error"

    def __init__(self, message: str, *, dbus_error: str | None = None) -> None:
        super().__init__(message)
        self.dbus_error = dbus_error


class BluezDBusError(Exception):
    """Een D-Bus-foutantwoord van BlueZ (of van de bus zelf), zonder dbus-fast-types."""

    def __init__(self, name: str, text: str = "") -> None:
        super().__init__(f"{name}: {text}" if text else name)
        self.name = name
        self.text = text


# Foutnamen uit de BlueZ-docs (Device.rst `Pair()`, Adapter.rst, AgentManager.rst) en de
# standaardfouten van de D-Bus-daemon, vertaald naar een melding met de eerstvolgende stap.
_ERRORS: dict[str, str] = {
    "org.bluez.Error.AuthenticationFailed": (
        "Koppelen mislukt: authenticatie mislukt. Klopt de code van het Flipper-scherm? Staat er "
        "een oude koppeling op de Flipper of op de Pi, dan klopt de sleutel niet meer: verwijder "
        "de koppeling aan beide kanten ('ble-unpair' hier, op de Flipper Settings > Bluetooth > "
        "Unpair All Devices) en probeer opnieuw."
    ),
    "org.bluez.Error.AuthenticationCanceled": (
        "Koppelen afgebroken (geen of geannuleerde code). Start 'ble-pair' opnieuw en voer de code "
        "in zodra die op de Flipper staat."
    ),
    "org.bluez.Error.AuthenticationRejected": (
        "De Flipper heeft het koppelen geweigerd. Staat Bluetooth aan (Settings > Bluetooth) en "
        "is de Flipper niet al gekoppeld aan een telefoon of andere host?"
    ),
    "org.bluez.Error.AuthenticationTimeout": (
        "Koppelen mislukt: time-out tijdens de authenticatie. Voer de code sneller in (de "
        "Flipper wacht maar beperkt) en probeer opnieuw."
    ),
    "org.bluez.Error.ConnectionAttemptFailed": (
        "Geen verbinding met de Flipper. Staat Bluetooth aan, ligt hij binnen bereik en is hij "
        "niet verbonden met een telefoon (de officiele Flipper-app houdt de enige BLE-verbinding "
        "vast)?"
    ),
    "org.bluez.Error.AlreadyExists": (
        "Al gekoppeld of er loopt al een koppeling. Wil je opnieuw koppelen: eerst 'ble-unpair "
        "<adres>' en op de Flipper 'Unpair All Devices'."
    ),
    "org.bluez.Error.InProgress": (
        "Er loopt al een koppel- of verbindingspoging. Wacht even of herstart de bluetooth-service."
    ),
    "org.bluez.Error.NotReady": (
        "De Bluetooth-adapter is niet klaar of staat uit (probeer 'rfkill unblock bluetooth' en "
        "controleer 'systemctl status bluetooth')."
    ),
    "org.bluez.Error.DoesNotExist": "Het Bluetooth-apparaat is niet (meer) bekend bij BlueZ.",
    "org.bluez.Error.NotAuthorized": (
        "Geen rechten voor deze Bluetooth-actie. Zit de gebruiker in de groep 'bluetooth'?"
    ),
    "org.bluez.Error.InvalidArguments": "BlueZ weigert de aanvraag (ongeldige argumenten).",
    "org.bluez.Error.Failed": "BlueZ meldde een algemene fout (Failed).",
    "org.freedesktop.DBus.Error.AccessDenied": (
        "De D-Bus-policy weigert toegang tot BlueZ. Zit de gebruiker in de groep 'bluetooth'?"
    ),
    "org.freedesktop.DBus.Error.ServiceUnknown": (
        "BlueZ (org.bluez) draait niet. Start de service: 'sudo systemctl enable --now bluetooth'."
    ),
    "org.freedesktop.DBus.Error.NoReply": (
        "BlueZ antwoordde niet op tijd (time-out op de D-Bus-aanroep)."
    ),
}


def describe_bluez_error(error: BluezDBusError, context: str = "") -> str:
    """Nederlandse melding bij een D-Bus-fout; onbekende fouten krijgen naam en tekst mee."""
    base = _ERRORS.get(error.name)
    if base is None:
        base = f"Bluetooth-fout {error.name}" + (f": {error.text}" if error.text else "")
    elif error.name == "org.bluez.Error.Failed" and error.text:
        base = f"{base[:-1]}: {error.text}"
    return f"{context}: {base}" if context else base


# --- het bus-protocol (volledig te faken) ------------------------------------------------


class BluezBus(Protocol):
    """Dunne laag over dbus-fast; alles wat deze module van D-Bus nodig heeft."""

    async def call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Sequence[Any] = (),
    ) -> list[Any]:
        """Methodeaanroep op org.bluez; geeft de body terug, of gooit `BluezDBusError`."""
        ...

    async def get_managed_objects(self) -> ManagedObjects: ...

    async def set_property(
        self, path: str, interface: str, name: str, signature: str, value: Any
    ) -> None: ...

    async def get_property(self, path: str, interface: str, name: str) -> Any:
        """org.freedesktop.DBus.Properties.Get, uitgepakt; gooit `BluezDBusError`."""
        ...

    def export_agent(self, path: str, agent: PairingAgent) -> None: ...

    def unexport_agent(self, path: str) -> None: ...

    async def close(self) -> None: ...


BusFactory = Callable[[], Awaitable[BluezBus]]


def _adapter_arg(adapter: str | None) -> str | None:
    try:
        return normalize_adapter(adapter)
    except ValueError as exc:
        raise BlePairingError(str(exc)) from exc


# --- hulpfuncties op ManagedObjects ------------------------------------------------------


def _mac(address: str) -> str:
    try:
        return normalize_mac(address)
    except ValueError as exc:
        raise BlePairingError(
            f"Ongeldig Bluetooth-adres: {address!r} (verwacht AA:BB:CC:DD:EE:FF)"
        ) from exc


def find_adapter(objects: ManagedObjects, adapter: str | None = None) -> str:
    """Objectpad van de adapter: de opgegeven `hciN`, anders de eerste ingeschakelde (zoals bleak:
    manager.get_default_adapter). Een adapter die BlueZ niet kent of die uit staat is een fout
    met de namen die er wel zijn."""
    adapters = sorted(path for path, ifaces in objects.items() if ADAPTER_IFACE in ifaces)
    known = ", ".join(path.rsplit("/", 1)[-1] for path in adapters) or "geen"
    if adapter is not None:
        path = adapter_path(adapter)
        if path not in adapters:
            raise BlePairingError(
                f"Bluetooth-adapter {adapter} bestaat niet in BlueZ (bekend: {known}). Controleer "
                "PITOOLBOX_FLIPPER_BLE_ADAPTER of --adapter en 'bluetoothctl list'; een adapter "
                "waarvan de firmware niet start (dmesg) ziet BlueZ niet."
            )
        if not objects[path][ADAPTER_IFACE].get("Powered", False):
            raise BlePairingError(
                f"Bluetooth-adapter {adapter} staat uit. Zet hem aan met 'rfkill unblock "
                "bluetooth' en 'bluetoothctl power on'."
            )
        return path
    if not adapters:
        raise BlePairingError(
            "Geen Bluetooth-adapter gevonden in BlueZ (controleer 'bluetoothctl list' en of de "
            "bluetooth-service draait)."
        )
    for path in adapters:
        if objects[path][ADAPTER_IFACE].get("Powered", False):
            return path
    raise BlePairingError(
        "De Bluetooth-adapter staat uit. Zet hem aan met 'rfkill unblock bluetooth' en "
        "'bluetoothctl power on'."
    )


def _devices(objects: ManagedObjects) -> list[tuple[str, dict[str, Any]]]:
    return [
        (path, ifaces[DEVICE_IFACE])
        for path, ifaces in sorted(objects.items())
        if DEVICE_IFACE in ifaces
    ]


def find_device(objects: ManagedObjects, mac: str, adapter: str | None = None) -> str | None:
    """Devicepad van `mac` (hoofdletters), optioneel alleen onder adapterpad `adapter`."""
    for path, props in _devices(objects):
        if str(props.get("Address", "")).upper() != mac:
            continue
        if adapter is not None and props.get("Adapter", adapter) != adapter:
            continue
        return path
    return None


async def _call(  # noqa: PLR0913 spiegelt bus.call (dbus-fast Message)
    bus: BluezBus,
    context: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: Sequence[Any] = (),
) -> list[Any]:
    """`bus.call` met vertaling van D-Bus-fouten naar `BlePairingError`."""
    try:
        return await bus.call(path, interface, member, signature, body)
    except BluezDBusError as exc:
        raise BlePairingError(describe_bluez_error(exc, context), dbus_error=exc.name) from exc
