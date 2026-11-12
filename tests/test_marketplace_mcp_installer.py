"""
test_marketplace_mcp_installer.py - installing an ``mcp`` entity.

An MCP entity is a connector preset: it ships no files and it creates no
connection. Installing it makes the server appear as a tile on the MCP
servers tab of the Connectors page, and the user creates as many
connections from that tile as the accounts they keep.

These tests drive the real installer against a temporary DATA_DIR and pin
what is easy to get wrong: the preset never carries a credential, a second
install changes nothing, a re-install of a catalog update rewrites the
stored preset in place, a server the platform already bundles installs as
a no-op, and a preset whose server cannot speak the platform's MCP revision
is refused before anything is written.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)

#: Preset of a remote server - the shape the catalog publishes.
PRESET = {
    "name": {"en": "Demo MCP", "ru": "Демо MCP"},
    "description": {"en": "A remote demo server", "ru": "Удалённый демо-сервер"},
    "icon": "\U0001f9ea",
    "protocol_version": "2025-03-26",
    "secret_env_fields": {
        "token": {"env": "DEMO_TOKEN", "label": "Token", "password": True}
    },
    "config": {
        "transport": "http",
        "url": "https://example.test/mcp/",
        "auth_header": "Bearer {DEMO_TOKEN}",
    },
    "memory_seed": "Endpoint: https://example.test/mcp/",
}


def _mcp_entity(tmp_path, version="1.0.0", *, preset=None, extra=None,
                entity_id="demo_mcp", preset_id="demo_mcp"):
    """Return ``(entity, package)`` of one MCP entity (no package files)."""
    block = dict(PRESET)
    block.update(preset or {})
    payload = {"preset": block, "preset_id": preset_id, "service": "mcp"}
    payload.update(extra or {})
    return make_entity(
        entity_id=entity_id,
        version=version,
        type_="mcp",
        files={},
        extra=payload,
    )


def _settings(monkeypatch, **overrides):
    """Patch the marketplace settings for one test."""
    from core.marketplace import config as marketplace_config

    values = {
        "enabled": True,
        "base_url": "",
        "allow_code": True,
        "require_signature": False,
        "channel": "main",
    }
    values.update(overrides)
    monkeypatch.setattr(marketplace_config, "marketplace_settings", lambda: dict(values))
    return values


def _install(entity, *, base_url="", **kwargs):
    """Install one MCP entity through the real installer."""
    from core.marketplace.mcp_installer import install_mcp
    from core.marketplace.source import MarketplaceSource

    return install_mcp(
        entity,
        channel="main",
        source=MarketplaceSource(base_url) if base_url else None,
        **kwargs,
    )


def _connections():
    """Return the live connector manifests of the current test."""
    from core import connectors

    return connectors.list_connections()


def _user_preset_file(tmp_path: Path, preset_id: str = "demo_mcp") -> Path:
    """Return the path the installer must write the preset to."""
    return tmp_path / "connector_presets" / "mcp" / f"{preset_id}.json"


# ─── Creation ─────────────────────────────────────────────────────────────

class TestCreate:
    def test_it_stores_a_preset_and_creates_no_connection(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        report = _install(entity)

        assert report["ok"] is True
        assert report["preset"] == "demo_mcp"
        assert report["origin"] == "user"
        assert report["stored"] is True
        # The whole point: nothing lands on the Connections tab.
        assert _connections() == []

        stored = json.loads(_user_preset_file(marketplace_data_dir).read_text(
            encoding="utf-8"))
        # The tile name comes from the preset block (the server), not from
        # the card title the fixture derives from the entity id.
        assert stored["name"] in ("Demo MCP", "Демо MCP")
        assert stored["config"]["url"] == "https://example.test/mcp/"
        assert stored["config"]["auth_header"] == "Bearer {DEMO_TOKEN}"
        assert stored["memory_seed"] == PRESET["memory_seed"]

        row = registry.get_installed("mcp", "demo_mcp")
        assert row is not None
        assert row["runtime_slug"] == "demo_mcp"
        assert row["version"] == "1.0.0"

    def test_the_installed_preset_becomes_a_tile(self, marketplace_data_dir, monkeypatch):
        from core import connector_presets

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        _install(entity)

        tiles = connector_presets.list_presets("mcp")
        ids = [tile["id"] for tile in tiles]
        assert "demo_mcp" in ids
        tile = next(tile for tile in tiles if tile["id"] == "demo_mcp")
        assert tile["origin"] == connector_presets.ORIGIN_USER
        assert tile["icon"] == "\U0001f9ea"
        assert tile["config"]["url"] == "https://example.test/mcp/"

    def test_a_stdio_preset_is_stored_with_its_args(
        self, marketplace_data_dir, monkeypatch
    ):
        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir,
            preset={
                "config": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                },
                "secret_env_fields": {},
            },
        )
        report = _install(entity)

        assert report["ok"] is True
        assert _connections() == []
        stored = json.loads(_user_preset_file(marketplace_data_dir).read_text(
            encoding="utf-8"))
        assert stored["config"]["transport"] == "stdio"
        assert stored["config"]["command"] == "npx"
        assert stored["config"]["args"][0] == "-y"

    def test_a_server_the_platform_bundles_installs_as_a_noop(
        self, marketplace_data_dir, monkeypatch
    ):
        """The tile is already shipped; a user copy would only shadow it."""
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir,
            preset={
                "name": {"en": "Vkusvill", "ru": "Вкусвилл"},
                "config": {
                    "transport": "http",
                    "url": "https://mcp.vkusvill.ru/mcp",
                    "auth_header": "Bearer {VKUSVILL_TOKEN}",
                },
                "secret_env_fields": {
                    "token": {"env": "VKUSVILL_TOKEN", "label": "Token"},
                },
            },
            entity_id="vkusvill",
            preset_id="vkusvill",
        )
        report = _install(entity)

        assert report["ok"] is True
        assert report["origin"] == "bundled"
        assert report["stored"] is False
        assert not _user_preset_file(marketplace_data_dir, "vkusvill").exists()
        assert registry.get_installed("mcp", "vkusvill")["runtime_slug"] == "vkusvill"
        assert _connections() == []


# ─── Identity ─────────────────────────────────────────────────────────────

class TestIdentity:
    def test_a_reinstall_of_the_same_version_is_a_noop(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        first = _install(entity)
        written = _user_preset_file(marketplace_data_dir).read_text(encoding="utf-8")
        second = _install(entity)

        assert first["noop"] is False
        assert second["noop"] is True
        # A no-op run leaves both the stored preset and the row untouched.
        assert _user_preset_file(marketplace_data_dir).read_text(
            encoding="utf-8") == written
        assert registry.get_installed("mcp", "demo_mcp")["install_id"] == first["install_id"]
        assert _connections() == []

    def test_a_changed_endpoint_rewrites_the_stored_preset(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        first = _install(entity)

        updated, _package = _mcp_entity(
            marketplace_data_dir,
            "1.1.0",
            preset={"config": {
                "transport": "http",
                "url": "https://example.test/mcp/v2/",
                "auth_header": "Bearer {DEMO_TOKEN}",
                "secret_env_fields": PRESET["secret_env_fields"],
            }},
        )
        report = _install(updated)

        assert report["noop"] is False
        assert report["stored"] is True
        assert report["preset"] == first["preset"]
        stored = json.loads(_user_preset_file(marketplace_data_dir).read_text(
            encoding="utf-8"))
        assert stored["config"]["url"] == "https://example.test/mcp/v2/"
        row = registry.get_installed("mcp", "demo_mcp")
        assert row["version"] == "1.1.0"
        assert row["runtime_slug"] == "demo_mcp"
        assert _connections() == []

    def test_an_uppercase_preset_id_is_refused(
        self, marketplace_data_dir, monkeypatch
    ):
        """The id becomes ``config.preset``, which the connector layer lowercases."""
        from core.marketplace.installer import InstallError

        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir, entity_id="Demo_MCP", preset_id="Demo_MCP"
        )

        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "invalid_preset_id"
        assert _connections() == []


# ─── Refusals ─────────────────────────────────────────────────────────────

class TestRefusals:
    def test_a_preset_carrying_a_credential_is_refused(
        self, marketplace_data_dir, monkeypatch
    ):
        """The catalog declares which fields exist, never what they hold."""
        from core.marketplace.installer import InstallError

        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir,
            preset={"config": {
                "transport": "http",
                "url": "https://example.test/mcp/",
                "token": "ghp_secret_value",
            }},
        )
        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "preset_carries_secrets"
        assert not _user_preset_file(marketplace_data_dir).exists()
        assert _connections() == []

    def test_a_secret_field_description_with_a_value_is_refused(
        self, marketplace_data_dir, monkeypatch
    ):
        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir,
            preset={"config": {
                "transport": "http",
                "url": "https://example.test/mcp/",
                "secret_env_fields": {
                    "token": {"env": "DEMO_TOKEN", "label": "Token", "value": "abc"}
                },
            }},
        )
        from core.marketplace.installer import InstallError

        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "preset_carries_secrets"

    def test_a_server_on_another_protocol_revision_is_refused(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace.installer import InstallError

        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir, preset={"protocol_version": "2025-11-25"}
        )
        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "protocol_incompatible"
        assert "2025-11-25" in excinfo.value.message
        assert not _user_preset_file(marketplace_data_dir).exists()

    def test_a_server_with_a_compatible_range_is_accepted(
        self, marketplace_data_dir, monkeypatch
    ):
        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir,
            preset={
                "protocol_version": "2025-11-25",
                "protocol_compat": {"min": "2025-03-26", "max": "2026-07-28"},
            },
        )
        report = _install(entity)

        assert report["ok"] is True

    def test_an_auth_header_naming_an_undeclared_field_is_refused(
        self, marketplace_data_dir, monkeypatch
    ):
        """A header placeholder must be a field the user can actually fill."""
        from core.marketplace.installer import InstallError

        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir,
            preset={"config": {
                "transport": "http",
                "url": "https://example.test/mcp/",
                "auth_header": "Bearer {SOME_OTHER_TOKEN}",
                "secret_env_fields": PRESET["secret_env_fields"],
            }},
        )
        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "undeclared_credential_field"
        assert excinfo.value.details["fields"] == ["SOME_OTHER_TOKEN"]

    def test_a_disabled_marketplace_refuses_the_install(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace.installer import InstallError

        _settings(monkeypatch, enabled=False)
        entity, _package = _mcp_entity(marketplace_data_dir)
        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "marketplace_disabled"
        assert not _user_preset_file(marketplace_data_dir).exists()


# ─── Dispatcher and catalog ───────────────────────────────────────────────

def test_install_entity_dispatches_to_the_mcp_installer(
    marketplace_data_dir, monkeypatch
):
    _settings(monkeypatch)
    entity, _package = _mcp_entity(marketplace_data_dir)

    from core.marketplace.installer import install_entity

    report = install_entity(entity, channel="main")

    assert report["type"] == "mcp"
    assert report["preset"] == "demo_mcp"
    assert report["stored"] is True
    assert _connections() == []


def test_the_catalog_card_reports_the_required_credentials(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = _mcp_entity(tmp_path)
    root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(root)

    from core.marketplace import catalog
    from core.marketplace.source import MarketplaceSource

    view = catalog.fetch_card(
        "demo_mcp", source=MarketplaceSource(base_url), type_="mcp"
    )

    assert view["requires_secrets"] == ["DEMO_TOKEN"]
    assert view["distribution"] == {}


def test_the_conflict_matrix_treats_a_server_as_installable(
    marketplace_data_dir, monkeypatch
):
    """An MCP entity owns no folder, so no folder can be reported as taken.

    The matrix reads the folder an entity occupies; for a server there is
    none, and a plan that invented a conflict would stop the user from
    installing something that is not there.
    """
    from core.marketplace import conflicts

    _settings(monkeypatch)
    entity, _package = _mcp_entity(marketplace_data_dir)
    plan = conflicts.resolve_plan(entity, type_="mcp")

    assert plan["action"] == "install"
    assert plan["runtime_slug"] == "demo_mcp"


def test_an_installed_server_reports_a_noop_plan(
    marketplace_data_dir, monkeypatch
):
    from core.marketplace import conflicts

    _settings(monkeypatch)
    entity, _package = _mcp_entity(marketplace_data_dir)
    _install(entity)
    plan = conflicts.resolve_plan(entity, type_="mcp")

    assert plan["action"] == "noop"


# ─── Uninstall ────────────────────────────────────────────────────────────

class TestUninstall:
    def test_it_removes_the_preset_it_installed(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import conflicts, registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        _install(entity)
        preset_file = _user_preset_file(marketplace_data_dir)
        assert preset_file.is_file()

        report = conflicts.uninstall("mcp", "demo_mcp")

        assert report["removed"] is True
        assert report["preset_removed"] is True
        assert not preset_file.exists()
        assert registry.get_installed("mcp", "demo_mcp") is None

    def test_it_takes_the_tile_off_the_mcp_tab(
        self, marketplace_data_dir, monkeypatch
    ):
        from core import connector_presets
        from core.marketplace import conflicts

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        _install(entity)
        assert "demo_mcp" in [tile["id"] for tile in connector_presets.list_presets("mcp")]

        conflicts.uninstall("mcp", "demo_mcp")

        assert "demo_mcp" not in [
            tile["id"] for tile in connector_presets.list_presets("mcp")]

    def test_connections_created_from_the_preset_stay(
        self, marketplace_data_dir, monkeypatch
    ):
        """The user's own accounts outlive the showcase entry."""
        from core import connector_presets, connectors
        from core.marketplace import conflicts

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        _install(entity)
        made = connector_presets.instantiate_preset(
            "demo_mcp", "mcp", name="First mailbox")
        conn_id = made["connection"]["id"]
        connectors.update_connection(
            conn_id, secrets={"env_pairs": {"DEMO_TOKEN": "a-real-token"}},
        )

        report = conflicts.uninstall("mcp", "demo_mcp")

        assert report["preset_removed"] is True
        assert connectors.get_connection(conn_id) is not None
        assert len(_connections()) == 1

    def test_a_bundled_preset_is_only_unregistered(
        self, marketplace_data_dir, monkeypatch
    ):
        """The platform's own template is left on disk."""
        from core import connector_presets
        from core.marketplace import conflicts, registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            marketplace_data_dir,
            preset={
                "config": {
                    "transport": "http",
                    "url": "https://mcp.vkusvill.ru/mcp",
                    "auth_header": "Bearer {VKUSVILL_TOKEN}",
                },
                "secret_env_fields": {
                    "token": {"env": "VKUSVILL_TOKEN", "label": "Token"},
                },
            },
            entity_id="vkusvill",
            preset_id="vkusvill",
        )
        _install(entity)

        report = conflicts.uninstall("mcp", "vkusvill")

        assert report["preset_removed"] is False
        assert registry.get_installed("mcp", "vkusvill") is None
        assert connector_presets.bundled_preset("vkusvill", "mcp") is not None

    def test_an_old_row_with_a_connection_id_still_removes_the_draft(
        self, marketplace_data_dir, monkeypatch
    ):
        """A row written before presets were installed keeps working.

        Such a row records the id of a credential-less connection the old
        installer created. Uninstalling the entity removes that draft - the
        user never filled it in - and leaves the preset catalogue alone.
        """
        from core import connector_presets, connectors
        from core.marketplace import conflicts, registry

        _settings(monkeypatch)
        draft = connectors.create_connection(
            service="mcp",
            name="Legacy demo",
            config={
                "transport": "http",
                "url": "https://example.test/mcp/",
                "preset": "legacy_demo",
            },
            pending=True,
        )
        registry.upsert_installed({
            "type": "mcp",
            "id": "legacy_demo",
            "runtime_slug": draft["id"],
            "runtime_db_id": None,
            "runtime_skill_id": None,
            "fork_of": None,
            "version": "1.0.0",
            "channel": "main",
            "source_url": "",
            "package_sha256": None,
            "tree_sha256": None,
            "index_sha256": None,
            "files_json": {},
            "skipped_code_files_json": [],
            "installed_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "installed_by": "marketplace",
            "user_modified": False,
            "user_modified_files_json": [],
            "install_id": None,
            "bundle_id": None,
            "pinned": False,
            "license_accepted_at": None,
        })

        report = conflicts.uninstall("mcp", "legacy_demo")

        assert report["removed"] is True
        assert report["connection_removed"] is True
        assert report["preset_removed"] is False
        assert not connectors.get_connection(draft["id"])
        assert "legacy_demo" not in [
            tile["id"] for tile in connector_presets.list_presets("mcp")]

    def test_uninstalling_twice_is_a_noop(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import conflicts

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        _install(entity)
        conflicts.uninstall("mcp", "demo_mcp")

        again = conflicts.uninstall("mcp", "demo_mcp")

        assert again["removed"] is False


# ─── Deleting the preset by hand ──────────────────────────────────────────

class TestPresetDeletion:
    def test_deleting_the_preset_file_does_not_uninstall_the_entity(
        self, marketplace_data_dir, monkeypatch
    ):
        """The registry row is the record of the installation, not the file.

        A tile removed by hand is a broken installation; the showcase shows
        it as installed and a re-install writes the preset back.
        """
        from core import connector_presets
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(marketplace_data_dir)
        _install(entity)
        assert connector_presets.delete_preset("demo_mcp") is True

        assert registry.get_installed("mcp", "demo_mcp") is not None
        again = _install(entity)
        assert again["stored"] is True
        assert _user_preset_file(marketplace_data_dir).is_file()
