"""
test_marketplace_installer.py - skill installation, rollback and recovery.

Installation is the operation that changes the user's installation, so the
promises here are strict: a run either lands completely or leaves the
disk exactly as it found it, a second install of the same version does
nothing at all, and a crash in the middle is undone on the next start.

Every test drives the real installer against a catalog served over
loopback; only the failure cases patch the atomic-write seam, which is the
reason that seam exists.
"""
import json
import os
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


@pytest.fixture
def catalog_server(marketplace_data_dir, serve_catalog, tmp_path):
    """Serve a one-skill catalog and return ``(base_url, entity, bytes)``."""
    entity, package = make_entity()
    root = make_catalog(tmp_path / "catalog", [(entity, package)])
    return serve_catalog(root), entity, package


def _install(base_url: str, entity: dict, **kwargs):
    """Run the installer against a local channel and return its report."""
    from core.marketplace.installer import install_skill
    from core.marketplace.source import MarketplaceSource

    return install_skill(
        entity,
        channel="main",
        source=MarketplaceSource(base_url),
        **kwargs,
    )


def _skill_folders(data_dir) -> list:
    """Return the names of the skill folders currently on disk."""
    root = Path(data_dir) / "skills"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _skills_registry(data_dir) -> dict:
    """Return the parsed ``skills.json`` of the temporary installation."""
    path = Path(data_dir) / "skills" / "skills.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class TestInstall:
    def test_installs_skill_from_catalog(self, catalog_server, marketplace_data_dir):
        from core.marketplace import registry

        base_url, entity, package = catalog_server
        result = _install(base_url, entity)

        assert result["ok"] is True
        assert result["noop"] is False
        assert result["version"] == "1.0.0"
        folder = Path(marketplace_data_dir) / "skills" / result["runtime_slug"]
        assert (folder / "SKILL.md").is_file()
        assert (folder / "references" / "notes.md").is_file()

        row = registry.get_installed("skill", entity["id"])
        assert row is not None
        assert row["version"] == "1.0.0"
        assert row["runtime_skill_id"] == result["skill_id"]
        assert row["package_sha256"] == entity["package"]["sha256"]
        assert row["tree_sha256"] == result["tree_sha256"]

    def test_registers_skills_json_metadata(self, catalog_server, marketplace_data_dir):
        base_url, entity, _package = catalog_server
        result = _install(base_url, entity)

        record = _skills_registry(marketplace_data_dir)[result["skill_id"]]
        assert record["version"] == "1.0.0"
        assert record["source"] == f"marketplace/{entity['id']}"
        assert record["installed_at"]
        assert record["adapted"] is True

    def test_records_run_in_history(self, catalog_server, marketplace_data_dir):
        from core.marketplace import store

        base_url, entity, _package = catalog_server
        result = _install(base_url, entity)

        run = store.find_run(result["install_id"])
        assert run is not None
        assert run["status"] == "done"
        assert run["action"] == "install"
        assert store.get_state() is None

    def test_code_installed_by_default(self, catalog_server, marketplace_data_dir):
        base_url, entity, _package = catalog_server
        result = _install(base_url, entity)

        # allow_code defaults to true: the catalog holds the owner's own
        # entities, so a skill must arrive complete.
        folder = Path(marketplace_data_dir) / "skills" / result["runtime_slug"]
        assert (folder / "scripts" / "run.py").is_file()
        assert result["skipped_code_files"] == []

    def test_file_manifest_covers_installed_files(self, catalog_server, marketplace_data_dir):
        from core.marketplace import registry

        base_url, entity, _package = catalog_server
        _install(base_url, entity)
        row = registry.get_installed("skill", entity["id"])
        assert set(row["files_json"]) == set(entity["files"])
        # Every hash in the stored manifest must match the file on disk.
        current = registry.entity_file_manifest(
            "skill", row["runtime_slug"], row["files_json"]
        )
        assert current == row["files_json"]


