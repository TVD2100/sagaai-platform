# -*- coding: utf-8 -*-
"""
Tests for per-thread DevAgent workspace persistence.

Covers:
  * Thread creation with workspace / target_file.
  * Loading metadata back.
  * Updating the saved workspace via save_thread_workspace.
  * DevAgent repository functions directly.
  * Migration adds missing workspace/target_file columns.
"""
import importlib
import os
import sys

import pytest
from sqlalchemy import inspect, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point SagaAI at a fresh temp data dir and reset DB engines."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import storage.db as db_mod
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()

    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)

    yield

    db_mod.reset_engine()
    db_mod.reset_devagent_engine()


def _create_temp_workspaces(tmp_path):
    """Create two real workspace dirs and a target file for testing."""
    ws1 = tmp_path / "proj_one"
    ws2 = tmp_path / "proj_two"
    ws1.mkdir()
    ws2.mkdir()
    target = ws2 / "main.py"
    target.write_text("print('hello')", encoding="utf-8")
    return str(ws1), str(ws2), str(target)


def test_create_devagent_thread_saves_workspace(tmp_path):
    """create_devagent_thread should persist workspace/target_file metadata."""
    from core.threads_devagent import create_devagent_thread, load_thread_meta

    ws1, ws2, target = _create_temp_workspaces(tmp_path)

    tid = create_devagent_thread(
        title="test",
        orchestrator_slug="dev_agent",
        orchestrator_name="DevAgent",
        workspace=ws1,
        target_file=None,
    )
    meta = load_thread_meta(tid)
    assert meta["workspace"] == ws1
    assert meta["target_file"] is None

    tid2 = create_devagent_thread(
        title="single file",
        orchestrator_slug="dev_agent",
        orchestrator_name="DevAgent",
        workspace=ws2,
        target_file=target,
    )
    meta2 = load_thread_meta(tid2)
    assert meta2["workspace"] == ws2
    assert meta2["target_file"] == target


def test_save_thread_workspace_updates_last_workspace(tmp_path):
    """save_thread_workspace must overwrite previous workspace/target_file."""
    from core.threads_devagent import (
        create_devagent_thread, load_thread_meta, save_thread_workspace
    )

    ws1, ws2, target = _create_temp_workspaces(tmp_path)
    tid = create_devagent_thread(
        title="test",
        orchestrator_slug="orchestrator_x",
        orchestrator_name="Custom",
        workspace=ws1,
        target_file=None,
    )

    # Switch to another project and single-file mode.
    assert save_thread_workspace(tid, ws2, target) is True
    meta = load_thread_meta(tid)
    assert meta["workspace"] == ws2
    assert meta["target_file"] == target

    # Switch again, back to normal workspace mode.
    assert save_thread_workspace(tid, ws1, "") is True
    meta = load_thread_meta(tid)
    assert meta["workspace"] == ws1
    assert meta["target_file"] is None


def test_repo_devagent_create_thread_persists_columns(tmp_path):
    """Repository-level create must store the new fields directly."""
    from storage.repository_devagent import (
        repo_devagent_create_thread, repo_devagent_load_thread_meta
    )

    ws1, ws2, target = _create_temp_workspaces(tmp_path)
    ok = repo_devagent_create_thread(
        "tid_ws", "title", "slug", "Name", workspace=ws1, target_file=target
    )
    assert ok is True

    meta = repo_devagent_load_thread_meta("tid_ws")
    assert meta["workspace"] == ws1
    assert meta["target_file"] == target


