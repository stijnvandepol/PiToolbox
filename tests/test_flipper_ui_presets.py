"""Presets en instellingen voor de Flipper-bridge."""

from __future__ import annotations

from pathlib import Path

import pytest

from pitoolbox.config import Settings
from pitoolbox.frontends.flipper_ui.presets import (
    MAX_PROMPT,
    PresetsError,
    load_presets,
    parse_presets,
)


def test_missing_file_means_no_presets(tmp_path: Path) -> None:
    assert load_presets(tmp_path / "bestaat-niet.yaml") == ()


def test_valid_presets(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text(
        "presets:\n"
        "  - id: 1\n    label: Wifi kort\n    prompt: Doe een wifi-scan.\n"
        "  - id: 200\n    label: Status\n    prompt: Hoe staat het ervoor?\n",
        encoding="utf-8",
    )
    presets = load_presets(path)
    assert [(p.id, p.label) for p in presets] == [(1, "Wifi kort"), (200, "Status")]
    assert presets[0].prompt == "Doe een wifi-scan."


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"presets": [{"id": 0, "label": "a", "prompt": "p"}]}, "presets.0.id"),
        ({"presets": [{"id": 1, "label": "é", "prompt": "p"}]}, "ASCII"),
        ({"presets": [{"id": 1, "label": "x" * 25, "prompt": "p"}]}, "label"),
        ({"presets": [{"id": 1, "label": "a", "prompt": ""}]}, "prompt"),
        ({"presets": [{"id": 1, "label": "a", "prompt": "p" * (MAX_PROMPT + 1)}]}, "prompt"),
        ({"presets": [{"id": 1, "label": "a", "prompt": "p", "tool": "x"}]}, "tool"),
        (
            {
                "presets": [
                    {"id": 1, "label": "a", "prompt": "p"},
                    {"id": 1, "label": "b", "prompt": "q"},
                ]
            },
            "uniek",
        ),
        ({"presets": [{"id": i, "label": "a", "prompt": "p"} for i in range(1, 14)]}, "presets"),
        (["geen mapping"], "mapping"),
    ],
)
def test_invalid_presets(data: object, fragment: str) -> None:
    with pytest.raises(PresetsError, match=fragment):
        parse_presets(data)


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("presets: [\n", encoding="utf-8")
    with pytest.raises(PresetsError, match="YAML"):
        load_presets(path)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PITOOLBOX_FLIPPER_UI", raising=False)
    monkeypatch.delenv("PITOOLBOX_FLIPPER_UI_APP", raising=False)
    defaults = Settings.from_env()
    assert defaults.flipper_ui == "off"
    assert defaults.flipper_ui_app == "/ext/apps/Tools/pitoolbox_ui.fap"
    monkeypatch.setenv("PITOOLBOX_FLIPPER_UI", "ON")
    monkeypatch.setenv("PITOOLBOX_FLIPPER_UI_APP", "/ext/apps/Misc/ptb.fap")
    monkeypatch.setenv("PITOOLBOX_FLIPPER_UI_PRESETS", str(tmp_path / "x.yaml"))
    settings = Settings.from_env()
    assert settings.flipper_ui == "on"
    assert settings.flipper_ui_app == "/ext/apps/Misc/ptb.fap"
    assert settings.flipper_ui_presets == tmp_path / "x.yaml"


@pytest.mark.parametrize("path", ["/ext/apps/../evil.fap", "/int/x.fap", "Infrared", "/ext/x.fap"])
def test_settings_reject_odd_app_path(path: str) -> None:
    with pytest.raises(ValueError, match="FLIPPER_UI_APP"):
        Settings(flipper_ui_app=path)
