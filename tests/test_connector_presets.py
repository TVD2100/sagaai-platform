# -*- coding: utf-8 -*-
"""
Tests for core.connector_presets: catalogue, validation and visibility.

A preset marked ``hidden`` must disappear from the tile catalogue
(``list_presets``) while staying resolvable by id, so already created
connections keep their preset fields and the preset can be re-enabled by
clearing the flag.
"""
import json
import os

import pytest

from core import connector_presets


_BASE_CONFIG = {
    "transport": "http",
    "url": "https://example.test/mcp",
    "auth_header": "Bearer {TEST_TOKEN}",
    "secret_env_fields": {
        "token": {
            "env": "TEST_TOKEN",
            "label": "Token",
            "help": "Test token.",
            "password": True,
        },
    },
}


def _write_preset(root, preset_id, **overrides):
    """Write one preset JSON file under ``root/mcp``."""
    data = {
        "name": "Test preset",
        "description": "Test preset description.",
        "icon": "T",
        "sort_order": 10,
        "config": dict(_BASE_CONFIG),
    }
    data.update(overrides)
    os.makedirs(os.path.join(root, "mcp"), exist_ok=True)
    path = os.path.join(root, "mcp", preset_id + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


@pytest.fixture()
def preset_root(tmp_path):
    """Presets root with a visible and a hidden preset."""
    root = str(tmp_path / "presets")
    _write_preset(root, "visible", sort_order=10)
    _write_preset(root, "hidden_one", sort_order=20, hidden=True)
    return root


def test_list_presets_hides_hidden(preset_root):
    ids = [p["id"] for p in connector_presets.list_presets(
        "mcp", preset_root=preset_root)]
    assert ids == ["visible"]


def test_list_presets_include_hidden(preset_root):
    ids = [p["id"] for p in connector_presets.list_presets(
        "mcp", preset_root=preset_root, include_hidden=True)]
    assert ids == ["visible", "hidden_one"]


def test_hidden_preset_is_resolvable(preset_root):
    preset = connector_presets.resolve_preset(
        "hidden_one", "mcp", preset_root=preset_root)
    assert preset is not None
    assert preset["hidden"] is True
    assert preset["config"]["url"] == "https://example.test/mcp"


def test_visible_preset_reports_hidden_false(preset_root):
    preset = connector_presets.resolve_preset(
        "visible", "mcp", preset_root=preset_root)
    assert preset["hidden"] is False


@pytest.mark.parametrize("value,expected", [
    (True, True),
    (False, False),
    ("true", True),
    (" TRUE ", True),
    ("yes", True),
    (1, True),
    (0, False),
    ("no", False),
    (None, False),
    ({"unexpected": 1}, False),
])
def test_hidden_flag_coercion(tmp_path, value, expected):
    root = str(tmp_path / "presets")
    _write_preset(root, "candidate", hidden=value)
    preset = connector_presets.resolve_preset(
        "candidate", "mcp", preset_root=root)
    assert preset is not None
    assert preset["hidden"] is expected


def test_invalid_hidden_does_not_break_load(tmp_path):
    root = str(tmp_path / "presets")
    _write_preset(root, "broken_flag", hidden=[1, 2, 3])
    assert connector_presets.load_preset(
        "broken_flag", "mcp", preset_root=root) is not None
    ids = [p["id"] for p in connector_presets.list_presets(
        "mcp", preset_root=root)]
    assert ids == ["broken_flag"]


def test_instantiate_hidden_preset_still_works(preset_root):
    made = connector_presets.instantiate_preset(
        "hidden_one", "mcp", preset_root=preset_root)
    assert made["preset"] == "hidden_one"
    assert made["connection"]["config"]["preset"] == "hidden_one"


def test_bundled_github_preset_is_hidden():
    """The bundled catalogue offers Vkusvill only; GitHub MCP stays resolvable."""
    ids = [p["id"] for p in connector_presets.list_presets("mcp")]
    assert "vkusvill" in ids
    assert "github_mcp" not in ids
    github = connector_presets.resolve_preset("github_mcp", "mcp")
    assert github is not None
    assert github["hidden"] is True
