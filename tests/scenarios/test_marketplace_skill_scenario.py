"""
Marketplace skill scenario - the full path a user walks.

Given a catalog that holds one skill, the user installs it, sees it in the
skills library, tries the same install again, edits a file, rolls the run
back and returns to the initial state. The point of this file is to drive
the real modules together - catalog, installer, registry, rollback - and
assert on what the user can actually observe on disk and in the registry,
not on internal calls.

Later slices extend this scenario with the conflict paths and uninstall;
the steps that need them are added there, so this file always describes
behaviour that exists.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


def _install(base_url: str, entity: dict, **kwargs):
    """Install one skill from a local catalog."""
    from core.marketplace.installer import install_skill
    from core.marketplace.source import MarketplaceSource

    return install_skill(
        entity, channel="main", source=MarketplaceSource(base_url), **kwargs
    )


def _skill_folder(data_dir, slug: str) -> Path:
    return Path(data_dir) / "skills" / slug


def _registry_record(data_dir, slug: str) -> dict:
    """Return the ``skills.json`` record of the installed skill."""
    path = Path(data_dir) / "skills" / "skills.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for record in data.values():
        if record.get("folder") == slug:
            return record
    return {}


def test_marketplace_skill_lifecycle(marketplace_data_dir, serve_catalog, tmp_path):
    """Install, reinstall, edit, roll back - the state follows the user."""
    from core.marketplace import registry, rollback
    from core.marketplace import store as marketplace_store

    data_dir = marketplace_data_dir
    entity, package = make_entity(entity_id="scenario_skill")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)

    # ── Given: the skill is not installed ────────────────────────────────
    assert registry.get_installed("skill", "scenario_skill") is None

    # ── When: the user installs it ──────────────────────────────────────
    result = _install(base_url, entity)
    slug = result["runtime_slug"]
    folder = _skill_folder(data_dir, slug)

    # ── Then: the files, the type registry and the marketplace record agree
    assert (folder / "SKILL.md").is_file()
    record = _registry_record(data_dir, slug)
    assert record["version"] == "1.0.0"
    assert record["source"] == "marketplace/scenario_skill"
    assert record["sha256"] == entity["package"]["sha256"]
    installed = registry.get_installed("skill", "scenario_skill")
    assert installed["version"] == "1.0.0"
    assert installed["runtime_skill_id"] == result["skill_id"]

    # ── When: the user installs the same version again ──────────────────
    installed_at_before = registry.get_installed("skill", "scenario_skill")["installed_at"]
    folders_before = sorted(p.name for p in (Path(data_dir) / "skills").iterdir() if p.is_dir())
    second = _install(base_url, entity)

    # ── Then: nothing changed ───────────────────────────────────────────
    assert second["noop"] is True
    assert registry.get_installed("skill", "scenario_skill")["installed_at"] == installed_at_before
    folders_after = sorted(p.name for p in (Path(data_dir) / "skills").iterdir() if p.is_dir())
    assert folders_after == folders_before

    # ── When: the user edits a file of the skill ────────────────────────
    (folder / "SKILL.md").write_text("my own instructions", encoding="utf-8")

    # ── Then: the change is visible against the installed manifest ──────
    row = registry.get_installed("skill", "scenario_skill")
    changed = [
        rel_path for rel_path, digest in row["files_json"].items()
        if registry.entity_file_manifest("skill", slug, {rel_path: digest}).get(rel_path) != digest
    ]
    assert changed == ["SKILL.md"]

    # ── When: the user rolls the install back ──────────────────────────
    report = rollback.rollback_run(result["install_id"])

    # ── Then: files, registry row and run log return to the prior state ──
    assert report["ok"] is True
    assert not folder.exists()
    assert registry.get_installed("skill", "scenario_skill") is None
    assert marketplace_store.find_run(result["install_id"])["status"] == "rolled_back"


def test_rollback_restores_the_previous_installed_row(marketplace_data_dir, serve_catalog, tmp_path):
    """A second install of another version is undone to the first one.

    P1 has no in-place update: the catalog version is a separate folder so
    the user's copy is never overwritten behind their back. Undoing the
    second run must therefore remove only that folder and put the first
    registry row back.
    """
    from core.marketplace import registry, rollback
    from tests.marketplace_fixtures import add_catalog_version

    data_dir = marketplace_data_dir
    first_entity, first_package = make_entity(entity_id="versioned_skill", version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(first_entity, first_package)])
    base_url = serve_catalog(catalog_root)
    first = _install(base_url, first_entity)
    first_slug = first["runtime_slug"]
    first_folder = _skill_folder(data_dir, first_slug)
    assert (first_folder / "SKILL.md").is_file()

    second_entity, second_package = make_entity(
        entity_id="versioned_skill",
        version="2.0.0",
        files={"SKILL.md": "# v2\n", "references/notes.md": "v2 notes\n"},
    )
    add_catalog_version(catalog_root, second_entity, second_package)

    second = _install(base_url, second_entity)
    second_slug = second["runtime_slug"]
    second_folder = _skill_folder(data_dir, second_slug)

    # The second install got its own folder and took over the registry row.
    assert second_slug != first_slug
    assert (second_folder / "SKILL.md").read_text(encoding="utf-8") == "# v2\n"
    assert registry.get_installed("skill", "versioned_skill")["version"] == "2.0.0"

    rollback.rollback_run(second["install_id"])

    # Only the second run is undone: the first installation is intact and
    # the registry row points at it again, byte for byte.
    assert not second_folder.exists()
    assert (first_folder / "SKILL.md").is_file()
    restored = registry.get_installed("skill", "versioned_skill")
    assert restored["version"] == "1.0.0"
    assert restored["runtime_slug"] == first_slug
    assert restored["install_id"] == first["install_id"]


def test_uninstall_keeps_the_skill_deleted(marketplace_data_dir, serve_catalog, tmp_path):
    """After removal the start-up importer must not bring the skill back."""
    from core.marketplace import conflicts, registry

    data_dir = marketplace_data_dir
    entity, package = make_entity(entity_id="removable_skill")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    first = _install(base_url, entity)
    slug = first["runtime_slug"]
    folder = _skill_folder(data_dir, slug)
    assert folder.is_dir()

    # ── When: the user removes it ────────────────────────────────────────
    report = conflicts.uninstall("skill", "removable_skill")

    # ── Then: files, records and a marker ───────────────────────────────
    assert report["removed"] is True
    assert not folder.exists()
    assert registry.get_installed("skill", "removable_skill") is None
    assert registry.is_removed("skill", "removable_skill") is True

    # ── When: the platform runs the default import again ─────────────────
    from core.default_imports import ensure_all_defaults

    ensure_all_defaults()

    # ── Then: the removed skill is not resurrected ──────────────────────
    assert not folder.exists()
    assert registry.get_installed("skill", "removable_skill") is None
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
