"""De tijdelijke BlueZ-pairing-agent (org.bluez.Agent1): de logica en de dbus-fast-interface.

Waarom een eigen agent, welke methoden en waarom KeyboardOnly: zie _ble_pairing.py (bronnen
Agent.rst, AgentManager.rst en gap.c).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from pitoolbox.core.devices._bluez_dbus import AGENT_IFACE, ERR_CANCELED, ERR_REJECTED

# Loggernaam van _ble_pairing.py: alle logregels van het koppelen onder één naam.
log = logging.getLogger("pitoolbox.core.devices._ble_pairing")

T = TypeVar("T")


class AgentRejected(Exception):
    """Door de agent te weigeren aanvraag; wordt een D-Bus-fout met `error_name`."""

    def __init__(self, error_name: str, text: str) -> None:
        super().__init__(text)
        self.error_name = error_name
        self.text = text


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

    def sig(fn: Callable[..., Any], **annotations: str) -> Callable[..., Any]:
        fn.__annotations__ = dict(annotations)
        return fn

    dbus_method = service.dbus_method
    members: dict[str, Any] = {
        name: dbus_method(name=name)(sig(fn, **annotations))
        for name, (fn, annotations) in _agent_methods(agent, errors.DBusError).items()
    }
    cls = type("PitoolboxPairingAgent", (service.ServiceInterface,), members)
    return cls(AGENT_IFACE)


def _agent_methods(
    agent: PairingAgent, dbus_error: Any
) -> dict[str, tuple[Callable[..., Any], dict[str, str]]]:
    """De Agent1-methoden (met `self`, zoals dbus-fast ze aanroept) en hun D-Bus-signatuur.

    Een `AgentRejected` van de agent wordt een `dbus_error` met dezelfde foutnaam en tekst.
    """

    def rejected(exc: AgentRejected) -> Exception:
        return dbus_error(exc.error_name, exc.text)  # type: ignore[no-any-return]

    def forward(method: Callable[..., T], *args: Any) -> T:
        try:
            return method(*args)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    async def request_passkey(self: Any, device: str) -> int:
        try:
            return await agent.request_passkey(device)
        except AgentRejected as exc:
            raise rejected(exc) from exc

    def request_confirmation(self: Any, device: str, passkey: int) -> None:
        forward(agent.request_confirmation, device, passkey)

    def display_passkey(self: Any, device: str, passkey: int, entered: int) -> None:
        forward(agent.display_passkey, device, passkey, entered)

    def request_authorization(self: Any, device: str) -> None:
        forward(agent.request_authorization, device)

    def authorize_service(self: Any, device: str, uuid: str) -> None:
        forward(agent.authorize_service, device, uuid)

    def request_pin_code(self: Any, device: str) -> str:
        return forward(agent.request_pin_code, device)

    def display_pin_code(self: Any, device: str, pincode: str) -> None:
        forward(agent.display_pin_code, device, pincode)

    def cancel(self: Any) -> None:
        agent.cancel()

    def release(self: Any) -> None:
        agent.release()

    return {
        "RequestPasskey": (request_passkey, {"device": "o", "return": "u"}),
        "RequestConfirmation": (request_confirmation, {"device": "o", "passkey": "u"}),
        "DisplayPasskey": (display_passkey, {"device": "o", "passkey": "u", "entered": "q"}),
        "RequestAuthorization": (request_authorization, {"device": "o"}),
        "AuthorizeService": (authorize_service, {"device": "o", "uuid": "s"}),
        "RequestPinCode": (request_pin_code, {"device": "o", "return": "s"}),
        "DisplayPinCode": (display_pin_code, {"device": "o", "pincode": "s"}),
        "Cancel": (cancel, {}),
        "Release": (release, {}),
    }