class TestReinstall:
    def test_same_version_is_a_noop(self, catalog_server, marketplace_data_dir):
        from core.marketplace import registry, store

        base_url, entity, _package = catalog_server
        first = _install(base_url, entity)
        before_row = registry.get_installed("skill", entity["id"])
        before_folders = _skill_folders(marketplace_data_dir)
        before_history = len(store.load_history())

        second = _install(base_url, entity)

        assert second["noop"] is True
        assert second["install_id"] == first["install_id"]
        after_row = registry.get_installed("skill", entity["id"])
        assert after_row["installed_at"] == before_row["installed_at"]
        # No second folder with a ``_2`` suffix and no extra run entry.
        assert _skill_folders(marketplace_data_dir) == before_folders
        assert len(store.load_history()) == before_history

    def test_collision_gets_a_suffix(self, catalog_server, marketplace_data_dir):
        from core.marketplace.installer import install_skill
        from core.marketplace.source import MarketplaceSource

        base_url, entity, _package = catalog_server
        # Occupy the natural folder name with a hand-made skill.
        occupied = Path(marketplace_data_dir) / "skills" / "demo_skill"
        occupied.mkdir(parents=True, exist_ok=True)
        (occupied / "SKILL.md").write_text("mine", encoding="utf-8")

        result = install_skill(
            entity, channel="main", source=MarketplaceSource(base_url)
        )
        assert result["runtime_slug"] == "demo_skill_2"
        # The user's own folder is untouched.
        assert (occupied / "SKILL.md").read_text(encoding="utf-8") == "mine"


class TestAllowCode:
    def test_strict_mode_skips_code(self, catalog_server, marketplace_data_dir):
        from core.marketplace import registry

        base_url, entity, _package = catalog_server
        result = _install(base_url, entity, allow_code=False)

        folder = Path(marketplace_data_dir) / "skills" / result["runtime_slug"]
        assert (folder / "SKILL.md").is_file()
        assert not (folder / "scripts" / "run.py").exists()
        assert result["skipped_code_files"] == ["scripts/run.py"]

        row = registry.get_installed("skill", entity["id"])
        assert row["skipped_code_files_json"] == ["scripts/run.py"]
        # The hash covers the installed subset only, so strict mode must not
        # produce a permanent "modified" flag.
        current = registry.entity_file_manifest(
            "skill", row["runtime_slug"], row["files_json"]
        )
        assert current == row["files_json"]
        assert registry.tree_sha256("skill", row["runtime_slug"], current) == row["tree_sha256"]

    def test_strict_mode_rejects_code_only_entity(self, catalog_server, marketplace_data_dir):
        from core.marketplace.installer import InstallError

        base_url, _entity, _package = catalog_server
        code_only, _bytes = make_entity(
            entity_id="code_only", files={"scripts/run.py": "print(1)\n"}
        )
        with pytest.raises(InstallError) as excinfo:
            _install(base_url, code_only, allow_code=False)
        assert excinfo.value.code == "nothing_to_install"


class TestValidation:
    def test_disabled_marketplace_refuses(self, catalog_server, marketplace_data_dir):
        from core.marketplace import config as marketplace_config
        from core.marketplace.installer import InstallError

        marketplace_config.save_settings(enabled=False)
        base_url, entity, _package = catalog_server
        with pytest.raises(InstallError) as excinfo:
            _install(base_url, entity)
        assert excinfo.value.code == "marketplace_disabled"

    def test_incompatible_platform_refuses(self, catalog_server, marketplace_data_dir):
        from core.marketplace.installer import InstallError

        base_url, _entity, _package = catalog_server
        future, _bytes = make_entity(
            entity_id="future_skill", platform={"min": "99.0.0"}
        )
        with pytest.raises(InstallError) as excinfo:
            _install(base_url, future)
        assert excinfo.value.code == "platform_incompatible"
        assert "99.0.0" in excinfo.value.message

    def test_broken_hash_refuses_before_unpacking(self, catalog_server, marketplace_data_dir):
        from core.marketplace.installer import InstallError

        base_url, entity, _package = catalog_server
        tampered = dict(entity)
        tampered["package"] = dict(entity["package"], sha256="0" * 64)
        with pytest.raises(InstallError) as excinfo:
            _install(base_url, tampered)
        assert excinfo.value.code == "package_hash_mismatch"
        assert _skill_folders(marketplace_data_dir) == []

    def test_file_hash_mismatch_refuses(self, catalog_server, marketplace_data_dir):
        from core.marketplace.installer import InstallError

        base_url, entity, _package = catalog_server
        tampered = dict(entity)
        tampered["files"] = dict(entity["files"], **{"SKILL.md": "0" * 64})
        with pytest.raises(InstallError) as excinfo:
            _install(base_url, tampered)
        assert excinfo.value.code == "file_hash_mismatch"
        assert _skill_folders(marketplace_data_dir) == []

    def test_missing_file_manifest_refuses(self, catalog_server, marketplace_data_dir):
        from core.marketplace.installer import InstallError

        base_url, entity, _package = catalog_server
        empty = dict(entity, files={})
        with pytest.raises(InstallError) as excinfo:
            _install(base_url, empty)
        assert excinfo.value.code == "missing_file_manifest"


