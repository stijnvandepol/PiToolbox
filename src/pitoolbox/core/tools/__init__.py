"""Toolregistry: verzamelt TOOLS uit de tool-modules plus confirm_action."""

from __future__ import annotations

from pitoolbox.core.tools.base import Registry


def build_registry() -> Registry:
    from pitoolbox.core.tools import flipper_tools, system_tools, wifi_tools
    from pitoolbox.core.tools.runner import CONFIRM_SPEC

    return Registry([*flipper_tools.TOOLS, *wifi_tools.TOOLS, *system_tools.TOOLS, CONFIRM_SPEC])
