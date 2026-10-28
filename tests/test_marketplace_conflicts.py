"""
test_marketplace_conflicts.py - the conflict matrix and the three paths.

Each test drives ``resolve_plan()`` with a locally prepared state and
checks the returned action, because that action is what the card renders
as a button. The three paths then get a real run: files on disk, the
registry row and the backup are all asserted, since "kept my edits" is
only true if the bytes are still there.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    add_catalog_version,
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


def _install(base_url, entity, **kwargs):
    """Install one skill from a local catalog."""
    from core.marketplace.installer import install_skill
    from core.marketplace.source import MarketplaceSource

    return install_skill(entity, channel="main", source=MarketplaceSource(base_url), **kwargs)


def _folder(data_dir, slug):
    return Path(data_dir) / "skills" / slug


def _v2_entity(entity_id="demo_skill"):
    """Return a second version with different content in two files."""
    return make_entity(
        entity_id=entity_id,
        version="2.0.0",
        files={
            "SKILL.md": "# v2 instructions\n",
            "references/notes.md": "v2 notes\n",
            "scripts/run.py": "print('v2')\n",
        },
    )


class TestMatrix:
    def test_row1_not_installed(self, marketplace_data_dir):
        from core.marketplace import conflicts

        plan = conflicts.resolve_plan(make_entity()[0])
        assert plan["action"] == "install"

    def test_row2_same_version_untouched(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import conflicts

        entity, package = make_entity()
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        _install(serve_catalog(catalog_root), entity)

        plan = conflicts.resolve_plan(entity)
        assert plan["action"] == "noop"

    def test_row3_same_version_modified(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import conflicts

        entity, package = make_entity()
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        result = _install(serve_catalog(catalog_root), entity)
        (_folder(marketplace_data_dir, result["runtime_slug"]) / "SKILL.md").write_text(
            "mine", encoding="utf-8"
        )

        plan = conflicts.resolve_plan(entity)
        assert plan["action"] == "modified_hold"
        assert plan["modified_files"] == ["SKILL.md"]

    def test_row4_older_installed(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import conflicts

        entity, package = make_entity(version="1.0.0")
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        _install(serve_catalog(catalog_root), entity)

        newer, newer_package = _v2_entity()
        plan = conflicts.resolve_plan(newer)
        assert plan["action"] == "update"
        assert plan["installed_version"] == "1.0.0"

    def test_row5_newer_installed_blocks_downgrade(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import conflicts

        entity, package = _v2_entity()
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        _install(serve_catalog(catalog_root), entity)

        older, older_package = make_entity(version="1.0.0")
        plan = conflicts.resolve_plan(older)
        assert plan["action"] == "downgrade_blocked"

        forced = conflicts.resolve_plan(older, force_downgrade=True)
        assert forced["action"] == "update"

    def test_row6_modified_and_newer(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import conflicts

        entity, package = make_entity(version="1.0.0")
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        result = _install(serve_catalog(catalog_root), entity)
        (_folder(marketplace_data_dir, result["runtime_slug"]) / "SKILL.md").write_text(
            "mine", encoding="utf-8"
        )

        newer, newer_package = _v2_entity()
        plan = conflicts.resolve_plan(newer)
        assert plan["action"] == "conflict_choice"
        assert plan["user_modified"] is True

    def test_row7_slug_taken_by_another_entity(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import conflicts

        first, first_package = make_entity(entity_id="first_owner")
        catalog_root = make_catalog(tmp_path / "catalog", [(first, first_package)])
        base_url = serve_catalog(catalog_root)
        installed = _install(base_url, first)

        # The second entity declares the same runtime folder under another id.
        second, second_package = make_entity(
            entity_id="second_owner",
            extra={"runtime_slug": installed["runtime_slug"]},
        )
        plan = conflicts.resolve_plan(second)
        assert plan["action"] == "suffix_install"

    def test_row8_registry_lost_is_adopted(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import conflicts, registry

        entity, package = make_entity()
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        _install(serve_catalog(catalog_root), entity)

        # Drop the registry row only: the folder still matches the catalog.
        registry.remove_installed("skill", "demo_skill")

        plan = conflicts.resolve_plan(entity)
        assert plan["action"] == "adopt"

        adopted = conflicts.adopt(entity, channel="main")
        assert adopted["adopted"] is True
        assert registry.get_installed("skill", "demo_skill")["version"] == "1.0.0"

    def test_row10_failure_rolls_the_whole_run_back(
        self, marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
    ):
        from core.marketplace import conflicts, registry, store

        entity, package = make_entity(version="1.0.0")
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        base_url = serve_catalog(catalog_root)
        result = _install(base_url, entity)
        folder = _folder(marketplace_data_dir, result["runtime_slug"])
        before = (folder / "SKILL.md").read_bytes()
        (folder / "SKILL.md").write_text("mine", encoding="utf-8")

        newer, newer_package = _v2_entity()
        add_catalog_version(catalog_root, newer, newer_package)

        def _boom(src, dst):
            if "skills" in str(dst):
                raise OSError("disk full")
            return _real_replace(src, dst)

        _real_replace = store._atomic_replace
        monkeypatch.setattr("core.marketplace.store._atomic_replace", _boom)
        with pytest.raises(conflicts.ConflictError):
            conflicts.resolve_three_way(
                "take_catalog", newer, channel="main", source=_source(base_url)
            )

        # The file the user edited is untouched and the row still says 1.0.0.
        assert (folder / "SKILL.md").read_text(encoding="utf-8") == "mine"
        assert before != b"mine"
        assert registry.get_installed("skill", "demo_skill")["version"] == "1.0.0"


def _source(base_url):
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


class TestThreePaths:
    def _installed_with_edit(self, data_dir, serve_catalog, tmp_path):
        """Install 1.0.0, edit SKILL.md and publish 2.0.0."""
        entity, package = make_entity(version="1.0.0")
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        base_url = serve_catalog(catalog_root)
        result = _install(base_url, entity)
        folder = _folder(data_dir, result["runtime_slug"])
        (folder / "SKILL.md").write_text("my own instructions", encoding="utf-8")
        newer, newer_package = _v2_entity()
        add_catalog_version(catalog_root, newer, newer_package)
        return newer, base_url, folder

    def test_keep_mine_updates_only_untouched_files(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import conflicts

        entity, base_url, folder = self._installed_with_edit(
            marketplace_data_dir, serve_catalog, tmp_path
        )
        report = conflicts.resolve_three_way(
            "keep_mine", entity, channel="main", source=_source(base_url)
        )

        assert (folder / "SKILL.md").read_text(encoding="utf-8") == "my own instructions"
        assert (folder / "references/notes.md").read_text(encoding="utf-8") == "v2 notes\n"
        assert report["kept_files"] == ["SKILL.md"]
        assert report["user_modified"] is True
        row = _row("demo_skill")
        assert row["version"] == "2.0.0"
        assert row["user_modified"] is True
        assert row["user_modified_files_json"] == ["SKILL.md"]

    def test_take_catalog_replaces_and_backs_up_user_bytes(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import conflicts

        entity, base_url, folder = self._installed_with_edit(
            marketplace_data_dir, serve_catalog, tmp_path
        )
        report = conflicts.resolve_three_way(
            "take_catalog", entity, channel="main", source=_source(base_url)
        )

        assert (folder / "SKILL.md").read_text(encoding="utf-8") == "# v2 instructions\n"
        assert report["backed_up_files"] == ["SKILL.md"]

        restored = conflicts.restore_user_version(report["install_id"])
        assert restored["ok"] is True
        assert restored["restored_files"] == ["SKILL.md"]
        # Byte-for-byte: the user's text comes back, not a normalised copy.
        assert (folder / "SKILL.md").read_text(encoding="utf-8") == "my own instructions"
        assert _row("demo_skill")["user_modified"] is True

    def test_fork_keeps_both_copies(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import conflicts, registry

        entity, base_url, folder = self._installed_with_edit(
            marketplace_data_dir, serve_catalog, tmp_path
        )
        report = conflicts.resolve_three_way(
            "fork", entity, channel="main", source=_source(base_url)
        )

        fork_slug = report["fork_slug"]
        fork_folder = _folder(marketplace_data_dir, fork_slug)
        assert report["fork_id"] == "demo_skill#fork1"
        assert fork_folder.is_dir()
        # The fork holds the user's text, the original went to the catalog.
        assert (fork_folder / "SKILL.md").read_text(encoding="utf-8") == "my own instructions"
        assert (folder / "SKILL.md").read_text(encoding="utf-8") == "# v2 instructions\n"
        fork_row = registry.get_installed("skill", "demo_skill#fork1")
        assert fork_row["fork_of"] == "demo_skill"
        assert fork_row["pinned"] is True
        assert fork_row["user_modified"] is True
        assert registry.get_installed("skill", "demo_skill")["version"] == "2.0.0"

    def test_unknown_choice_is_refused(self, marketplace_data_dir):
        from core.marketplace import conflicts

        with pytest.raises(conflicts.ConflictError) as excinfo:
            conflicts.resolve_three_way("merge", make_entity()[0], channel="main")
        assert excinfo.value.code == "invalid_choice"


def _row(entity_id: str) -> dict:
    from core.marketplace import registry

    return registry.get_installed("skill", entity_id)


class TestReset:
    def test_reset_restores_catalog_files_from_cache(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import conflicts

        entity, package = make_entity()
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        result = _install(serve_catalog(catalog_root), entity)
        folder = _folder(marketplace_data_dir, result["runtime_slug"])
        (folder / "SKILL.md").write_text("mine", encoding="utf-8")

        report = conflicts.reset_to_catalog(entity)
        assert report["reset"] is True
        assert report["reset_files"] == ["SKILL.md"]
        assert (folder / "SKILL.md").read_text(encoding="utf-8") == (
            "# demo_skill\n\nInstructions for the demo skill.\n"
        )
        assert _row("demo_skill")["user_modified"] is False

        back = conflicts.restore_user_version(report["install_id"])
        assert back["restored_files"] == ["SKILL.md"]
        assert (folder / "SKILL.md").read_text(encoding="utf-8") == "mine"

    def test_reset_without_cached_package_is_refused(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace import conflicts, store

        entity, package = make_entity()
        catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
        result = _install(serve_catalog(catalog_root), entity)
        folder = _folder(marketplace_data_dir, result["runtime_slug"])
        (folder / "SKILL.md").write_text("mine", encoding="utf-8")
        store.remove_cached_package(_row("demo_skill")["package_sha256"])

        with pytest.raises(conflicts.ConflictError) as excinfo:
            conflicts.reset_to_catalog(entity)
        assert excinfo.value.code == "package_not_cached"
        # The refusal must not touch the user's file.
        assert (folder / "SKILL.md").read_text(encoding="utf-8") == "mine"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
