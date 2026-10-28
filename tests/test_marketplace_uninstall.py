"""
test_marketplace_uninstall.py - removal must stick.

A skill the user deleted has to stay deleted: the default importer runs on
every start and would otherwise restore it. The tests cover the three
effects of a removal - files gone, records gone, marker written - plus the
start-up path that must honour the marker.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


def _install(base_url, entity, **kwargs):
    from core.marketplace.installer import install_skill
    from core.marketplace.source import MarketplaceSource

    return install_skill(entity, channel="main", source=MarketplaceSource(base_url), **kwargs)


def _install_demo(marketplace_data_dir, serve_catalog, tmp_path):
    entity, package = make_entity()
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    report = _install(serve_catalog(catalog_root), entity)
    return entity, report, Path(marketplace_data_dir) / "skills" / report["runtime_slug"]


def test_uninstall_removes_files_records_and_writes_a_marker(
    marketplace_data_dir, serve_catalog, tmp_path
):
    from core.marketplace import conflicts, registry
    from core.skills_library import list_skills

    entity, report, folder = _install_demo(marketplace_data_dir, serve_catalog, tmp_path)
    assert folder.is_dir()
    assert _has_skill(report["skill_id"]) is True

    result = conflicts.uninstall("skill", "demo_skill")

    assert result["removed"] is True
    assert result["files_removed"] is True
    assert not folder.exists()
    assert registry.get_installed("skill", "demo_skill") is None
    assert _has_skill(report["skill_id"]) is False
    assert registry.is_removed("skill", "demo_skill") is True
    assert registry.list_removed("skill")[0]["runtime_slug"] == report["runtime_slug"]
    assert list_skills() == [] or all(
        skill.get("folder") != report["runtime_slug"] for skill in list_skills()
    )


def test_uninstall_is_idempotent(marketplace_data_dir, serve_catalog, tmp_path):
    from core.marketplace import conflicts

    _install_demo(marketplace_data_dir, serve_catalog, tmp_path)
    conflicts.uninstall("skill", "demo_skill")

    second = conflicts.uninstall("skill", "demo_skill")

    assert second["ok"] is True
    assert second["removed"] is False


def test_removal_marker_survives_a_default_import(marketplace_data_dir, monkeypatch):
    """The start-up importer must not resurrect a deleted skill."""
    from core.marketplace import conflicts, registry

    # Register a folder that looks like the bundled default skill, then
    # remove it through the marketplace and let the importer run again.
    from core.skills_library import register_skill, ensure_skills_root

    folder_name = "rag_base_creator"
    target = Path(ensure_skills_root()) / folder_name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text("# Rag Base Creator\n", encoding="utf-8")
    skill_id = register_skill("Rag Base Creator", "creates bases", folder_name)

    now = __import__("core.marketplace.store", fromlist=["isoformat_utc"]).isoformat_utc()
    manifest = {"SKILL.md": __import__("core.marketplace.registry", fromlist=["_sha256_file"])._sha256_file(str(target / "SKILL.md"))}
    registry.upsert_installed({
        "type": "skill",
        "id": "marketplace/rag_base_creator",
        "runtime_slug": folder_name,
        "runtime_skill_id": skill_id,
        "version": "1.0.0",
        "channel": "main",
        "source_url": "skills/rag_base_creator/1.0.0",
        "package_sha256": "0" * 64,
        "tree_sha256": registry.tree_sha256("skill", folder_name, manifest),
        "files_json": manifest,
        "skipped_code_files_json": [],
        "installed_at": now,
        "updated_at": now,
        "installed_by": "marketplace",
    })

    conflicts.uninstall("skill", "marketplace/rag_base_creator")
    assert not target.exists()

    from core.default_imports import ensure_default_skills

    results = ensure_default_skills()

    assert results.get(folder_name) == "exists"
    assert not target.exists()


def test_removal_marker_is_cleared_by_a_new_install(
    marketplace_data_dir, serve_catalog, tmp_path
):
    from core.marketplace import conflicts, registry

    entity, report, folder = _install_demo(marketplace_data_dir, serve_catalog, tmp_path)
    conflicts.uninstall("skill", "demo_skill")
    assert registry.is_removed("skill", "demo_skill") is True

    catalog_root = make_catalog(tmp_path / "second", [(entity, make_entity()[1])])
    _install(serve_catalog(catalog_root), entity)

    # Reinstalling the entity is an explicit act, so the removal marker is
    # gone and the next start-up import no longer suppresses it.
    assert registry.get_installed("skill", "demo_skill") is not None
    assert registry.is_removed("skill", "demo_skill") is False
    assert folder.is_dir()


def _has_skill(skill_id: str) -> bool:
    from core.skills_library import list_skills

    return any(str(skill.get("id")) == str(skill_id) for skill in list_skills())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
