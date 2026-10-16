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
  inclusief bonding-informatie).
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
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pitoolbox.core.errors import DeviceError
from pitoolbox.core.scope import normalize_mac

log = logging.getLogger(__name__)

T = TypeVar("T")

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


class AgentRejected(Exception):
    """Door de agent te weigeren aanvraag; wordt een D-Bus-fout met `error_name`."""

    def __init__(self, error_name: str, text: str) -> None:
        super().__init__(text)
        self.error_name = error_name
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


@dataclass(frozen=True)
class PairedDevice:
    address: str
    name: str | None
    paired: bool
    bonded: bool
    trusted: bool
    connected: bool


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

    def export_agent(self, path: str, agent: PairingAgent) -> None: ...

    def unexport_agent(self, path: str) -> None: ...

    async def close(self) -> None: ...


BusFactory = Callable[[], Awaitable[BluezBus]]


# --- de agent --------------------------------------------------------------------------


async def _call_in_daemon_thread(fn: Callable[[], T], timeout: float | None) -> T:
    """Draai een blokkerende aanroep (bijv. `input()`) in een daemon-thread.

    Bewust geen `run_in_executor`: `asyncio.run` wacht bij het afsluiten op de threads van de
    standaard-executor, waardoor het proces bleef hangen tot de gebruiker op Enter drukte nadat
    de koppeling al was afgebroken. Een daemon-thread blokkeert het afsluiten niet.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[T] = loop.create_future()

    def _set_result(value: T) -> None:
        if not fut.done():
            fut.set_result(value)

    def _set_exception(exc: Exception) -> None:
        if not fut.done():
            fut.set_exception(exc)

    def runner() -> None:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - elke fout van de provider naar de event loop
            with contextlib.suppress(RuntimeError):  # loop al gesloten
                loop.call_soon_threadsafe(_set_exception, exc)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_set_result, value)

    threading.Thread(target=runner, daemon=True, name="ble-passkey-prompt").start()
    return await asyncio.wait_for(fut, timeout)


class PairingAgent:
    """Logica van de org.bluez.Agent1-agent; los van dbus-fast, dus eenvoudig te testen.

    Alleen het apparaat dat we zelf koppelen (`target_path`) wordt bediend; elk ander apparaat
    en elk onverwacht verzoek krijgt org.bluez.Error.Rejected (Agent.rst: `Possible errors`).
    De code zelf wordt nooit gelogd.
    """

    def __init__(
        self,
        target_path: str,
        passkey_provider: Callable[[], int],
        *,
        provider_timeout: float | None = None,
    ) -> None:
        self._target = target_path
        self._provider = passkey_provider
        self._timeout = provider_timeout
        self.passkey_requested = False
        self.cancelled = False
        self.released = False

    def _check_target(self, device: str) -> None:
        if device != self._target:
            log.warning("agent weigert verzoek voor onverwacht apparaat %s", device)
            raise AgentRejected(ERR_REJECTED, "onverwacht apparaat")

    async def request_passkey(self, device: str) -> int:
        """`uint32 RequestPasskey(object device)`: de code die de gebruiker intikt (0..999999)."""
        self._check_target(device)
        self.passkey_requested = True
        try:
            value = await _call_in_daemon_thread(self._provider, self._timeout)
        except TimeoutError as exc:
            raise AgentRejected(ERR_CANCELED, "geen code ingevoerd binnen de tijd") from exc
        except Exception as exc:  # EOF of een fout in de provider
            raise AgentRejected(ERR_CANCELED, "code-invoer afgebroken") from exc
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999_999:
            raise AgentRejected(ERR_REJECTED, "ongeldige code (verwacht 0..999999)")
        return value

    def request_confirmation(self, device: str, passkey: int) -> None:
        """Numerieke vergelijking verwachten we niet (KeyboardOnly): altijd weigeren."""
        raise AgentRejected(ERR_REJECTED, "numerieke bevestiging niet ondersteund")

    def display_passkey(self, device: str, passkey: int, entered: int) -> None:
        """Alleen een weergaveverzoek (lege reactie volgens de docs); we tonen niets."""
        self._check_target(device)

    def request_authorization(self, device: str) -> None:
        """Just-works-verzoek: alleen voor het apparaat dat de gebruiker zelf koos."""
        self._check_target(device)

    def authorize_service(self, device: str, uuid: str) -> None:
        """Dienstautorisatie (BR/EDR-profielen): alleen voor het gekozen apparaat."""
        self._check_target(device)

    def request_pin_code(self, device: str) -> str:
        """Legacy-PIN (pre-2.1): niet ondersteund."""
        raise AgentRejected(ERR_REJECTED, "legacy PIN-koppeling niet ondersteund")

    def display_pin_code(self, device: str, pincode: str) -> None:
        raise AgentRejected(ERR_REJECTED, "legacy PIN-koppeling niet ondersteund")

    def cancel(self) -> None:
        """`Cancel()`: BlueZ trok de aanvraag in voor er een antwoord was."""
        self.cancelled = True

    def release(self) -> None:
        """`Release()`: bluetoothd heeft de agent al afgemeld (geen UnregisterAgent nodig)."""
        self.released = True


def _build_agent_interface(agent: PairingAgent) -> Any:
    """Maak het dbus-fast `ServiceInterface`-object voor org.bluez.Agent1.

    De klasse wordt dynamisch gebouwd omdat dbus-fast niet op elk platform bestaat en omdat
    dbus-fast de D-Bus-signatuur uit `__annotations__` leest: met `from __future__ import
    annotations` zouden dat Python-typenamen zijn, dus zetten we de signaturen expliciet
    (documentatie: dbus_method-decorator, "must each be annotated with a signature string").
    """
    service = importlib.import_module("dbus_fast.service")
    errors = importlib.import_module("dbus_fast.errors")

    def rejected(exc: AgentRejected) -> Exception:
        return errors.DBusError(exc.error_name, exc.text)  # type: ignore[no-any-return]

    def sig(fn: Callable[..., Any], **annotations: str) -> Callable[..., Any]:
        fn.__annotations__ = dict(annotations)
        return fn

    async def request_passkey(self: Any, device: str) -> int:
        try:
            return await agent.request_passkey(device)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    def request_confirmation(self: Any, device: str, passkey: int) -> None:
        try:
            agent.request_confirmation(device, passkey)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    def display_passkey(self: Any, device: str, passkey: int, entered: int) -> None:
        try:
            agent.display_passkey(device, passkey, entered)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    def request_authorization(self: Any, device: str) -> None:
        try:
            agent.request_authorization(device)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    def authorize_service(self: Any, device: str, uuid: str) -> None:
        try:
            agent.authorize_service(device, uuid)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    def request_pin_code(self: Any, device: str) -> str:
        try:
            return agent.request_pin_code(device)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    def display_pin_code(self: Any, device: str, pincode: str) -> None:
        try:
            agent.display_pin_code(device, pincode)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    def cancel(self: Any) -> None:
        agent.cancel()

    def release(self: Any) -> None:
        agent.release()

    dbus_method = service.dbus_method
    members: dict[str, Any] = {
        "RequestPasskey": dbus_method(name="RequestPasskey")(
            sig(request_passkey, device="o", **{"return": "u"})
        ),
        "RequestConfirmation": dbus_method(name="RequestConfirmation")(
            sig(request_confirmation, device="o", passkey="u")
        ),
        "DisplayPasskey": dbus_method(name="DisplayPasskey")(
            sig(display_passkey, device="o", passkey="u", entered="q")
        ),
        "RequestAuthorization": dbus_method(name="RequestAuthorization")(
            sig(request_authorization, device="o")
        ),
        "AuthorizeService": dbus_method(name="AuthorizeService")(
            sig(authorize_service, device="o", uuid="s")
        ),
        "RequestPinCode": dbus_method(name="RequestPinCode")(
            sig(request_pin_code, device="o", **{"return": "s"})
        ),
        "DisplayPinCode": dbus_method(name="DisplayPinCode")(
            sig(display_pin_code, device="o", pincode="s")
        ),
        "Cancel": dbus_method(name="Cancel")(sig(cancel)),
        "Release": dbus_method(name="Release")(sig(release)),
    }
    cls = type("PitoolboxPairingAgent", (service.ServiceInterface,), members)
    return cls(AGENT_IFACE)


# --- echte bus (dbus-fast) --------------------------------------------------------------


class _DbusFastBus:
    """`BluezBus` bovenop dbus_fast.aio.MessageBus (system bus)."""

    def __init__(self, bus: Any, modules: dict[str, Any]) -> None:
        self._bus = bus
        self._m = modules

    @classmethod
    async def connect(cls) -> _DbusFastBus:
        if sys.platform != "linux":
            raise BlePairingError(
                "Bluetooth-koppeling werkt alleen op Linux met BlueZ (de Raspberry Pi); "
                f"dit is {sys.platform}."
            )
        try:
            modules = {
                "aio": importlib.import_module("dbus_fast.aio"),
                "constants": importlib.import_module("dbus_fast.constants"),
                "message": importlib.import_module("dbus_fast.message"),
                "signature": importlib.import_module("dbus_fast.signature"),
                "root": importlib.import_module("dbus_fast"),
            }
        except ImportError as exc:
            raise BlePairingError(
                "dbus-fast is niet geinstalleerd (komt mee met bleak op Linux): draai 'uv sync'."
            ) from exc
        try:
            bus = (
                await modules["aio"]
                .MessageBus(bus_type=modules["constants"].BusType.SYSTEM)
                .connect()
            )
        except Exception as exc:  # socket-, auth- en policy-fouten van dbus-fast
            raise BlePairingError(
                f"Geen verbinding met de system D-Bus ({exc}). Draait dbus en BlueZ?"
            ) from exc
        return cls(bus, modules)

    async def call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Sequence[Any] = (),
    ) -> list[Any]:
        message = self._m["message"].Message(
            destination=BLUEZ_SERVICE,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=list(body),
        )
        reply = await self._bus.call(message)
        if reply.message_type == self._m["constants"].MessageType.ERROR:
            text = str(reply.body[0]) if reply.body else ""
            raise BluezDBusError(reply.error_name or "org.bluez.Error.Failed", text)
        return list(reply.body)

    async def get_managed_objects(self) -> ManagedObjects:
        body = await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        unpacked: ManagedObjects = self._m["root"].unpack_variants(body[0])
        return unpacked

    async def set_property(
        self, path: str, interface: str, name: str, signature: str, value: Any
    ) -> None:
        variant = self._m["signature"].Variant(signature, value)
        await self.call(path, PROPERTIES_IFACE, "Set", "ssv", [interface, name, variant])

    def export_agent(self, path: str, agent: PairingAgent) -> None:
        self._bus.export(path, _build_agent_interface(agent))

    def unexport_agent(self, path: str) -> None:
        self._bus.unexport(path)

    async def close(self) -> None:
        self._bus.disconnect()
        with contextlib.suppress(Exception):
            await self._bus.wait_for_disconnect()


async def _connect(bus_factory: BusFactory | None) -> BluezBus:
    if bus_factory is not None:
        return await bus_factory()
    return await _DbusFastBus.connect()


# --- hulpfuncties op ManagedObjects ------------------------------------------------------


def _mac(address: str) -> str:
    try:
        return normalize_mac(address)
    except ValueError as exc:
        raise BlePairingError(
            f"Ongeldig Bluetooth-adres: {address!r} (verwacht AA:BB:CC:DD:EE:FF)"
        ) from exc


def _find_adapter(objects: ManagedObjects) -> str:
    """Eerste ingeschakelde adapter (zoals bleak: manager.get_default_adapter)."""
    adapters = sorted(path for path, ifaces in objects.items() if ADAPTER_IFACE in ifaces)
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


def _find_device(objects: ManagedObjects, mac: str, adapter: str | None = None) -> str | None:
    for path, props in _devices(objects):
        if str(props.get("Address", "")).upper() != mac:
            continue
        if adapter is not None and props.get("Adapter", adapter) != adapter:
            continue
        return path
    return None


def _as_paired_device(props: dict[str, Any]) -> PairedDevice:
    name = props.get("Alias") or props.get("Name")
    return PairedDevice(
        address=str(props.get("Address", "")).upper(),
        name=str(name) if name else None,
        paired=bool(props.get("Paired", False)),
        bonded=bool(props.get("Bonded", False)),
        trusted=bool(props.get("Trusted", False)),
        connected=bool(props.get("Connected", False)),
    )


async def _call(
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


async def _resolve_device(
    bus: BluezBus, mac: str, *, discovery_timeout: float, poll_s: float
) -> tuple[str, str]:
    """Geef (adapterpad, devicepad); start zo nodig een korte discovery."""
    objects = await bus.get_managed_objects()
    adapter = _find_adapter(objects)
    path = _find_device(objects, mac, adapter)
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
            path = _find_device(await bus.get_managed_objects(), mac, adapter)
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
) -> PairResult:
    """Koppel met `address`. `passkey_provider` levert de code van het Flipper-scherm.

    `timeout` geldt voor de Pair()-aanroep, dus inclusief de tijd dat de gebruiker de code
    intikt. Na afloop (ook bij een fout) is de agent altijd afgemeld.
    """
    mac = _mac(address)
    bus = await _connect(bus_factory)
    exported = False
    registered = False
    try:
        _adapter, device_path = await _resolve_device(
            bus, mac, discovery_timeout=min(discovery_timeout, timeout), poll_s=poll_s
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


async def unpair_async(address: str, *, bus_factory: BusFactory | None = None) -> bool:
    """Verwijder het apparaat en zijn bonding uit BlueZ (Adapter1.RemoveDevice).

    Geeft False terug als het apparaat niet bekend was (er viel niets te verwijderen).
    """
    mac = _mac(address)
    bus = await _connect(bus_factory)
    try:
        objects = await bus.get_managed_objects()
        path = _find_device(objects, mac)
        if path is None:
            return False
        adapter = str(objects[path][DEVICE_IFACE].get("Adapter") or _find_adapter(objects))
        await _call(bus, "Ontkoppelen", adapter, ADAPTER_IFACE, "RemoveDevice", "o", [path])
        return True
    finally:
        with contextlib.suppress(Exception):
            await bus.close()


async def list_devices_async(
    prefix: str = "", *, only_paired: bool = True, bus_factory: BusFactory | None = None
) -> list[PairedDevice]:
    """Door BlueZ gekende apparaten waarvan de naam met `prefix` begint (hoofdletterongevoelig)."""
    bus = await _connect(bus_factory)
    try:
        objects = await bus.get_managed_objects()
    finally:
        with contextlib.suppress(Exception):
            await bus.close()
    wanted = prefix.casefold()
    result = []
    for _path, props in _devices(objects):
        device = _as_paired_device(props)
        if only_paired and not (device.paired or device.bonded):
            continue
        if wanted and not (device.name or "").casefold().startswith(wanted):
            continue
        result.append(device)
    return result


async def paired_devices_async(
    prefix: str = "", *, bus_factory: BusFactory | None = None
) -> list[PairedDevice]:
    return await list_devices_async(prefix, only_paired=True, bus_factory=bus_factory)


# --- synchrone wrappers voor de CLI ------------------------------------------------------


def pair(
    address: str,
    passkey_provider: Callable[[], int],
    timeout: float = 90.0,
    *,
    bus_factory: BusFactory | None = None,
) -> PairResult:
    """Synchrone variant van `pair_async` (niet aanroepen vanuit een draaiende event loop)."""
    return asyncio.run(pair_async(address, passkey_provider, timeout, bus_factory=bus_factory))


def unpair(address: str, *, bus_factory: BusFactory | None = None) -> bool:
    return asyncio.run(unpair_async(address, bus_factory=bus_factory))


def list_devices(
    prefix: str = "", *, only_paired: bool = True, bus_factory: BusFactory | None = None
) -> list[PairedDevice]:
    return asyncio.run(list_devices_async(prefix, only_paired=only_paired, bus_factory=bus_factory))


def paired_devices(
    prefix: str = "", *, bus_factory: BusFactory | None = None
) -> list[PairedDevice]:
    return asyncio.run(paired_devices_async(prefix, bus_factory=bus_factory))
