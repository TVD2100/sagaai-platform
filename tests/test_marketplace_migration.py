# -*- coding: utf-8 -*-
"""
tests.test_marketplace_migration - adopting entities that predate the catalog.

Bundled entities (a default orchestrator, a prebuilt knowledge base, a preset
skill, a connector preset) already exist on an installation that predates the
marketplace. These tests pin the contract of the adoption:

  * an entity that is present is registered under its catalog id with the
    synthetic bundled version, so the showcase shows "installed" instead of
    offering a second copy;
  * an entity the user deleted is never resurrected - neither by the adoption
    nor by the bundled default import, which is a second, older path to the
    same folder;
  * the pre-marketplace delete markers are carried into the registry, because
    they live in files the catalog code never reads;
  * adoption is idempotent and does not duplicate a folder that a catalog row
    already owns.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402,F401 - fixtures
    marketplace_data_dir,
    serve_catalog,
)

# ─── Helpers ──────────────────────────────────────────────────────────────

@pytest.fixture
def ready_dir(marketplace_data_dir):
    """A temporary DATA_DIR with the marketplace tables created."""
    from core.marketplace import registry

    registry.ensure_tables()
    return marketplace_data_dir


def _make_agent_folder(slug: str, prompt: str = "prompt") -> str:
    """Create an orchestrator folder with the files an agent consists of."""
    from core.orchestrator_folders import get_orchestrator_dir

    folder = get_orchestrator_dir(slug)
    os.makedirs(os.path.join(folder, "instructions"), exist_ok=True)
    with open(os.path.join(folder, "system_prompt.md"), "w", encoding="utf-8") as handle:
        handle.write(prompt)
    with open(os.path.join(folder, "orchestrator.json"), "w", encoding="utf-8") as handle:
        json.dump({"slug": slug, "name": slug}, handle)
    with open(os.path.join(folder, "instructions", "guide.md"), "w", encoding="utf-8") as handle:
        handle.write("guide")
    return folder


def _make_rag_base(slug: str, source: str = "") -> str:
    """Create a base folder with a manifest and the given source marker."""
    from core import rag

    folder = rag.base_dir(slug)
    os.makedirs(folder, exist_ok=True)
    manifest = {"slug": slug, "name": slug, "type": "rag", "status": "ready"}
    if source:
        manifest["source"] = source
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    return folder


def _register_default_skill(folder: str) -> str:
    """Create a skill folder and a library record pointing at defaults/."""
    from core import skills_library

    root = skills_library.get_skills_root()
    path = os.path.join(root, folder)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write("body")
    skill_id = skills_library.register_skill(folder.title(), "", folder)
    registry_data = skills_library._load_registry()
    record = registry_data.get(skill_id)
    if record is not None:
        record["source"] = f"defaults/{folder}"
        skills_library._save_registry(registry_data)
    return skill_id


# ─── Adoption ─────────────────────────────────────────────────────────────

class TestAdoption:
    def test_an_empty_installation_adopts_nothing(self, ready_dir):
        from core.marketplace import migration

        assert migration.seed_legacy_entities() == []

    def test_a_bundled_agent_is_adopted(self, ready_dir):
        from core.marketplace import migration, registry

        _make_agent_folder("ya_agent")

        seeded = migration.seed_legacy_entities()

        assert [row["id"] for row in seeded] == ["ya_agent"]
        row = registry.get_installed("agent", "ya_agent")
        assert row is not None
        # The synthetic version means "the copy that shipped with the
        # platform": the first catalog release is higher, so the user is
        # offered a real update instead of a silent no-op.
        assert row["version"] == "2.0.0"
        assert row["channel"] == "legacy"
        assert row["installed_by"] == "legacy_defaults"
        assert row["runtime_slug"] == "ya_agent"
        assert row["tree_sha256"]

    def test_a_bundled_base_is_adopted_from_its_source_marker(self, ready_dir):
        from core.marketplace import migration, registry

        _make_rag_base("yaagentai_2020", source="defaults/rag_bases/yaagentai_2020")

        migration.seed_legacy_entities()

        row = registry.get_installed("rag", "yaagentai_2020")
        assert row is not None
        assert row["version"] == "1.0.0"

    def test_a_base_without_the_source_marker_is_not_adopted(self, ready_dir):
        """A base the user built is not a bundled entity."""
        from core.marketplace import migration, registry

        _make_rag_base("yaagentai_2020")

        assert migration.seed_legacy_entities() == []
        assert registry.get_installed("rag", "yaagentai_2020") is None

    def test_a_default_skill_is_adopted(self, ready_dir):
        from core.marketplace import migration, registry

        skill_id = _register_default_skill("rag_base_creator")

        migration.seed_legacy_entities()

        row = registry.get_installed("skill", "rag_base_creator")
        assert row is not None
        assert row["runtime_skill_id"] == skill_id

    def test_a_connection_using_the_bundled_preset_is_adopted(self, ready_dir):
        from core import connectors
        from core.marketplace import migration, registry

        connectors.create_connection(
            service="mcp",
            name="GitHub MCP",
            config={"transport": "http", "url": "https://example.test/mcp", "preset": "github_mcp"},
        )

        migration.seed_legacy_entities()

        row = registry.get_installed("mcp", "github_mcp")
        assert row is not None
        assert row["runtime_slug"]

    def test_adoption_is_idempotent(self, ready_dir):
        from core.marketplace import migration

        _make_agent_folder("ya_agent")

        assert len(migration.seed_legacy_entities()) == 1
        assert migration.seed_legacy_entities() == []

    def test_a_bundled_executor_is_adopted(self, ready_dir):
        """The general-purpose executor ships with the platform as well."""
        from core.marketplace import migration, registry

        _make_agent_folder("universal_agent")

        seeded = migration.seed_legacy_entities()

        assert [row["id"] for row in seeded] == ["universal_agent"]
        row = registry.get_installed("agent", "universal_agent")
        assert row is not None
        # Below the first catalog release, so the catalog offers a real
        # update instead of the installation pretending to be current.
        assert row["version"] == "0.9.0"
        assert row["channel"] == "legacy"
        assert row["runtime_slug"] == "universal_agent"

    def test_the_executor_entry_stays_below_its_first_catalog_release(self):
        from core.marketplace import migration, semver

        spec = migration.LEGACY_DEFAULTS[("agent", "universal_agent")]

        assert semver.compare(str(spec["version"]), "1.0.0") < 0

    def test_both_bundled_agents_are_adopted_side_by_side(self, ready_dir):
        from core.marketplace import migration

        _make_agent_folder("ya_agent")
        _make_agent_folder("universal_agent")

        seeded = migration.seed_legacy_entities()

        assert sorted(row["id"] for row in seeded) == ["universal_agent", "ya_agent"]

    def test_a_deleted_entity_is_not_adopted(self, ready_dir):
        from core.marketplace import migration, registry

        _make_agent_folder("ya_agent")
        registry.mark_removed("agent", "ya_agent", "ya_agent")

        assert migration.seed_legacy_entities() == []


# ─── Legacy removal markers ───────────────────────────────────────────────

class TestLegacyRemovalMarkers:
    def test_a_deleted_base_marker_is_carried_into_the_registry(self, ready_dir):
        from core import rag
        from core.marketplace import migration, registry

        rag._save_removed_defaults(["defaults/rag_bases/yaagentai_2020"])

        carried = migration.carry_removal_markers()

        assert {"type": "rag", "id": "yaagentai_2020"} in carried
        assert registry.is_removed("rag", "yaagentai_2020") is True

    def test_a_carried_marker_keeps_a_present_folder_unadopted(self, ready_dir):
        """The marker wins over the folder: the user deleted the base."""
        from core import rag
        from core.marketplace import migration, registry

        _make_rag_base("yaagentai_2020", source="defaults/rag_bases/yaagentai_2020")
        rag._save_removed_defaults(["defaults/rag_bases/yaagentai_2020"])

        result = migration.migrate()

        assert result["seeded"] == []
        assert registry.is_removed("rag", "yaagentai_2020") is True
        assert registry.get_installed("rag", "yaagentai_2020") is None

    def test_a_deleted_skill_marker_is_carried(self, ready_dir):
        from core import skills_library
        from core.marketplace import migration, registry

        skills_library._save_removed_defaults(["defaults/rag_base_creator"])

        carried = migration.carry_removal_markers()

        assert {"type": "skill", "id": "rag_base_creator"} in carried
        assert registry.is_removed("skill", "rag_base_creator") is True

    def test_carrying_is_idempotent(self, ready_dir):
        from core import rag
        from core.marketplace import migration

        rag._save_removed_defaults(["defaults/rag_bases/yaagentai_2020"])

        assert len(migration.carry_removal_markers()) == 1
        assert migration.carry_removal_markers() == []

    def test_migrate_reports_both_halves(self, ready_dir):
        from core.marketplace import migration

        _make_agent_folder("ya_agent")

        result = migration.migrate()

        assert result["ok"] is True
        assert [row["id"] for row in result["seeded"]] == ["ya_agent"]
        assert result["carried"] == []


# ─── Bundled default import must not resurrect ────────────────────────────

class TestDefaultImportRespectsRemoval:
    def test_the_agent_import_skips_a_marketplace_removal(self, ready_dir):
        from core import default_imports
        from core.marketplace import registry

        registry.mark_removed("agent", "ya_agent", "ya_agent")

        result = default_imports.ensure_default_orchestrators()

        assert result.get("ya_agent") == "exists"
        from core.orchestrators import get_orchestrator

        assert get_orchestrator("ya_agent") is None

    def test_the_base_import_skips_a_marketplace_removal(self, ready_dir):
        from core import default_imports, rag
        from core.marketplace import registry

        registry.mark_removed("rag", "yaagentai_2020", "yaagentai_2020")

        result = default_imports.ensure_default_rag_bases()

        assert result.get("yaagentai_2020") == "exists"
        assert rag.get_base("yaagentai_2020", with_stats=False) == {}

    def test_a_disabled_marketplace_leaves_the_import_alone(self, ready_dir, monkeypatch):
        """With the switch off the bundled defaults keep working as before."""
        from core import default_imports
        from core.marketplace import config as marketplace_config

        monkeypatch.setattr(
            marketplace_config,
            "marketplace_settings",
            lambda: {
                "enabled": False,
                "base_url": "",
                "allow_code": True,
                "require_signature": False,
                "channel": "main",
            },
        )

        result = default_imports.ensure_default_orchestrators()

        assert result.get("ya_agent") == "created"


# ─── Deletion writes the marker ───────────────────────────────────────────

class TestDeletionWritesTheMarker:
    def test_deleting_an_adopted_agent_records_a_removal(self, ready_dir):
        from core.marketplace import migration, registry
        from core.orchestrators import repo_create_orchestrator

        _make_agent_folder("ya_agent")
        repo_create_orchestrator(
            orchestrator_id="ya123456",
            slug="ya_agent",
            name="YaAgent",
            description="",
            prompt_text="prompt",
            config={},
            tools=[],
            max_steps=100,
            auto_apply=True,
            is_builtin=False,
            sort_order=150,
        )
        migration.seed_legacy_entities()

        from core.orchestrators import delete_orchestrator, get_orchestrator

        assert get_orchestrator("ya_agent") is not None
        assert delete_orchestrator("ya_agent") is True

        assert registry.is_removed("agent", "ya_agent") is True
        assert registry.get_installed("agent", "ya_agent") is None

    def test_deleting_a_hand_made_orchestrator_records_nothing(self, ready_dir):
        """An orchestrator the marketplace never installed stays local."""
        from core.marketplace import registry
        from core.orchestrators import (
            delete_orchestrator,
            repo_create_orchestrator,
        )

        repo_create_orchestrator(
            orchestrator_id="abc12345",
            slug="my_own",
            name="My own",
            description="",
            prompt_text="prompt",
            config={},
            tools=[],
            max_steps=100,
            auto_apply=True,
            is_builtin=False,
            sort_order=150,
        )

        assert delete_orchestrator("my_own") is True
        assert registry.list_removed("agent") == []

    def test_deleting_a_base_with_the_marketplace_block_records_a_removal(self, ready_dir):
        from core import rag
        from core.marketplace import registry

        folder = _make_rag_base("yaagentai_2020")
        path = os.path.join(folder, "manifest.json")
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        manifest["marketplace"] = {"id": "yaagentai_2020", "version": "1.1.0"}
        Path(path).write_text(json.dumps(manifest), encoding="utf-8")
        registry.upsert_installed({
            "type": "rag", "id": "yaagentai_2020", "runtime_slug": "yaagentai_2020",
            "version": "1.1.0", "channel": "main", "source_url": "catalog", "files_json": {},
            "installed_at": "2026-10-27T10:00:00Z",
            "updated_at": "2026-10-27T10:00:00Z", "installed_by": "marketplace",
        })

        assert rag.delete_base("yaagentai_2020") is True

        assert registry.is_removed("rag", "yaagentai_2020") is True
        assert registry.get_installed("rag", "yaagentai_2020") is None

    def test_deleting_an_installed_skill_records_a_removal(self, ready_dir):
        """A catalog skill deleted from the library must not come back."""
        from core import skills_library
        from core.marketplace import registry

        skill_id = _register_default_skill("rag_base_creator")
        registry.upsert_installed({
            "type": "skill", "id": "rag_base_creator",
            "runtime_slug": "rag_base_creator", "runtime_skill_id": skill_id,
            "version": "1.0.0", "channel": "main", "source_url": "catalog",
            "files_json": {},
            "installed_at": "2026-10-27T10:00:00Z",
            "updated_at": "2026-10-27T10:00:00Z",
            "installed_by": "marketplace",
        })

        assert skills_library.delete_skill(skill_id) is True

        assert registry.is_removed("skill", "rag_base_creator") is True
        assert registry.get_installed("skill", "rag_base_creator") is None


# ─── No duplicate rows for one folder ─────────────────────────────────────

class TestNoDuplicateRows:
    def test_seeding_skips_a_folder_a_catalog_row_already_owns(self, ready_dir):
        from core.marketplace import migration, registry

        _register_default_skill("rag_base_creator")
        migration.seed_legacy_entities()

        # The local-orphan seeding derives ``local/<folder>`` ids; the adopted
        # row owns the same folder under the catalog id, so a second row for
        # it would show one installation twice.
        assert registry.seed_local_orphans() == []
        rows = [row for row in registry.list_installed("skill")
                if row.get("runtime_slug") == "rag_base_creator"]
        assert len(rows) == 1


# ─── The executor: bundled AND in the catalog ─────────────────────────────

class TestBundledExecutorIsOneInstallation:
    """A preinstalled executor must not be offered a second copy."""

    def _entity(self, version="1.0.0"):
        return {
            "schema": "sagaai.marketplace.entity/1",
            "type": "agent",
            "agent_kind": "orchestrator",
            "id": "universal_agent",
            "version": version,
            "name": {"en": "Universal-Agent"},
            "description": {"en": "General-purpose executor"},
            "license": {"spdx": "PolyForm-Noncommercial-1.0.0"},
        }

    def test_the_catalog_offers_an_update_not_a_second_copy(self, ready_dir):
        from core.marketplace import conflicts, migration

        _make_agent_folder("universal_agent")
        migration.seed_legacy_entities()

        plan = conflicts.resolve_plan(self._entity(), type_="agent")

        assert plan["action"] == "update"
        assert plan["runtime_slug"] == "universal_agent"

    def test_the_adopted_row_is_not_reported_as_user_modified(self, ready_dir):
        """The adoption hashes what is on disk, so nothing looks edited."""
        from core.marketplace import conflicts, migration

        _make_agent_folder("universal_agent")
        migration.seed_legacy_entities()

        state = conflicts.local_state(self._entity(), type_="agent")

        assert state["installed"] is True
        assert state["user_modified"] is False
        assert state["modified_files"] == []

    def test_a_never_touched_copy_is_recognised_as_adoptable(self, ready_dir):
        """Even without a registry row the folder is adopted, not duplicated."""
        from core.marketplace import conflicts

        folder = _make_agent_folder("universal_agent")
        entity = self._entity()
        entity["files"] = {
            path: __import__("hashlib").sha256(
                open(os.path.join(folder, path), "rb").read()
            ).hexdigest()
            for path in ("orchestrator.json", "system_prompt.md")
        }

        plan = conflicts.resolve_plan(entity, type_="agent")

        assert plan["action"] == "adopt"

    def test_installing_from_the_catalog_updates_the_same_folder(
        self, ready_dir, serve_catalog, tmp_path
    ):
        """The end of the chain: one folder, one row, no suffixed second copy."""
        from core.marketplace import migration, registry
        from core.marketplace.agent_installer import install_agent
        from core.marketplace.source import MarketplaceSource
        from core.orchestrator_folders import get_orchestrator_dir
        from core.orchestrators import get_orchestrator
        from tests.marketplace_fixtures import make_catalog, make_entity

        _make_agent_folder("universal_agent", prompt="Bundled prompt.\n")
        migration.seed_legacy_entities()

        bundle = {
            "format": "sagaai_orchestrator/v1",
            "slug": "universal_agent",
            "name": "Universal-Agent",
            "description": "General-purpose executor",
            "config": {"strong_model": "from_package"},
            "tools": [],
            "max_steps": 300,
            "auto_apply": True,
        }
        files = {
            "orchestrator.json": json.dumps(bundle, ensure_ascii=False, indent=2),
            "system_prompt.md": "You are the general-purpose executor.\n",
        }
        entity, package = make_entity(
            entity_id="universal_agent", version="1.0.0", type_="agent", files=files
        )
        entity["agent_kind"] = "orchestrator"
        root = make_catalog(tmp_path / "catalog", [(entity, package)])

        report = install_agent(
            entity, channel="main", source=MarketplaceSource(serve_catalog(root))
        )

        assert report["ok"] is True
        assert report["runtime_slug"] == "universal_agent"
        # The preinstalled folder was refreshed in place, not duplicated.
        assert not Path(get_orchestrator_dir("universal_agent_2")).exists()
        assert Path(
            get_orchestrator_dir("universal_agent"), "system_prompt.md"
        ).read_text(encoding="utf-8") == "You are the general-purpose executor.\n"
        assert get_orchestrator("universal_agent") is not None
        assert get_orchestrator("universal_agent_2") is None
        # One registry row: the adopted record moved to the catalog version.
        rows = [row for row in registry.list_installed("agent")
                if row.get("runtime_slug") == "universal_agent"]
        assert len(rows) == 1
        assert rows[0]["version"] == "1.0.0"
