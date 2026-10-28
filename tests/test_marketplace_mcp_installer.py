"""
test_marketplace_mcp_installer.py - installing an ``mcp`` entity.

An MCP entity is a connector preset: it ships no files, it produces a
connection. These tests drive the real installer against a temporary DATA_DIR
and pin what is easy to get wrong: the preset never carries a credential, a
second install adopts the existing connection instead of duplicating it, a
re-install of the same version changes nothing, and a preset whose server
cannot speak the platform's MCP revision is refused before anything is
created.
"""
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


def _mcp_entity(tmp_path, version="1.0.0", *, preset=None, extra=None):
    """Return ``(entity, package)`` of one MCP entity (no package files)."""
    block = dict(PRESET)
    block.update(preset or {})
    payload = {"preset": block, "preset_id": "demo_mcp", "service": "mcp"}
    payload.update(extra or {})
    return make_entity(
        entity_id="demo_mcp",
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


# ─── Creation ─────────────────────────────────────────────────────────────

class TestCreate:
    def test_creates_a_draft_connection_and_a_registry_row(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        report = _install(entity)

        assert report["ok"] is True
        assert report["created"] is True
        assert report["preset"] == "demo_mcp"
        manifests = _connections()
        assert len(manifests) == 1
        connection = manifests[0]
        assert connection["id"] == report["connection_id"]
        assert connection["config"]["preset"] == "demo_mcp"
        # A catalog preset has no credentials: the connection waits for them.
        assert connection["credentials_status"] == "pending"
        assert report["requires_secrets"] == ["DEMO_TOKEN"]

        row = registry.get_installed("mcp", "demo_mcp")
        assert row is not None
        assert row["runtime_slug"] == report["connection_id"]
        assert row["version"] == "1.0.0"

    def test_the_preset_memory_seed_becomes_the_connection_memory(
        self, marketplace_data_dir, monkeypatch
    ):
        from core import connectors

        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        report = _install(entity)

        memory = connectors.get_connection_memory(report["connection_id"])
        assert memory == PRESET["memory_seed"]

    def test_a_stdio_preset_is_accepted_with_its_args(
        self, marketplace_data_dir, monkeypatch
    ):
        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            Path("/tmp"),
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
        config = _connections()[0]["config"]
        assert config["transport"] == "stdio"
        assert config["command"] == "npx"
        assert config["args"][0] == "-y"


# ─── Deduplication ────────────────────────────────────────────────────────

class TestIdentity:
    def test_a_second_install_adopts_the_existing_connection(
        self, marketplace_data_dir, monkeypatch
    ):
        """The connection id is random, so identity is (service, preset)."""
        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        first = _install(entity)
        # Pretend the registry lost the row - a restored backup, a manual
        # edit. The connection itself is still there.
        from core.marketplace import registry

        registry.remove_installed("mcp", "demo_mcp")

        second = _install(entity)

        assert second["ok"] is True
        assert second["adopted"] is True
        assert second["created"] is False
        assert second["connection_id"] == first["connection_id"]
        assert len(_connections()) == 1

    def test_forcing_a_new_connection_keeps_both(
        self, marketplace_data_dir, monkeypatch
    ):
        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        first = _install(entity)
        second = _install(entity, adopt_existing=False)

        assert second["created"] is True
        assert second["connection_id"] != first["connection_id"]
        assert len(_connections()) == 2

    def test_a_reinstall_of_the_same_version_is_a_noop(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        first = _install(entity)
        second = _install(entity)

        assert first["noop"] is False
        assert second["noop"] is True
        # A no-op run leaves the recorded install untouched.
        assert registry.get_installed("mcp", "demo_mcp")["install_id"] == first["install_id"]
        assert len(_connections()) == 1

    def test_a_changed_non_secret_setting_is_applied_in_place(
        self, marketplace_data_dir, monkeypatch
    ):
        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        first = _install(entity)

        updated, _package = _mcp_entity(
            Path("/tmp"),
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
        assert report["connection_id"] == first["connection_id"]
        assert len(_connections()) == 1
        assert _connections()[0]["config"]["url"] == "https://example.test/mcp/v2/"


# ─── Refusals ─────────────────────────────────────────────────────────────

class TestRefusals:
    def test_a_preset_carrying_a_credential_is_refused(
        self, marketplace_data_dir, monkeypatch
    ):
        """The catalog declares which fields exist, never what they hold."""
        from core.marketplace.installer import InstallError

        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            Path("/tmp"),
            preset={"config": {
                "transport": "http",
                "url": "https://example.test/mcp/",
                "token": "ghp_secret_value",
            }},
        )
        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "preset_carries_secrets"
        assert _connections() == []

    def test_a_secret_field_description_with_a_value_is_refused(
        self, marketplace_data_dir, monkeypatch
    ):
        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            Path("/tmp"),
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
            Path("/tmp"), preset={"protocol_version": "2025-11-25"}
        )
        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "protocol_incompatible"
        assert "2025-11-25" in excinfo.value.message
        assert _connections() == []

    def test_a_server_with_a_compatible_range_is_accepted(
        self, marketplace_data_dir, monkeypatch
    ):
        _settings(monkeypatch)
        entity, _package = _mcp_entity(
            Path("/tmp"),
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
            Path("/tmp"),
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
        entity, _package = _mcp_entity(Path("/tmp"))
        with pytest.raises(InstallError) as excinfo:
            _install(entity)

        assert excinfo.value.code == "marketplace_disabled"
        assert _connections() == []


# ─── Dispatcher and catalog ───────────────────────────────────────────────

def test_install_entity_dispatches_to_the_mcp_installer(
    marketplace_data_dir, monkeypatch
):
    _settings(monkeypatch)
    entity, _package = _mcp_entity(Path("/tmp"))

    from core.marketplace.installer import install_entity

    report = install_entity(entity, channel="main")

    assert report["type"] == "mcp"
    assert report["created"] is True


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
    entity, _package = _mcp_entity(Path("/tmp"))
    plan = conflicts.resolve_plan(entity, type_="mcp")

    assert plan["action"] == "install"
    assert plan["runtime_slug"] == "demo_mcp"


def test_an_installed_server_reports_a_noop_plan(
    marketplace_data_dir, monkeypatch
):
    from core.marketplace import conflicts, registry

    _settings(monkeypatch)
    entity, _package = _mcp_entity(Path("/tmp"))
    report = _install(entity)
    registry.upsert_installed(
        dict(registry.get_installed("mcp", "demo_mcp"), runtime_slug=report["connection_id"])
    )
    plan = conflicts.resolve_plan(entity, type_="mcp")

    assert plan["action"] == "noop"


# ─── Uninstall ────────────────────────────────────────────────────────────

class TestUninstall:
    def test_it_removes_the_connection_it_created(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import conflicts, registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        _install(entity)

        report = conflicts.uninstall("mcp", "demo_mcp")

        assert report["removed"] is True
        assert report["connection_removed"] is True
        assert _connections() == []
        assert registry.get_installed("mcp", "demo_mcp") is None

    def test_it_keeps_a_connection_the_user_has_configured(
        self, marketplace_data_dir, monkeypatch
    ):
        """Credentials entered by hand belong to the user, not the catalog.

        A configured connection may be used by prompts that have nothing to
        do with the showcase entry, so uninstalling the entity only drops
        the entry and leaves the connection alone.
        """
        from core import connectors
        from core.marketplace import conflicts

        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        installed = _install(entity)
        connectors.update_connection(
            installed["connection_id"],
            secrets={"env_pairs": {"DEMO_TOKEN": "a-real-token"}},
        )
        assert connectors.get_connection(installed["connection_id"])["credentials_status"] == "active"

        report = conflicts.uninstall("mcp", "demo_mcp")

        assert report["removed"] is True
        assert report["connection_removed"] is False
        assert len(_connections()) == 1

    def test_uninstalling_twice_is_a_noop(
        self, marketplace_data_dir, monkeypatch
    ):
        from core.marketplace import conflicts

        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        _install(entity)
        conflicts.uninstall("mcp", "demo_mcp")

        again = conflicts.uninstall("mcp", "demo_mcp")

        assert again["removed"] is False


# ─── Deleting the connection by hand ──────────────────────────────────────

class TestConnectionDeletion:
    def test_deleting_the_connection_uninstalls_the_entity(
        self, marketplace_data_dir, monkeypatch
    ):
        """The connection is the installation: deleting it stops the card.

        Without the removal marker the registry keeps the row, the showcase
        shows a server that no longer exists as installed, and pressing
        Install again would be answered with a silent no-op.
        """
        from core import connectors
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        installed = _install(entity)

        assert connectors.delete_connection(installed["connection_id"]) is True

        assert registry.get_installed("mcp", "demo_mcp") is None
        assert registry.is_removed("mcp", "demo_mcp") is True

    def test_a_rollback_does_not_leave_a_removal_marker(
        self, marketplace_data_dir, monkeypatch
    ):
        """A connection dropped because the install failed was never installed."""
        from core import connectors
        from core.marketplace import registry

        _settings(monkeypatch)
        entity, _package = _mcp_entity(Path("/tmp"))
        installed = _install(entity)

        connectors.delete_connection(installed["connection_id"], record_removal=False)

        assert registry.is_removed("mcp", "demo_mcp") is False

    def test_deleting_a_hand_made_connection_records_nothing(
        self, marketplace_data_dir, monkeypatch
    ):
        from core import connectors
        from core.marketplace import registry

        _settings(monkeypatch)
        connection = connectors.create_connection(
            service="mcp",
            name="My own server",
            config={"transport": "http", "url": "https://example.test/mcp"},
        )

        assert connectors.delete_connection(connection["id"]) is True
        assert registry.list_removed("mcp") == []
