"""Flipper als bediening (fase 4): de FAP `pitoolbox_ui` en de Pi.

`protocol`: codec en bouwstenen (docs/fase4-protocol.md). `bridge`: de Pi-kant die FAP-
berichten via de ToolRunner en de agent-loop afhandelt (`PITOOLBOX_FLIPPER_UI=on`).
`presets`: vaste prompts uit de Pi-config. `protocol` en `bridge` zijn de ingangen; hun delen
staan in `protocol_*.py` en `bridge_*.py` (zie de module-docstring van elk).
"""
