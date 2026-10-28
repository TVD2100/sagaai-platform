"""
test_marketplace_registry.py - SQLite registry, hashing area and removals.

The registry is the single source of truth about what the marketplace
installed. These tests pin the parts that the rest of the feature depends
on: the tables appear on a fresh AND on an existing database, the hashing
area ignores user data, removal markers survive so nothing is resurrected,
and legacy skills are seeded with synthetic versions instead of a guessed
one.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    marketplace_data_dir,  # noqa: F401 - fixture
    make_entity,
    make_package_tar,
)


@pytest.fixture
def ready_dir(marketplace_data_dir):
    """A temporary DATA_DIR with the marketplace tables created."""
    from storage.db import get_engine

    engine = get_engine()
    from storage.db import _ensure_marketplace_tables

    _ensure_marketplace_tables(engine)
    return marketplace_data_dir


def _make_skill_folder(root: Path, name: str, files: dict) -> Path:
    """Create a skill folder with the given files and return its path."""
    folder = root / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        target = folder / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return folder


class TestMigration:
    def test_tables_created_on_fresh_database(self, ready_dir):
        from sqlalchemy import inspect

        from storage.db import get_engine

        tables = set(inspect(get_engine()).get_table_names())
        assert "marketplace_installed" in tables
        assert "marketplace_removed" in tables

    def test_migration_is_idempotent(self, ready_dir):
        from storage.db import _ensure_marketplace_tables, get_engine

        engine = get_engine()
        # Running twice must not raise and must not drop data.
        _ensure_marketplace_tables(engine)
        _ensure_marketplace_tables(engine)
        assert "marketplace_installed" in set(
            __import__("sqlalchemy").inspect(engine).get_table_names()
        )

    def test_migration_failure_does_not_block_engine(self, ready_dir, monkeypatch):
        from storage.db import _ensure_marketplace_tables

        class _BrokenEngine:
            def begin(self):
                raise RuntimeError("database is locked")

        # A failure inside the migration is logged, never raised: otherwise
        # a broken table would stop the whole application from starting.
        _ensure_marketplace_tables(_BrokenEngine())

    def test_runtime_db_id_is_text(self, ready_dir):
        from sqlalchemy import inspect

        from storage.db import get_engine

        columns = {c["name"]: c for c in inspect(get_engine()).get_columns("marketplace_installed")}
        # Entity primary keys are String(8); an INTEGER column could never
        # hold them.
        assert "TEXT" in str(columns["runtime_db_id"]["type"]).upper()


class TestInstalledRows:
    def _record(self, **overrides):
        base = {
            "type": "skill",
            "id": "demo",
            "runtime_slug": "demo",
            "version": "1.0.0",
            "channel": "main",
            "source_url": "skills/demo/1.0.0",
            "files_json": {"SKILL.md": "abc"},
            "installed_at": "2026-10-27T10:00:00Z",
            "updated_at": "2026-10-27T10:00:00Z",
            "installed_by": "marketplace",
            "install_id": "20261027T100000Z-aaaa",
        }
        base.update(overrides)
        return base

    def test_upsert_and_read_back(self, ready_dir):
        from core.marketplace import registry

        registry.upsert_installed(self._record())
        row = registry.get_installed("skill", "demo")
        assert row is not None
        assert row["version"] == "1.0.0"
        assert row["files_json"] == {"SKILL.md": "abc"}
        assert row["user_modified"] is False

    def test_upsert_replaces_existing_row(self, ready_dir):
        from core.marketplace import registry

        registry.upsert_installed(self._record())
        registry.upsert_installed(self._record(version="1.1.0"))
        rows = registry.list_installed("skill")
        assert len(rows) == 1
        assert rows[0]["version"] == "1.1.0"

    def test_remove_installed(self, ready_dir):
        from core.marketplace import registry

        registry.upsert_installed(self._record())
        registry.remove_installed("skill", "demo")
        assert registry.get_installed("skill", "demo") is None

    def test_list_filters_by_type(self, ready_dir):
        from core.marketplace import registry

        registry.upsert_installed(self._record())
        registry.upsert_installed(self._record(type="rag", id="base", runtime_slug="base"))
        assert len(registry.list_installed("skill")) == 1
        assert len(registry.list_installed("rag")) == 1
        assert len(registry.list_installed()) == 2

    def test_json_fields_round_trip(self, ready_dir):
        from core.marketplace import registry

        registry.upsert_installed(self._record(
            skipped_code_files_json=["scripts/run.py"],
            user_modified_files_json=["SKILL.md"],
            user_modified=True,
        ))
        row = registry.get_installed("skill", "demo")
        assert row["skipped_code_files_json"] == ["scripts/run.py"]
        assert row["user_modified_files_json"] == ["SKILL.md"]
        assert row["user_modified"] is True


class TestHashingArea:
    def _install_row(self, root: Path, name: str, files: dict, entity_id: str = ""):
        """Register an installed row whose manifest covers *files*."""
        from core.marketplace import registry

        _make_skill_folder(root, name, files)
        manifest = {
            rel_path: registry._sha256_file(str(root / "skills" / name / rel_path))
            for rel_path in files
        }
        registry.upsert_installed({
            "type": "skill",
            "id": entity_id or name,
            "runtime_slug": name,
            "version": "1.0.0",
            "channel": "main",
            "source_url": "x",
            "files_json": manifest,
            "tree_sha256": registry.tree_sha256("skill", name, manifest),
            "installed_at": "2026-10-27T10:00:00Z",
            "updated_at": "2026-10-27T10:00:00Z",
            "installed_by": "marketplace",
        })
        return manifest

    def test_tree_hash_covers_catalog_files_only(self, ready_dir):
        from core.marketplace import registry

        self._install_row(ready_dir, "demo", {"SKILL.md": "body"})
        before = registry.tree_sha256("skill", "demo")

        # A user file the package never declared must not change the hash:
        # the hashing area is the declared manifest, not the folder.
        (Path(ready_dir) / "skills" / "demo" / "notes.md").write_text("mine", encoding="utf-8")
        assert registry.tree_sha256("skill", "demo") == before

    def test_changing_a_catalog_file_changes_the_hash(self, ready_dir):
        from core.marketplace import registry

        self._install_row(ready_dir, "demo", {"SKILL.md": "body"})
        before = registry.tree_sha256("skill", "demo")
        (Path(ready_dir) / "skills" / "demo" / "SKILL.md").write_text("edited", encoding="utf-8")
        assert registry.tree_sha256("skill", "demo") != before

    def test_rag_index_is_outside_the_hashing_area(self, ready_dir):
        from core.marketplace import registry

        folder = Path(ready_dir) / "rag_bases" / "base" / "files"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "doc.md").write_text("text", encoding="utf-8")
        manifest = {"files/doc.md": registry._sha256_file(str(folder / "doc.md"))}
        before = registry.tree_sha256("rag", "base", manifest)

        # The first BM25 query rewrites index.db; hashing it would report a
        # modification the user never made.
        (Path(ready_dir) / "rag_bases" / "base" / "index.db").write_bytes(b"index")
        (Path(ready_dir) / "rag_bases" / "base" / "bm25_terms.json").write_text("[]", encoding="utf-8")
        assert registry.tree_sha256("rag", "base", manifest) == before

    def test_folder_walk_is_only_a_fallback(self, ready_dir):
        from core.marketplace import registry

        # Without a registry row there is nothing to compare against, so the
        # walk covers everything except volatile paths. This is the seeding
        # path for a skill installed before the marketplace existed.
        _make_skill_folder(ready_dir, "legacy", {"SKILL.md": "body"})
        walked = registry.entity_file_manifest("skill", "legacy")
        assert set(walked) == {"SKILL.md"}

    def test_manifest_limited_to_declared_files(self, ready_dir):
        from core.marketplace import registry

        _make_skill_folder(ready_dir, "demo", {"SKILL.md": "body", "other.md": "x"})
        manifest = registry.entity_file_manifest("skill", "demo", {"SKILL.md": ""})
        assert set(manifest) == {"SKILL.md"}
        assert manifest["SKILL.md"]

    def test_missing_declared_file_has_empty_hash(self, ready_dir):
        from core.marketplace import registry

        _make_skill_folder(ready_dir, "demo", {"SKILL.md": "body"})
        manifest = registry.entity_file_manifest("skill", "demo", {"gone.md": ""})
        assert manifest["gone.md"] == ""


class TestRemovalMarkers:
    def test_mark_and_check(self, ready_dir):
        from core.marketplace import registry

        assert registry.is_removed("skill", "demo") is False
        registry.mark_removed("skill", "demo", "demo")
        assert registry.is_removed("skill", "demo") is True

    def test_unmark(self, ready_dir):
        from core.marketplace import registry

        registry.mark_removed("skill", "demo", "demo")
        registry.unmark_removed("skill", "demo")
        assert registry.is_removed("skill", "demo") is False

    def test_removed_source_markers_map_to_defaults(self, ready_dir):
        from core.marketplace import registry

        registry.mark_removed("skill", "marketplace/demo", "demo")
        assert registry.removed_source_markers() == ["defaults/demo"]


class TestLegacyOrphans:
    def test_seeds_skill_without_registry_row(self, ready_dir):
        from core.marketplace import registry
        from core.skills_library import register_skill

        _make_skill_folder(ready_dir, "legacy_one", {"SKILL.md": "body"})
        sid = register_skill("Legacy One", "old skill", "legacy_one")

        seeded = registry.seed_local_orphans()
        assert len(seeded) == 1
        row = registry.get_installed("skill", "local/legacy_one")
        assert row is not None
        # Synthetic values: the marketplace cannot know a real version, and
        # guessing one would make update detection lie.
        assert row["version"] == "0.0.0"
        assert row["channel"] == "legacy"
        assert row["installed_by"] == "legacy_defaults"
        assert row["runtime_skill_id"] == sid

    def test_skips_already_registered(self, ready_dir):
        from core.marketplace import registry
        from core.skills_library import register_skill

        _make_skill_folder(ready_dir, "legacy_two", {"SKILL.md": "body"})
        register_skill("Legacy Two", "", "legacy_two")
        assert len(registry.seed_local_orphans()) == 1
        assert registry.seed_local_orphans() == []

    def test_skips_removed_entity(self, ready_dir):
        from core.marketplace import registry
        from core.skills_library import register_skill

        _make_skill_folder(ready_dir, "legacy_three", {"SKILL.md": "body"})
        register_skill("Legacy Three", "", "legacy_three")
        registry.mark_removed("skill", "local/legacy_three", "legacy_three")
        assert registry.seed_local_orphans() == []


class TestExportedCache:
    def test_export_writes_registry_json(self, ready_dir):
        from core.marketplace import registry, store

        registry.upsert_installed({
            "type": "skill", "id": "demo", "runtime_slug": "demo",
            "version": "1.0.0", "channel": "main", "source_url": "x",
            "files_json": {}, "installed_at": "2026-10-27T10:00:00Z",
            "updated_at": "2026-10-27T10:00:00Z", "installed_by": "marketplace",
        })
        path = registry.export_registry_cache()
        assert path == store.registry_cache_path()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["schema"] == "sagaai.marketplace.registry/1"
        assert "skill:demo" in data["entities"]

    def test_export_is_a_cache_not_a_source(self, ready_dir):
        from core.marketplace import registry, store

        store.ensure_layout()
        # A hand-written cache file must not be read back as truth.
        Path(store.registry_cache_path()).write_text(
            json.dumps({"entities": {"skill:ghost": {"id": "ghost"}}}),
            encoding="utf-8",
        )
        assert registry.list_installed() == []
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
