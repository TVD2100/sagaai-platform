"""
Update discovery and application (P3).

The check decides between four outcomes for one installed entity: an
update, a metadata refresh, a platform refusal and a re-stamp that is not
shown at all. Each test pins one of them, because telling them apart is
the whole job of ``check_entity_updates()``.

``verify_installed()`` is checked separately: it must distinguish a clean
installation, an edited file and a deleted file, and it must stay quiet
for a strict-mode installation whose code files were never placed on
disk.
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


# ─── Helpers ──────────────────────────────────────────────────────────────

def _settings(monkeypatch, **overrides):
    """Patch the marketplace settings for one test.

    Without this the check reads the developer's own config file, so a
    machine with the section switched off would fail the tests for a
    reason that has nothing to do with the code under test.
    """
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


def _install(base_url, entity, *, allow_code=True):
    """Install one entity through the real installer."""
    from core.marketplace.installer import install_skill
    from core.marketplace.source import MarketplaceSource

    return install_skill(
        entity,
        channel="main",
        allow_code=allow_code,
        source=MarketplaceSource(base_url),
    )


def _source(base_url):
    """Return a catalog transport bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


def _data_dir():
    """Return the live data directory of the current test."""
    from core.marketplace import store

    return store.data_dir()


# ─── Discovery ────────────────────────────────────────────────────────────

def test_newer_catalog_version_is_reported_as_an_update(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)

    newer, newer_package = make_entity(
        version="1.1.0", files={"SKILL.md": "# demo_skill v1.1\n"}
    )
    add_catalog_version(catalog_root, newer, newer_package)

    from core.marketplace import updates

    report = updates.check_entity_updates(source=_source(base_url))

    assert report["ok"] is True
    assert report["checked"] == 1
    assert report["total"] == 1
    item = report["skill"][0]
    assert item["id"] == "demo_skill"
    assert item["installed_version"] == "1.0.0"
    assert item["version"] == "1.1.0"
    assert item["action"] == "update"
    assert item["requires_choice"] is False


def test_same_version_with_a_newer_manifest_is_only_a_metadata_refresh(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)

    refreshed, refreshed_package = make_entity(
        version="1.0.0",
        extra={"updated_at": "2030-01-01T00:00:00Z"},
    )
    add_catalog_version(catalog_root, refreshed, refreshed_package)

    from core.marketplace import updates

    report = updates.check_entity_updates(source=_source(base_url))

    item = report["skill"][0]
    assert item["action"] == "metadata"
    # Nothing is downloaded for a metadata refresh: the entry carries no
    # file list and no size that a button could act on.
    assert "replaces_files" not in item


def test_an_entity_that_needs_a_newer_platform_is_not_offered_as_an_update(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)

    newer, newer_package = make_entity(
        version="2.0.0", platform={"min": "99.0.0"}
    )
    add_catalog_version(catalog_root, newer, newer_package)

    from core.marketplace import updates

    report = updates.check_entity_updates(source=_source(base_url))

    item = report["skill"][0]
    assert item["action"] == "platform_required"
    assert "99.0.0" in item["required_platform"]


