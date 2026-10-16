"""CLI/RPC-modewissel van `FlipperDevice`: de poort naar de CLI-prompt of in een RPC-sessie.

Bronnen (cli_shell_line.c, rpc_cli.c, rpc.c, cli_vcp.c, busse/flipperzero-mcp): zie flipper.py.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from pitoolbox.core.devices._flipper_link import FlipperLink
from pitoolbox.core.devices._flipper_rpc.client import RpcClient, RpcError
from pitoolbox.core.devices._flipper_rpc.framing import FrameError, encode_frame
from pitoolbox.core.devices._flipper_rpc.pb import flipper_pb2
from pitoolbox.core.devices._flipper_transport import TransportError
from pitoolbox.core.devices._flipper_types import AppLocked
from pitoolbox.core.errors import Aborted, DeviceError, DeviceTimeout, UnsupportedTransport

# Loggernaam van flipper.py: alle logregels van FlipperDevice onder één naam.
log = logging.getLogger("pitoolbox.core.devices.flipper")

_ETX = b"\x03"
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_GENERIC_PROMPT_SUFFIX = ">: "

_CLI_BLOCKED_BY_APP = (
    "De PiToolbox-app is open op de Flipper; Sub-GHz/NFC/RFID via de CLI kunnen niet tegelijk "
    "(de Flipper laat geen CLI-actie toe naast een open app). Sluit de app op de Flipper "
    "(Back in het hoofdmenu) en probeer het opnieuw."
)

_CLI_NEEDS_USB = (
    "Sub-GHz/NFC/RFID gaan via de Flipper-CLI en die bestaat alleen via USB; "
    "sluit de Flipper aan met USB"
)


class FlipperModes(FlipperLink):
    """Brengt de poort in CLI- of RPC-modus en leest ruw tot een prompt."""

    # --- ruwe leesprimitieven ---------------------------------------------------------

    def _read_until_prompt(
        self, suffix_ok: Callable[[str], bool], timeout: float, gen: int | None
    ) -> str | None:
        """Lees tot de laatste regel aan `suffix_ok` voldoet; None bij time-out."""
        transport = self._tx()
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            if gen is not None and self._abort_gen != gen:
                raise Aborted("afgebroken tijdens het wachten op de Flipper-prompt")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            chunk = transport.read(4096, min(self._timing.read_slice_s, remaining))
            if chunk:
                buf.extend(chunk)
                text = _clean(buf)
                if suffix_ok(_last_line(text)):
                    return text

    def _settle(self) -> None:
        """Wacht tot de poort even stil is (laatste echo's/prompts na Ctrl+C weglezen)."""
        transport = self._tx()
        end = time.monotonic() + self._timing.settle_max_s
        while time.monotonic() < end:
            if not transport.read(4096, self._timing.settle_quiet_s):
                return

    # --- moduswissel ------------------------------------------------------------------

    def _sync_cli(self, gen: int | None) -> None:
        """Breng de poort naar de hoofd-CLI-prompt, vanuit elke bekende of onbekende toestand."""
        transport = self._tx()
        timing = self._timing
        if self._maybe_rpc:
            # Een geldig stop_session-frame op een frame-grens sluit de sessie schoon; Ctrl+C
            # zou in de RPC-stroom als lengtebyte gelezen worden.
            self._blind_stop_session(gen)
            self._maybe_rpc = False
        for attempt in range(3):
            transport.reset_input()
            transport.write(_ETX)  # leegt een half getypte regel en toont de prompt opnieuw
            text = self._read_until_prompt(
                lambda line: line.endswith(_GENERIC_PROMPT_SUFFIX), timing.prompt_timeout_s, gen
            )
            if text is None:
                if attempt == 1:
                    self._blind_stop_session(gen)
                continue
            line = _last_line(text)
            if line != _GENERIC_PROMPT_SUFFIX:
                # Subshell (bijv. "[nfc]>: ") nog open: netjes verlaten.
                transport.write(b"exit\r")
                text = self._read_until_prompt(
                    lambda ln: ln == _GENERIC_PROMPT_SUFFIX, timing.prompt_timeout_s, gen
                )
                if text is None:
                    continue
            self._settle()
            self._mode = "cli"
            self._client = None
            self._app_active = False
            return
        self._mode = "unknown"
        raise DeviceTimeout("Flipper-CLI reageert niet (geen prompt na Ctrl+C)")

    def _blind_stop_session(self, gen: int | None) -> None:
        """Poort reageert niet op CLI-tekst: mogelijk staat er nog een RPC-sessie open."""
        transport = self._tx()
        main = flipper_pb2.Main()
        main.command_id = 1
        main.stop_session.SetInParent()
        transport.reset_input()
        transport.write(encode_frame(main.SerializeToString()))
        self._read_until_prompt(
            lambda line: line == _GENERIC_PROMPT_SUFFIX, self._timing.prompt_timeout_s, gen
        )

    def _ensure_cli(self, gen: int | None) -> None:
        if self._tx().starts_in_rpc:
            raise UnsupportedTransport(_CLI_NEEDS_USB)
        if self._rpc_app_state != "off":
            raise AppLocked(_CLI_BLOCKED_BY_APP)
        if self._mode == "cli":
            return
        if self._mode == "rpc" and self._client is not None and self._leave_rpc(gen):
            return
        self._sync_cli(gen)

    def _leave_rpc(self, gen: int | None) -> bool:
        """RPC -> CLI via stop_session (rpc.c); True als de CLI-prompt terug is."""
        client = self._client
        if client is None:
            return False
        self._mode = "unknown"  # pas "cli" als de prompt echt terug is
        text: str | None = None
        try:
            if self._app_active:
                self._exit_app(client, timeout=self._timing.app_exit_timeout_s)
            main = flipper_pb2.Main()
            main.stop_session.SetInParent()
            client.send(main)
            # Het OK-antwoord en de nieuwe prompt komen achter elkaar binnen; daarom lezen we
            # ruw door tot de prompt in plaats van het antwoord via de client te consumeren.
            text = self._read_until_prompt(
                lambda line: line == _GENERIC_PROMPT_SUFFIX, self._timing.prompt_timeout_s, gen
            )
        except TransportError:
            raise
        except DeviceError as exc:
            log.debug("stop_session mislukte: %s", exc)
        self._client = None
        if text is None:
            return False
        self._settle()
        self._mode = "cli"
        return True

    def _ensure_rpc(self, gen: int | None) -> RpcClient:
        if self._mode == "rpc" and self._client is not None:
            return self._client
        if self._tx().starts_in_rpc:
            return self._enter_native_rpc()
        self._sync_cli(gen)
        transport = self._tx()
        transport.reset_input()
        self._mode = "unknown"  # vanaf hier kan de poort in RPC staan; pas na de probe "rpc"
        self._maybe_rpc = True
        # CR alleen, geen CRLF: de '\n' zou als eerste varint-byte worden gelezen (busse/…).
        transport.write(b"start_rpc_session\r")
        self._wait_echo(b"start_rpc_session", gen)
        transport.reset_input()
        timing = self._timing
        for _ in range(timing.probe_attempts):
            client = self._new_client(transport)
            ping = flipper_pb2.Main()
            ping.system_ping_request.data = b"pitoolbox"
            try:
                reply = client.request(
                    ping, timing.probe_timeout_s, cancel=self._cancel_or_none(gen)
                )
            except (DeviceTimeout, FrameError, RpcError) as exc:
                log.debug("RPC-probe mislukt: %s", exc)
                transport.reset_input()
                continue
            if reply[0].system_ping_response.data == b"pitoolbox":
                self._client = client
                self._mode = "rpc"
                return client
        self._mode = "unknown"
        raise DeviceError("De RPC-sessie op de Flipper startte niet (geen ping-antwoord)")

    def _wait_echo(self, needle: bytes, gen: int | None) -> None:
        """Wacht (best effort) tot de CLI de getypte regel heeft teruggegeven."""
        transport = self._tx()
        buf = bytearray()
        deadline = time.monotonic() + self._timing.prompt_timeout_s
        while time.monotonic() < deadline:
            if gen is not None and self._abort_gen != gen:
                raise Aborted("afgebroken tijdens het starten van de RPC-sessie")
            chunk = transport.read(4096, self._timing.read_slice_s)
            if chunk:
                buf.extend(chunk)
                if needle + b"\r\n" in bytes(buf) or needle + b"\n" in bytes(buf):
                    return


def _clean(raw: bytes | bytearray) -> str:
    """Bytes naar tekst: ANSI weg, CRLF/CR naar LF."""
    text = _ANSI_RE.sub("", bytes(raw).decode("utf-8", errors="replace"))
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _last_line(text: str) -> str:
    return text.rsplit("\n", 1)[-1]
