"""
test_marketplace_local_mods.py - what counts as a local edit.

The detector decides whether an update may proceed silently, so it must be
precise in both directions: a changed package file is an edit, and a file
the user added next to it is not. Each test asserts the flag and the file
list, because the list is what the card shows.
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


def _installed(marketplace_data_dir, serve_catalog, tmp_path, **kwargs):
    """Install the demo skill and return ``(entity, folder)``."""
    entity, package = make_entity()
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    report = _install(serve_catalog(catalog_root), entity, **kwargs)
    return entity, Path(marketplace_data_dir) / "skills" / report["runtime_slug"]


def test_editing_a_package_file_is_detected(marketplace_data_dir, serve_catalog, tmp_path):
    from core.marketplace import conflicts

    entity, folder = _installed(marketplace_data_dir, serve_catalog, tmp_path)
    assert conflicts.detect_local_modifications("skill", folder.name)["modified"] is False

    (folder / "SKILL.md").write_text("rewritten", encoding="utf-8")
    result = conflicts.detect_local_modifications("skill", folder.name)

    assert result["modified"] is True
    assert result["files"] == ["SKILL.md"]
    assert result["missing"] == []


def test_deleting_a_package_file_is_detected(marketplace_data_dir, serve_catalog, tmp_path):
    from core.marketplace import conflicts

    entity, folder = _installed(marketplace_data_dir, serve_catalog, tmp_path)
    (folder / "references/notes.md").unlink()

    result = conflicts.detect_local_modifications("skill", folder.name)

    assert result["modified"] is True
    assert result["missing"] == ["references/notes.md"]


def test_adding_a_user_file_is_not_an_edit(marketplace_data_dir, serve_catalog, tmp_path):
    from core.marketplace import conflicts

    entity, folder = _installed(marketplace_data_dir, serve_catalog, tmp_path)
    (folder / "my-notes.md").write_text("personal note", encoding="utf-8")
    (folder / "references/local.md").write_text("more notes", encoding="utf-8")

    result = conflicts.detect_local_modifications("skill", folder.name)

    assert result["modified"] is False
    assert result["files"] == []


def test_registry_bookkeeping_is_not_an_edit(marketplace_data_dir, serve_catalog, tmp_path):
    """Changing a display field of the skill record is not a file edit."""
    from core.marketplace import conflicts
    from core.skills_library import update_skill

    entity, folder = _installed(marketplace_data_dir, serve_catalog, tmp_path)
    skill_id = _skill_id(folder.name)
    update_skill(skill_id, name="Renamed by the user")

    result = conflicts.detect_local_modifications("skill", folder.name)

    assert result["modified"] is False


def test_allow_code_false_does_not_report_a_permanent_edit(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """A skipped ``scripts/`` file must not look like a modification."""
    from core.marketplace import conflicts, config as marketplace_config

    monkeypatch.setattr(
        marketplace_config, "marketplace_settings",
        lambda: {"enabled": True, "base_url": "", "allow_code": False,
                 "require_signature": False, "channel": "main"},
    )
    entity, folder = _installed(marketplace_data_dir, serve_catalog, tmp_path)
    assert not (folder / "scripts/run.py").exists()

    result = conflicts.detect_local_modifications("skill", folder.name)

    assert result["modified"] is False


def test_detection_after_a_keep_mine_round_keeps_the_list(
    marketplace_data_dir, serve_catalog, tmp_path
):
    """Files the user kept stay listed even when they match the new version."""
    from core.marketplace import conflicts
    from tests.marketplace_fixtures import add_catalog_version

    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    report = _install(base_url, entity)
    folder = Path(marketplace_data_dir) / "skills" / report["runtime_slug"]
    (folder / "SKILL.md").write_text("my own instructions", encoding="utf-8")

    newer, newer_package = make_entity(
        entity_id="demo_skill", version="2.0.0",
        files={"SKILL.md": "# v2\n", "references/notes.md": "v2 notes\n"},
    )
    add_catalog_version(catalog_root, newer, newer_package)
    conflicts.resolve_three_way(
        "keep_mine", newer, channel="main",
        source=_source(base_url),
    )

    result = conflicts.detect_local_modifications("skill", folder.name)
    assert result["modified"] is True
    assert "SKILL.md" in result["files"]


def _source(base_url):
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


def _skill_id(folder_name: str) -> str:
    from core.skills_library import list_skills

    for skill in list_skills():
        if str(skill.get("folder") or "") == folder_name:
            return str(skill["id"])
    raise AssertionError(f"skill {folder_name} is not registered")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
