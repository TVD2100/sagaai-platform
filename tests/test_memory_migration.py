# -*- coding: utf-8 -*-
"""
Tests for moving legacy working-data folders into the central root.

A migration must be safe by construction: the plan is read-only, a copy is
verified before it is reported as done, the legacy folder is never deleted,
and a target that already holds files is reported as a conflict instead of
being merged silently.
"""
import os

import pytest

import core.paths
from core import memory_migration as mm
from core import orchestrator_folders as of


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolated central root, workspace and a neutralised record lookup."""
    central = tmp_path / "agents_data"
    central.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(core.paths, "AGENTS_DATA_DIR", str(central))
    # The DEFAULT branch is what a legacy folder is compared against.
    import core.orchestrators as om
    monkeypatch.setattr(om, "get_orchestrator", lambda slug: None, raising=False)
    return {"central": central, "workspace": workspace}


def _legacy(sandbox, slug: str, files: dict) -> str:
    """Create a legacy <slug>_data folder with the given files."""
    folder = sandbox["workspace"] / f"{slug}_data"
    folder.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return str(folder)


# ─── Planning ────────────────────────────────────────────────────────────────


def test_plan_reports_a_ready_migration(sandbox):
    folder = _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    assert entry["status"] == mm.STATUS_READY
    assert entry["source"] == folder
    assert entry["target"] == str(sandbox["central"] / "marketer")
    assert entry["files"] == 1
    assert entry["target_files"] == 0


def test_plan_is_read_only(sandbox):
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    before = sorted(os.listdir(str(sandbox["central"])))
    mm.plan_migration("marketer", str(sandbox["workspace"]))
    assert sorted(os.listdir(str(sandbox["central"]))) == before


def test_plan_without_a_legacy_folder_has_no_source(sandbox):
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    assert entry["status"] == mm.STATUS_NO_SOURCE
    assert entry["files"] == 0


def test_plan_marks_an_empty_legacy_folder(sandbox):
    (sandbox["workspace"] / "marketer_data").mkdir()
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    assert entry["status"] == mm.STATUS_EMPTY


def test_plan_reports_a_conflict_when_the_target_holds_files(sandbox):
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    other = sandbox["central"] / "marketer"
    other.mkdir()
    (other / "other.md").write_text("# другое", encoding="utf-8")
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    assert entry["status"] == mm.STATUS_CONFLICT
    assert entry["target_files"] == 1


def test_plan_detects_an_already_migrated_folder(sandbox):
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    plan = mm.plan_migration("marketer", str(sandbox["workspace"]))
    assert mm.apply_migration(plan)["ok"] is True
    again = mm.plan_migration("marketer", str(sandbox["workspace"]))
    assert again["status"] == mm.STATUS_ALREADY


def test_plan_skips_a_role_that_uses_the_legacy_folder(sandbox):
    """A role deliberately kept in the project must not be "fixed"."""
    folder = _legacy(sandbox, "editor_assistant", {"registry.md": "# р"})
    resolved = of.resolve_data_dir(
        "editor_assistant", str(sandbox["workspace"]),
        prompt_text="MEMORY_DIR: .", config={})
    assert resolved["path"] == str(sandbox["workspace"])
    entry = mm.plan_migration("editor_assistant", str(sandbox["workspace"]))
    assert entry["status"] == mm.STATUS_READY
    assert entry["source"] == folder


def test_plan_migrations_finds_unknown_legacy_folders(sandbox):
    """Data of a deleted employee is still reported, not skipped."""
    _legacy(sandbox, "deleted_role", {"registry.md": "# р"})
    plan = mm.plan_migrations(str(sandbox["workspace"]), include_known=False)
    assert [e["slug"] for e in plan] == ["deleted_role"]


def test_plan_migrations_ignores_unrelated_folders(sandbox):
    (sandbox["workspace"] / "src").mkdir()
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    plan = mm.plan_migrations(str(sandbox["workspace"]), include_known=False)
    assert [e["slug"] for e in plan] == ["marketer"]


def _install_tree(tmp_path, monkeypatch):
    """Build a tree where the central root sits INSIDE the scanned workspace.

    The default ``sandbox`` fixture keeps them as siblings, which never
    exercises the ``_data`` name collision: the central root is called
    ``agents_data`` and so looks exactly like a legacy folder of an employee
    called ``agents``.
    """
    install = tmp_path / "install"
    install.mkdir()
    central = install / "agents_data"
    # A DIFFERENT employee occupies the central root, so the legacy folder
    # created by a test never collides with an already populated target.
    (central / "dev_agent").mkdir(parents=True)
    (central / "dev_agent" / "registry.md").write_text("# реестр", encoding="utf-8")
    monkeypatch.setattr(core.paths, "AGENTS_DATA_DIR", str(central))
    return install, central


def test_plan_migrations_ignores_the_central_root(tmp_path, monkeypatch):
    """The central root is not a legacy folder of an employee called agents."""
    install, _central = _install_tree(tmp_path, monkeypatch)
    plan = mm.plan_migrations(str(install), include_known=False)
    assert [e["slug"] for e in plan] == []


def test_plan_migrations_still_finds_a_legacy_folder_beside_the_central_root(
        tmp_path, monkeypatch):
    """Skipping the central root must not hide real legacy data next to it."""
    install, _central = _install_tree(tmp_path, monkeypatch)
    # A role with no stored record: the resolver then falls back to the
    # (patched) central root, instead of reading a live employee's own path.
    legacy = install / "retired_role_data"
    legacy.mkdir()
    (legacy / "registry.md").write_text("# старый реестр", encoding="utf-8")
    plan = mm.plan_migrations(str(install), include_known=False)
    assert [e["slug"] for e in plan] == ["retired_role"]
    assert plan[0]["status"] == mm.STATUS_READY


# ─── Applying ────────────────────────────────────────────────────────────────


def test_apply_copies_nested_files(sandbox):
    _legacy(sandbox, "marketer", {
        "registry.md": "# реестр",
        "items/a/card.md": "# карточка",
        "items/b/card.md": "# карточка 2",
    })
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    result = mm.apply_migration(entry)
    assert result["ok"] is True
    assert result["copied"] == 3
    target = sandbox["central"] / "marketer"
    assert (target / "registry.md").read_text(encoding="utf-8") == "# реестр"
    assert (target / "items" / "a" / "card.md").exists()


def test_apply_keeps_the_source_folder(sandbox):
    folder = _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    mm.apply_migration(entry)
    assert os.path.isdir(folder)
    assert os.path.isfile(os.path.join(folder, "registry.md"))


def test_apply_dry_run_writes_nothing(sandbox):
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    result = mm.apply_migration(entry, dry_run=True)
    assert result["ok"] is True
    assert result["copied"] == 0
    assert not (sandbox["central"] / "marketer").exists()


def test_apply_refuses_a_conflict(sandbox):
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    target = sandbox["central"] / "marketer"
    target.mkdir()
    (target / "other.md").write_text("# другое", encoding="utf-8")
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    result = mm.apply_migration(entry)
    assert result["ok"] is False
    assert result["copied"] == 0
    assert (target / "registry.md").exists() is False


def test_apply_reports_the_kept_folder_in_the_reason(sandbox):
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    entry = mm.plan_migration("marketer", str(sandbox["workspace"]))
    result = mm.apply_migration(entry)
    assert "kept" in result["reason"]


def test_apply_plan_summarises_the_run(sandbox):
    _legacy(sandbox, "alpha", {"registry.md": "# a"})
    _legacy(sandbox, "beta", {"registry.md": "# b", "j.md": "# j"})
    plan = mm.plan_migrations(str(sandbox["workspace"]), include_known=False)
    result = mm.apply_plan(plan)
    assert result["ok"] is True
    assert result["total_copied"] == 3
    assert {o["slug"] for o in result["applied"]} == {"alpha", "beta"}


def test_apply_plan_reports_failures(sandbox):
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    target = sandbox["central"] / "marketer"
    target.mkdir()
    (target / "other.md").write_text("# другое", encoding="utf-8")
    plan = mm.plan_migrations(str(sandbox["workspace"]), include_known=False)
    result = mm.apply_plan(plan)
    assert result["ok"] is False
    assert len(result["failed"]) == 1


def test_apply_is_idempotent(sandbox):
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    first = mm.apply_migration(mm.plan_migration("marketer", str(sandbox["workspace"])))
    assert first["ok"] is True
    again = mm.apply_migration(mm.plan_migration("marketer", str(sandbox["workspace"])))
    assert again["ok"] is True
    assert again["copied"] == 0


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_is_a_dry_run_by_default(sandbox, capsys):
    import scripts.migrate_agent_data as cli
    _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    code = cli.main(["--workspace", str(sandbox["workspace"]),
                     "--slug", "marketer"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Dry run" in out
    assert not (sandbox["central"] / "marketer").exists()


def test_cli_apply_copies_and_keeps_the_source(sandbox, capsys):
    import scripts.migrate_agent_data as cli
    folder = _legacy(sandbox, "marketer", {"registry.md": "# реестр"})
    code = cli.main(["--workspace", str(sandbox["workspace"]),
                     "--slug", "marketer", "--apply"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Copied files: 1" in out
    assert (sandbox["central"] / "marketer" / "registry.md").exists()
    assert os.path.isdir(folder)


def test_cli_reports_an_empty_plan(sandbox, capsys):
    import scripts.migrate_agent_data as cli
    code = cli.main(["--workspace", str(sandbox["workspace"]), "--slug", "nobody"])
    assert code == 0
    assert "No legacy working-data folders found." in capsys.readouterr().out
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