class TestRollbackOnFailure:
    def test_failure_midway_leaves_no_trace(self, catalog_server, marketplace_data_dir, monkeypatch):
        from core.marketplace import registry, store
        from core.marketplace.installer import InstallError, install_skill
        from core.marketplace.source import MarketplaceSource

        base_url, entity, _package = catalog_server
        before_registry = Path(marketplace_data_dir) / "skills" / "skills.json"
        before_bytes = before_registry.read_bytes() if before_registry.is_file() else None

        real_write = store.atomic_write_json
        calls = {"count": 0}

        def _failing_write(path, data):
            # Let the run-log and backup metadata through, then fail exactly
            # when the registry file is rewritten.
            calls["count"] += 1
            if str(path).endswith("skills.json") and calls["count"] > 2:
                raise OSError("disk full")
            return real_write(path, data)

        monkeypatch.setattr(store, "atomic_write_json", _failing_write)
        with pytest.raises(InstallError):
            install_skill(entity, channel="main", source=MarketplaceSource(base_url))

        assert _skill_folders(marketplace_data_dir) == []
        assert registry.get_installed("skill", entity["id"]) is None
        after_bytes = before_registry.read_bytes() if before_registry.is_file() else None
        assert after_bytes == before_bytes

    def test_failed_run_is_logged(self, catalog_server, marketplace_data_dir):
        from core.marketplace import store
        from core.marketplace.installer import InstallError

        base_url, entity, _package = catalog_server
        tampered = dict(entity)
        tampered["package"] = dict(entity["package"], sha256="0" * 64)
        with pytest.raises(InstallError):
            _install(base_url, tampered)

        runs = store.load_history()
        assert runs and runs[-1]["status"] == "failed"
        assert runs[-1]["failure_reason"]


class TestRecovery:
    def test_recover_rolls_back_interrupted_run(self, catalog_server, marketplace_data_dir):
        from core.marketplace import bootstrap, registry, store

        base_url, entity, _package = catalog_server
        install_id = "20261027T120000Z-dead"
        store.prepare_run_dirs(install_id)
        store.write_run_meta(install_id, {
            "install_id": install_id,
            "type": "skill",
            "id": entity["id"],
            "runtime_slug": "demo_skill",
            "created_folder": True,
            "created_files": [],
        })
        # A half-written folder stands in for a process killed mid-install.
        half = Path(marketplace_data_dir) / "skills" / "demo_skill"
        half.mkdir(parents=True, exist_ok=True)
        (half / "SKILL.md").write_text("partial", encoding="utf-8")
        store.append_run({
            "install_id": install_id, "type": "skill", "id": entity["id"],
            "status": "applying",
        })

        reports = bootstrap.recover()

        assert len(reports) == 1 and reports[0]["ok"] is True
        assert not half.exists()
        assert registry.get_installed("skill", entity["id"]) is None
        assert store.get_state() is None
        assert store.applying_runs() == []

    def test_recover_marks_failures(self, marketplace_data_dir):
        from core.marketplace import bootstrap, store

        # A run with no backup metadata cannot be undone; it must be marked
        # failed rather than silently forgotten.
        install_id = "20261027T130000Z-beef"
        store.ensure_layout()
        store.append_run({
            "install_id": install_id, "type": "skill", "id": "ghost",
            "status": "applying",
        })
        reports = bootstrap.recover()
        assert reports[0]["ok"] is False
        assert store.find_run(install_id)["status"] == "failed"

    def test_bootstrap_is_idempotent(self, catalog_server, marketplace_data_dir):
        from core.marketplace import bootstrap

        first = bootstrap.bootstrap()
        second = bootstrap.bootstrap()
        assert first["ok"] is True
        assert second["ok"] is True
        assert second["recovered"] == []


class TestLicence:
    def test_acceptance_required_refuses_without_acceptance(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.installer import InstallError

        entity, package = make_entity(license_acceptance_required=True)
        root = make_catalog(tmp_path / "catalog", [(entity, package)])
        with pytest.raises(InstallError) as excinfo:
            _install(serve_catalog(root), entity)
        assert excinfo.value.code == "license_not_accepted"

    def test_acceptance_is_recorded(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import registry

        entity, package = make_entity(license_acceptance_required=True)
        root = make_catalog(tmp_path / "catalog", [(entity, package)])
        _install(serve_catalog(root), entity, accept_license=True)

        row = registry.get_installed("skill", entity["id"])
        # Acceptance lives on the registry row, not in widget state: it has
        # to survive a restart.
        assert row["license_accepted_at"]

    def test_no_acceptance_recorded_when_not_required(
        self, catalog_server, marketplace_data_dir
    ):
        from core.marketplace import registry

        base_url, entity, _package = catalog_server
        _install(base_url, entity)
        assert registry.get_installed("skill", entity["id"])["license_accepted_at"] is None
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
