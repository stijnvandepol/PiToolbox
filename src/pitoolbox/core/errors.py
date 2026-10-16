"""Foutklassen die tools naar een nette, gestructureerde fout vertalen."""

from __future__ import annotations


class ToolboxError(Exception):
    """Basis voor verwachte fouten. `code` komt in het toolresultaat en in de audit."""

    code = "toolbox_error"


class DeviceError(ToolboxError):
    code = "device_error"


class DeviceNotConnected(DeviceError):
    code = "device_not_connected"


class DeviceTimeout(DeviceError):
    code = "device_timeout"


class Aborted(ToolboxError):
    """Actie afgebroken door emergency_stop."""

    code = "aborted"


class UnsupportedFirmware(DeviceError):
    code = "unsupported_firmware"


class UnsupportedTransport(DeviceError):
    """Actie kan niet over de huidige verbinding (bijv. CLI-tools over BLE: alleen RPC)."""

    code = "unsupported_transport"


class InvalidState(ToolboxError):
    """Bijv. scan in managed mode gevraagd terwijl de adapter in monitor staat."""

    code = "invalid_state"


class CommandFailed(ToolboxError):
    code = "command_failed"
