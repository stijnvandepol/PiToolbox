"""Sub-GHz, RFID en NFC via de Flipper-CLI (alleen USB), met firmwareprofiel en device-info.

De vaste commando's en parsers per firmwareprofiel staan in `_flipper_cli`; hier zit de
uitvoering (Ctrl+C na een duur, abort, de NFC-subshell). Bronnen: zie flipper.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pitoolbox.core.devices._flipper_cli import CliProfile, select_profile
from pitoolbox.core.devices._flipper_modes import (
    _CLI_BLOCKED_BY_APP,
    _CLI_NEEDS_USB,
    _ETX,
    FlipperModes,
    _clean,
    _last_line,
)
from pitoolbox.core.devices._flipper_rpc.pb import flipper_pb2
from pitoolbox.core.devices._flipper_types import AppLocked, _Attempt
from pitoolbox.core.errors import Aborted, DeviceError, DeviceTimeout, UnsupportedTransport

# Loggernaam van flipper.py: alle logregels van FlipperDevice onder één naam.
log = logging.getLogger("pitoolbox.core.devices.flipper")

_MAX_CLI_BYTES = 1_000_000
_MAX_RAW_CHARS = 16_000


class FlipperCliActions(FlipperModes):
    """CLI-acties van `FlipperDevice`; elke actie wisselt zo nodig eerst naar de CLI."""

    def subghz_rx(self, frequency_hz: int, duration_s: float) -> dict[str, Any]:
        """Luister `duration_s` seconden op `frequency_hz` met de interne CC1101."""

        def op(gen: int, attempt: _Attempt) -> dict[str, Any]:
            self._require_cli_transport()
            profile = self._get_profile(gen)
            command = profile.subghz_rx_command(frequency_hz)  # valideert de frequentie
            self._ensure_cli(gen)
            body = self._cli_exec(
                gen,
                command,
                prompts=(profile.main_prompt,),
                stop_after_s=duration_s,
            )
            parsed = profile.parse_subghz_rx(body)
            return {
                "frequency_hz": frequency_hz,
                "tuned_frequency_hz": parsed.tuned_frequency_hz,
                "duration_s": duration_s,
                "packets": parsed.packets,
                "packet_count": parsed.packet_count,
                "raw": body[:_MAX_RAW_CHARS],
            }

        return self._run("subghz_rx", op)

    def rfid_read(self, timeout_s: float) -> dict[str, Any]:
        """Lees een 125 kHz-tag (stopt zodra er een tag is of na `timeout_s`)."""

        def op(gen: int, attempt: _Attempt) -> dict[str, Any]:
            self._require_cli_transport()
            profile = self._get_profile(gen)
            self._ensure_cli(gen)
            body = self._cli_exec(
                gen,
                profile.rfid_read_command,
                prompts=(profile.main_prompt,),
                stop_after_s=timeout_s,
            )
            parsed = profile.parse_rfid_read(body)
            return {
                "found": parsed.found,
                "protocol": parsed.protocol,
                "data_hex": parsed.data_hex,
                "info": parsed.info,
                "raw": body[:_MAX_RAW_CHARS],
            }

        return self._run("rfid_read", op)

    def nfc_scan(self, timeout_s: float) -> dict[str, Any]:
        """Detecteer een NFC-tag via de NFC-subshell (`nfc` -> `scanner -t` -> `exit`)."""

        def op(gen: int, attempt: _Attempt) -> dict[str, Any]:
            self._require_cli_transport()
            profile = self._get_profile(gen)
            self._ensure_cli(gen)
            entered = self._cli_exec(
                gen,
                profile.nfc_enter_command,
                prompts=(profile.nfc_prompt, profile.main_prompt),
                keep_prompt=True,
            )
            if _last_line(entered) != profile.nfc_prompt:
                if profile.nfc_busy_marker in entered:
                    raise DeviceError(
                        "De NFC-app draait op de Flipper; sluit die eerst (de NFC-CLI start "
                        "niet naast de NFC-app)."
                    )
                raise DeviceError(
                    "De NFC-subshell startte niet; ondersteunt deze firmware 'nfc' in de CLI? "
                    f"({_first_nonblank(entered)[:100]!r})"
                )
            try:
                body = self._cli_exec(
                    gen,
                    profile.nfc_scan_command,
                    prompts=(profile.nfc_prompt,),
                    stop_after_s=timeout_s,
                )
            finally:
                self._leave_nfc_shell(profile)
            parsed = profile.parse_nfc_scanner(body)
            return {
                "found": parsed.found,
                "protocols": parsed.protocols,
                "raw": body[:_MAX_RAW_CHARS],
            }

        return self._run("nfc_scan", op)

    def _require_cli_transport(self) -> None:
        """CLI-acties: op een transport zonder CLI (BLE) eerst naar USB wisselen of weigeren."""
        if self._rpc_app_state != "off":
            raise AppLocked(_CLI_BLOCKED_BY_APP)
        if not self._tx().starts_in_rpc:
            return
        if self._usb_probe is not None and self._usb_probe():
            log.info("CLI-actie gevraagd over BLE; overschakelen naar USB")
            self._drop_connection()
            self._ensure_open()  # de factory kiest nu USB
            if not self._tx().starts_in_rpc:
                return
        raise UnsupportedTransport(_CLI_NEEDS_USB)

    # --- CLI-uitvoering ---------------------------------------------------------------

    def _cli_exec(
        self,
        gen: int | None,
        command: str,
        *,
        prompts: tuple[str, ...],
        stop_after_s: float | None = None,
        keep_prompt: bool = False,
    ) -> str:
        """Stuur één vaste CLI-regel en lees tot een van `prompts` terug is.

        Met `stop_after_s` wordt na die tijd Ctrl+C gestuurd (voor commando's die tot Ctrl+C
        doorlopen, zoals `subghz rx`); daarna krijgt het commando `stop_grace_s` om af te
        ronden. `keep_prompt` laat de slotprompt in de tekst staan.
        """
        transport = self._tx()
        timing = self._timing
        transport.write(command.encode("ascii") + b"\r")
        buf = bytearray()
        start = time.monotonic()
        etx_sent = False
        limit = (
            stop_after_s + timing.stop_grace_s
            if stop_after_s is not None
            else timing.command_timeout_s
        )
        while True:
            if gen is not None and self._abort_gen != gen:
                self._interrupt(prompts)
                raise Aborted(f"'{command}' afgebroken")
            elapsed = time.monotonic() - start
            if stop_after_s is not None and not etx_sent and elapsed >= stop_after_s:
                transport.write(_ETX)
                etx_sent = True
            if elapsed >= limit:
                if not etx_sent:
                    transport.write(_ETX)
                self._mode = "unknown"
                raise DeviceTimeout(f"Flipper-commando '{command}' kwam niet terug op de prompt")
            chunk = transport.read(4096, timing.read_slice_s)
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > _MAX_CLI_BYTES:
                transport.write(_ETX)
                self._mode = "unknown"
                raise DeviceError(f"te veel uitvoer van '{command}'; afgebroken")
            text = _clean(buf)
            if _last_line(text) in prompts:
                if etx_sent:
                    self._settle()
                body = _strip_echo(text, command)
                return body if keep_prompt else _strip_prompt(body, prompts)

    def _interrupt(self, prompts: tuple[str, ...]) -> None:
        """Ctrl+C sturen en op de prompt wachten (best effort, na een abort)."""
        transport = self._tx()
        transport.write(_ETX)
        text = self._read_until_prompt(
            lambda line: line in prompts, self._timing.prompt_timeout_s, None
        )
        if text is None:
            self._mode = "unknown"
        else:
            self._settle()

    def _leave_nfc_shell(self, profile: CliProfile) -> None:
        """Verlaat de NFC-subshell; zonder abort-controle zodat opruimen altijd doorgaat."""
        try:
            self._cli_exec(
                None,
                profile.nfc_exit_command,
                prompts=(profile.main_prompt,),
            )
        except DeviceError as exc:
            log.warning("NFC-subshell verlaten mislukte: %s", exc)
            self._mode = "unknown"

    # --- profiel en device-info -------------------------------------------------------

    def _fetch_device_info(self, gen: int | None) -> dict[str, str]:
        client = self._ensure_rpc(gen)
        main = flipper_pb2.Main()
        main.system_device_info_request.SetInParent()
        frames = client.request(main, self._timing.rpc_timeout_s, cancel=self._cancel_or_none(gen))
        info: dict[str, str] = {}
        for frame in frames:
            if frame.WhichOneof("content") == "system_device_info_response":
                resp = frame.system_device_info_response
                if resp.key:
                    info[str(resp.key)] = str(resp.value)
        self._device_info = info
        return info

    def _get_profile(self, gen: int | None) -> CliProfile:
        if self._profile is not None:
            return self._profile
        if self._profile_setting.strip().lower() in ("", "auto"):
            info = (
                self._device_info if self._device_info is not None else self._fetch_device_info(gen)
            )
            self._profile = select_profile(self._profile_setting, info)
        else:
            self._profile = select_profile(self._profile_setting, None)
        return self._profile


def _strip_echo(text: str, command: str) -> str:
    """Haal de door de CLI teruggegeven commandoregel (echo) uit de tekst."""
    idx = text.find(command)
    if idx < 0:
        return text
    newline = text.find("\n", idx)
    return text[newline + 1 :] if newline >= 0 else ""


def _strip_prompt(text: str, prompts: tuple[str, ...]) -> str:
    last = _last_line(text)
    return text[: len(text) - len(last)] if last in prompts else text


def _first_nonblank(text: str) -> str:
    for line in text.split("\n"):
        if line.strip():
            return line.strip()
    return ""