def test_edited_files_turn_an_update_into_a_choice(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    folder = Path(_data_dir()) / "skills" / "demo_skill"
    (folder / "SKILL.md").write_text("my own words", encoding="utf-8")

    newer, newer_package = make_entity(
        version="2.0.0",
        files={
            "SKILL.md": "# demo_skill v2\n",
            "references/notes.md": "v2 notes\n",
            "scripts/run.py": "print('demo v2')\n",
        },
    )
    add_catalog_version(catalog_root, newer, newer_package)

    from core.marketplace import updates

    report = updates.check_entity_updates(source=_source(base_url))

    item = report["skill"][0]
    assert item["action"] == "update"
    assert item["user_modified"] is True
    assert item["requires_choice"] is True
    assert item["modified_files"] == ["SKILL.md"]


def test_identical_files_restamp_the_row_instead_of_offering_an_update(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """D-22: a bumped version with the same content is not an update."""
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)

    # Same files, same bytes, only the declared version differs.
    bumped, bumped_package = make_entity(version="1.0.1")
    add_catalog_version(catalog_root, bumped, bumped_package)

    from core.marketplace import registry, updates

    report = updates.check_entity_updates(source=_source(base_url))

    assert report["skill"] == []
    assert report["restamped"] == [
        {"id": "demo_skill", "from": "1.0.0", "to": "1.0.1"}
    ]
    row = registry.get_installed("skill", "demo_skill")
    assert row["version"] == "1.0.1"


def test_pinned_and_removed_entities_are_not_checked(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)

    newer, newer_package = make_entity(version="2.0.0")
    add_catalog_version(catalog_root, newer, newer_package)

    from core.marketplace import registry, updates

    registry.update_installed("skill", "demo_skill", pinned=True)
    report = updates.check_entity_updates(source=_source(base_url))
    assert report["skipped"]["pinned"] == 1
    assert report["skill"] == []

    registry.update_installed("skill", "demo_skill", pinned=False)
    registry.mark_removed("skill", "demo_skill", "demo_skill")
    report = updates.check_entity_updates(source=_source(base_url))
    assert report["skipped"]["removed"] == 1
    assert report["skill"] == []
    registry.unmark_removed("skill", "demo_skill")


def test_a_disabled_marketplace_refuses_to_check(
    marketplace_data_dir, monkeypatch
):
    _settings(monkeypatch, enabled=False)

    from core.marketplace import store, updates

    with pytest.raises(store.MarketplaceError) as caught:
        updates.check_entity_updates()

    assert caught.value.code == "marketplace_disabled"


# ─── verify_installed ─────────────────────────────────────────────────────

def test_verify_installed_tells_clean_changed_and_deleted_apart(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    folder = Path(_data_dir()) / "skills" / "demo_skill"

    from core.marketplace import updates

    clean = updates.verify_installed("skill")
    assert clean["ok"] is True
    assert clean["checked"] == 1
    assert clean["clean"] == 1
    assert clean["modified"] == []

    (folder / "SKILL.md").write_text("edited", encoding="utf-8")
    changed = updates.verify_installed("skill")
    assert changed["clean"] == 0
    assert changed["modified"][0]["files"] == ["SKILL.md"]

    (folder / "references" / "notes.md").unlink()
    deleted = updates.verify_installed("skill")
    files = set(deleted["modified"][0]["files"])
    assert "references/notes.md" in files
    assert "references/notes.md" in set(deleted["modified"][0]["missing"])


def test_verify_installed_is_quiet_for_a_strict_mode_installation(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """``allow_code = false`` must not report the skipped code as modified."""
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity, allow_code=False)
    folder = Path(_data_dir()) / "skills" / "demo_skill"
    assert not (folder / "scripts" / "run.py").exists()

    from core.marketplace import updates

    report = updates.verify_installed("skill")

    assert report["clean"] == 1
    assert report["modified"] == []


def test_verify_installed_can_record_the_state_on_the_row(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    folder = Path(_data_dir()) / "skills" / "demo_skill"
    (folder / "SKILL.md").write_text("edited", encoding="utf-8")

    from core.marketplace import registry, updates

    assert registry.get_installed("skill", "demo_skill")["user_modified"] is False
    updates.verify_installed("skill", record=True)
    row = registry.get_installed("skill", "demo_skill")
    assert row["user_modified"] is True
    assert row["user_modified_files_json"] == ["SKILL.md"]


# ─── Applying ─────────────────────────────────────────────────────────────

def test_apply_updates_changes_the_file_and_the_recorded_version(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    folder = Path(_data_dir()) / "skills" / "demo_skill"

    newer, newer_package = make_entity(
        version="1.1.0",
        files={
            "SKILL.md": "# demo_skill v1.1\n",
            "references/notes.md": "v1.1 notes\n",
            "scripts/run.py": "print('demo v1.1')\n",
        },
    )
    add_catalog_version(catalog_root, newer, newer_package)

    from core.marketplace import registry, updates

    report = updates.apply_entity_updates(
        ["demo_skill"], "keep_mine", source=_source(base_url)
    )

    assert report["ok"] is True
    assert len(report["applied"]) == 1
    assert report["applied"][0]["version"] == "1.1.0"
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == "# demo_skill v1.1\n"
    assert registry.get_installed("skill", "demo_skill")["version"] == "1.1.0"
    run = report["applied"][0]["install_id"]
    assert run


def test_apply_keep_mine_leaves_the_edited_file_alone(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    folder = Path(_data_dir()) / "skills" / "demo_skill"
    (folder / "SKILL.md").write_text("my own words", encoding="utf-8")

    newer, newer_package = make_entity(
        version="2.0.0",
        files={
            "SKILL.md": "# demo_skill v2\n",
            "references/notes.md": "v2 notes\n",
            "scripts/run.py": "print('demo v2')\n",
        },
    )
    add_catalog_version(catalog_root, newer, newer_package)

    from core.marketplace import updates

    report = updates.apply_entity_updates(
        ["demo_skill"], "keep_mine", source=_source(base_url)
    )

    assert report["ok"] is True
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == "my own words"
    assert (folder / "references/notes.md").read_text(encoding="utf-8") == "v2 notes\n"


def test_apply_skip_policy_reports_the_entity_as_skipped(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    entity, package = make_entity(version="1.0.0")
    catalog_root = make_catalog(tmp_path / "catalog", [(entity, package)])
    base_url = serve_catalog(catalog_root)
    _install(base_url, entity)
    folder = Path(_data_dir()) / "skills" / "demo_skill"
    (folder / "SKILL.md").write_text("my own words", encoding="utf-8")

    newer, newer_package = make_entity(version="2.0.0")
    add_catalog_version(catalog_root, newer, newer_package)

    from core.marketplace import updates

    report = updates.apply_entity_updates(
        ["demo_skill"], "skip", source=_source(base_url)
    )

    assert report["applied"] == []
    assert report["skipped"][0]["reason"] == "user_modified"
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == "my own words"


def test_apply_reports_an_unknown_entity_without_failing_the_batch(
    marketplace_data_dir, monkeypatch
):
    _settings(monkeypatch)

    from core.marketplace import updates

    report = updates.apply_entity_updates(["nowhere"], "keep_mine")

    assert report["ok"] is False
    assert report["failed"][0]["error_code"] == "not_installed"


def test_the_check_covers_every_entity_type(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """One installed entity of each type produces one row in its section.

    The check used to stop at skills: the other types were skipped as
    unsupported, so their sections stayed empty even when the catalog had
    a newer release. The registry rows are written directly because the
    test is about discovery, not about the four installers.
    """
    from tests.marketplace_fixtures import make_installed_row

    _settings(monkeypatch)
    entities = []
    for type_ in ("skill", "agent", "rag", "mcp"):
        entity_id = f"demo_{type_}"
        make_installed_row(entity_id, type_=type_, version="1.0.0")
        newer, package = make_entity(
            entity_id=entity_id, version="2.0.0", type_=type_,
            files={} if type_ == "mcp" else {"entity.json": "{}\n"},
        )
        entities.append((newer, package))

    catalog_root = make_catalog(tmp_path / "catalog", entities)
    base_url = serve_catalog(catalog_root)

    from core.marketplace import updates

    report = updates.check_entity_updates(source=_source(base_url))

    assert report["ok"] is True
    for type_ in ("skill", "agent", "rag", "mcp"):
        section = report[type_]
        assert [item["type"] for item in section] == [type_]
        assert section[0]["id"] == f"demo_{type_}"
        assert section[0]["action"] == "update"
    assert len(report["items"]) == 4
    assert report["total"] == 4


def test_an_unknown_policy_is_refused(marketplace_data_dir, monkeypatch):
    _settings(monkeypatch)

    from core.marketplace import updates

    with pytest.raises(updates.UpdateError) as caught:
        updates.apply_entity_updates(["demo_skill"], "merge")

    assert caught.value.code == "invalid_policy"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
