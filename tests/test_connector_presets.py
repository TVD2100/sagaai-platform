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

import core.paths
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


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp directory for tests that create a connection.

    ``instantiate_preset`` resolves the preset catalogue through
    ``preset_root`` but the resulting connection is written through
    ``core.connectors`` -> ``core.paths.DATA_DIR``. Without this override a
    test run would leave real connection folders in the installation.
    """
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    yield tmp_path


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


def test_instantiate_hidden_preset_still_works(preset_root, isolated_data_dir):
    made = connector_presets.instantiate_preset(
        "hidden_one", "mcp", preset_root=preset_root)
    assert made["preset"] == "hidden_one"
    assert made["connection"]["config"]["preset"] == "hidden_one"


def test_bundled_github_preset_is_hidden():
    """The bundled catalogue offers the public servers; GitHub MCP stays resolvable."""
    ids = [p["id"] for p in connector_presets.list_presets("mcp")]
    assert "vkusvill" in ids
    assert "yandex_mail" in ids
    assert "github_mcp" not in ids
    github = connector_presets.resolve_preset("github_mcp", "mcp")
    assert github is not None
    assert github["hidden"] is True


def test_bundled_yandex_mail_preset():
    """The mail preset carries the OAuth scheme and exactly one credential field."""
    preset = connector_presets.resolve_preset("yandex_mail", "mcp")
    assert preset is not None
    assert preset["hidden"] is False
    config = preset["config"]
    assert config["transport"] == "http"
    assert config["url"] == "https://mcp.360.yandex.net/v1/mail/mcp"
    # The server accepts the OAuth scheme only; a Bearer header is rejected.
    assert config["auth_header"] == "OAuth {YANDEX_MAIL_OAUTH_TOKEN}"
    fields = config["secret_env_fields"]
    assert list(fields) == ["token"]
    assert fields["token"]["env"] == "YANDEX_MAIL_OAUTH_TOKEN"
    assert fields["token"]["password"] is True
    # Drafting and sending are different things: the preset must say so.
    assert "Отправка писем не поддерживается" in preset["description"]
    assert "mail.read_message" in preset["memory_seed"]


# ─── User-level presets (installed from the marketplace) ──────────────────

@pytest.fixture()
def user_root(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp dir and return the user preset root."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(core.paths, "DATA_DIR", str(data_dir))
    return data_dir / "connector_presets" / "mcp"


class TestUserPresets:
    def test_the_catalogue_merges_both_roots(self, preset_root, user_root):
        connector_presets.save_preset(
            "from_catalog",
            {
                "name": "From catalog",
                "description": "Installed by the marketplace.",
                "icon": "C",
                "sort_order": 5,
                "config": dict(_BASE_CONFIG),
            },
        )

        ids = [
            p["id"] for p in connector_presets.list_presets(
                "mcp", preset_root=None)
        ]
        assert "from_catalog" in ids
        # A dead user root must not hide the bundled catalogue.
        assert "vkusvill" in ids

    def test_a_user_preset_shadows_the_bundled_one_of_the_same_id(
        self, preset_root, user_root
    ):
        _write_preset(str(preset_root), "shared", sort_order=10)
        connector_presets.save_preset(
            "shared",
            {
                "name": "Shadowing",
                "description": "Installed over a bundled template.",
                "icon": "S",
                "sort_order": 10,
                "config": dict(_BASE_CONFIG),
            },
        )

        resolved = connector_presets.resolve_preset("shared", "mcp")
        assert resolved is not None
        assert resolved["name"] == "Shadowing"
        assert resolved["origin"] == connector_presets.ORIGIN_USER
        # The bundled template is still on disk and comes back on delete.
        assert connector_presets.bundled_preset(
            "shared", "mcp", preset_root=str(preset_root))["name"] == "Test preset"
        assert connector_presets.delete_preset("shared", "mcp") is True
        restored = connector_presets.resolve_preset(
            "shared", "mcp", preset_root=str(preset_root))
        assert restored["name"] == "Test preset"
        assert restored["origin"] == connector_presets.ORIGIN_BUNDLED

    def test_save_and_delete_round_trip(self, user_root):
        saved = connector_presets.save_preset(
            "round_trip",
            {
                "name": "Round trip",
                "description": "Stored under DATA_DIR.",
                "icon": "R",
                "sort_order": 42,
                "config": dict(_BASE_CONFIG),
                "memory_seed": "A note.",
            },
        )
        assert saved["origin"] == connector_presets.ORIGIN_USER
        path = user_root / "round_trip.json"
        assert path.is_file()

        loaded = connector_presets.user_preset("round_trip", "mcp")
        assert loaded["sort_order"] == 42
        assert loaded["memory_seed"] == "A note."
        assert connector_presets.preset_origin("round_trip", "mcp") == (
            connector_presets.ORIGIN_USER)

        assert connector_presets.delete_preset("round_trip", "mcp") is True
        assert not path.exists()
        assert connector_presets.delete_preset("round_trip", "mcp") is False
        assert connector_presets.preset_origin("round_trip", "mcp") == ""

    def test_a_pinned_root_never_sees_the_user_catalogue(
        self, preset_root, user_root
    ):
        connector_presets.save_preset(
            "only_user",
            {
                "name": "Only user",
                "description": "Not part of a pinned root.",
                "icon": "U",
                "sort_order": 10,
                "config": dict(_BASE_CONFIG),
            },
        )

        ids = [
            p["id"] for p in connector_presets.list_presets(
                "mcp", preset_root=preset_root)
        ]
        assert "only_user" not in ids
        assert connector_presets.resolve_preset(
            "only_user", "mcp", preset_root=preset_root) is None

    def test_save_refuses_a_preset_the_connector_layer_rejects(self, user_root):
        with pytest.raises(ValueError):
            connector_presets.save_preset(
                "broken",
                {
                    "name": "Broken",
                    "description": "An http preset without a URL.",
                    "icon": "B",
                    "sort_order": 10,
                    "config": {"transport": "http"},
                },
            )
        assert not (user_root / "broken.json").exists()

    def test_save_refuses_an_unusable_id(self, user_root):
        with pytest.raises(ValueError):
            connector_presets.save_preset(
                "../evil",
                {
                    "name": "Evil",
                    "description": "Path traversal in the id.",
                    "icon": "E",
                    "sort_order": 10,
                    "config": dict(_BASE_CONFIG),
                },
            )