def test_thread_columns_are_migrated(tmp_path):
    """
    If a DB already has the threads table but lacks workspace/target_file,
    creating the engine must add the missing columns without dropping rows.
    """
    import sqlite3
    from sqlalchemy import create_engine

    # Create a legacy-shaped table with an existing row.
    db_path = os.path.join(str(tmp_path), "migrate.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE threads (thread_id VARCHAR(64) PRIMARY KEY, "
        "skill_id VARCHAR(8), skill_name VARCHAR(256) NOT NULL DEFAULT '', "
        "title VARCHAR(256) NOT NULL DEFAULT '', type VARCHAR(32) NOT NULL DEFAULT 'chat', "
        "created_at VARCHAR(32) NOT NULL DEFAULT '', updated_at VARCHAR(32) NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO threads (thread_id) VALUES ('legacy_thread')"
    )
    conn.commit()
    conn.close()

    import storage.db as db_mod
    from storage.models import Base

    engine = create_engine(f"sqlite:///{db_path}")
    # Simulate the migration path used by get_engine().
    db_mod._migrate_threads_table_if_needed(engine)
    Base.metadata.create_all(engine)
    db_mod._ensure_thread_columns(engine)

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("threads")}
    assert "workspace" in cols
    assert "target_file" in cols

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM threads")).fetchall()
    assert len(rows) == 1
    assert rows[0].thread_id == "legacy_thread"


def test_to_dict_contains_new_columns(tmp_path):
    """Thread.to_dict() must expose workspace and target_file."""
    from storage.models import Thread

    obj = Thread(
        thread_id="tid", assistant_id=None, assistant_name="sn", title="t",
        type="chat", created_at="2024", updated_at="2024",
        workspace="/tmp/ws", target_file="/tmp/ws/a.py",
    )
    d = obj.to_dict()
    assert d["workspace"] == "/tmp/ws"
    assert d["target_file"] == "/tmp/ws/a.py"


# ─── Legacy meta migration (workspace isolation v2) ───────────────────────────

def test_migration_clears_platform_folders_for_foreign_threads(tmp_path):
    """Platform folders in non-dev_agent meta are cleared; others survive."""
    from dev_agent import config
    from core.threads_devagent import (
        create_devagent_thread,
        load_thread_meta,
        migrate_platform_workspace_meta,
    )

    install = str(config.INSTALL_ROOT.resolve())
    inside = os.path.join(install, "apps", "some_project")

    teacher = create_devagent_thread(
        title="teacher dialog", orchestrator_slug="teacher_assistant",
        orchestrator_name="Teacher", workspace=install, target_file=None,
    )
    teacher_nested = create_devagent_thread(
        title="teacher nested", orchestrator_slug="teacher_assistant",
        orchestrator_name="Teacher", workspace=inside, target_file=None,
    )
    dev_thread = create_devagent_thread(
        title="dev dialog", orchestrator_slug="dev_agent",
        orchestrator_name="DevAgent", workspace=install, target_file=None,
    )
    proj = tmp_path / "own_project"
    proj.mkdir()
    teacher_ok = create_devagent_thread(
        title="teacher ok", orchestrator_slug="teacher_assistant",
        orchestrator_name="Teacher", workspace=str(proj), target_file=None,
    )

    updated_before = load_thread_meta(teacher).get("updated_at")
    assert migrate_platform_workspace_meta() == 2

    # then: the foreign threads lost the platform folder (empty state on reopen)
    assert not load_thread_meta(teacher).get("workspace")
    assert not load_thread_meta(teacher).get("target_file")
    assert not load_thread_meta(teacher_nested).get("workspace")
    # ...and their updated_at was NOT bumped (the dialog list stays ordered)
    assert load_thread_meta(teacher).get("updated_at") == updated_before
    # dev_agent keeps the platform folder; a real project is untouched
    assert load_thread_meta(dev_thread).get("workspace") == install
    assert load_thread_meta(teacher_ok).get("workspace") == str(proj)

    # idempotent: the second pass finds nothing to clean
    assert migrate_platform_workspace_meta() == 0


def test_migration_clears_platform_target_file(tmp_path):
    """A single-file target inside the platform root is cleaned too."""
    from dev_agent import config
    from core.threads_devagent import (
        create_devagent_thread,
        load_thread_meta,
        migrate_platform_workspace_meta,
    )

    install = str(config.INSTALL_ROOT.resolve())
    tid = create_devagent_thread(
        title="teacher single-file", orchestrator_slug="teacher_assistant",
        orchestrator_name="Teacher", workspace=None,
        target_file=os.path.join(install, "app.py"),
    )

    assert migrate_platform_workspace_meta() == 1
    meta = load_thread_meta(tid)
    assert not meta.get("workspace")
    assert not meta.get("target_file")