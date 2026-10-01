# -*- coding: utf-8 -*-
"""Tests for core.recent_workspaces - persistent workspace history."""
import pytest
import tempfile
import os
import sys
import importlib
import shutil
from pathlib import Path

# Ensure the sagaai package is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own temporary SQLite database."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    # Reset cached engine so it picks up the new env var
    import storage.db as db_mod
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()
    # Reload core.paths so DB_PATH reflects the new env
    import core.paths as paths_mod
    importlib.reload(paths_mod)
    # Reload db module so it uses new paths
    importlib.reload(db_mod)
    yield
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()


@pytest.fixture
def two_dirs(tmp_path):
    """Create two temporary directories with a marker file in each."""
    a = tmp_path / "project_alpha"
    b = tmp_path / "project_beta"
    a.mkdir()
    b.mkdir()
    (a / "README.md").write_text("alpha")
    (b / "README.md").write_text("beta")
    return str(a), str(b)


def test_add_and_get():
    """Start from scratch: add one path and get it back."""
    from core.recent_workspaces import add_recent_workspace, get_recent_workspaces

    tmpdir = tempfile.mkdtemp()
    try:
        add_recent_workspace(tmpdir)
        recent = get_recent_workspaces()
        assert len(recent) == 1
        assert recent[0] == str(Path(tmpdir).resolve())
    finally:
        os.rmdir(tmpdir)


def test_get_empty_returns_list():
    """No history at all -> empty list, no error."""
    from core.recent_workspaces import get_recent_workspaces
    assert get_recent_workspaces() == []


def test_duplicates_are_deduplicated(two_dirs):
    """Adding the same path twice keeps only one entry at the top."""
    from core.recent_workspaces import add_recent_workspace, get_recent_workspaces

    a, b = two_dirs
    add_recent_workspace(a)
    add_recent_workspace(b)
    add_recent_workspace(a)  # duplicate -> moves to top
    recent = get_recent_workspaces()
    assert recent[0] == a
    assert len(recent) == 2  # alpha, beta - only one copy of alpha
    assert b in recent


def test_max_limit_is_five(tmp_path):
    """Adding more than 5 projects trims the oldest."""
    from core.recent_workspaces import add_recent_workspace, get_recent_workspaces

    dirs = []
    for i in range(7):
        d = tmp_path / f"proj_{i}"
        d.mkdir()
        dirs.append(str(d))

    for i in range(7):
        add_recent_workspace(dirs[i])

    recent = get_recent_workspaces()
    assert len(recent) == 5
    # The newest are proj_6 ... proj_2 (5 items)
    assert dirs[6] == recent[0]
    assert dirs[2] == recent[-1]


def test_nonexistent_folders_are_filtered(two_dirs):
    """get_recent_workspaces skips paths that no longer exist."""
    from core.recent_workspaces import add_recent_workspace, get_recent_workspaces

    a, b = two_dirs
    add_recent_workspace(a)
    add_recent_workspace(b)

    shutil.rmtree(b)

    recent = get_recent_workspaces()
    assert len(recent) == 1
    assert recent[0] == a


def test_clear_removes_all(two_dirs):
    """clear_recent_workspaces() empties the history."""
    from core.recent_workspaces import add_recent_workspace, get_recent_workspaces, clear_recent_workspaces

    a, b = two_dirs
    add_recent_workspace(a)
    add_recent_workspace(b)
    assert len(get_recent_workspaces()) == 2
    clear_recent_workspaces()
    assert get_recent_workspaces() == []


def test_add_idempotent_on_failure():
    """add_recent_workspace does not crash when DB ops fail.

    We only need to be sure the call doesn't raise.
    """
    from core.recent_workspaces import add_recent_workspace
    tmpdir = tempfile.mkdtemp()
    try:
        add_recent_workspace(tmpdir)
    finally:
        Path(tmpdir).rmdir()


def test_list_recent_workspaces_tool(two_dirs):
    """Verify that workspace_tools.list_recent_workspaces() works."""
    from core.recent_workspaces import add_recent_workspace
    a, b = two_dirs
    add_recent_workspace(a)
    add_recent_workspace(b)

    from dev_agent.workspace_tools import list_recent_workspaces
    result = list_recent_workspaces()
    assert result["ok"] is True
    assert result["count"] == 2
    projects = result["projects"]
    assert projects[0]["index"] == 1
    assert projects[0]["path"] == b
    assert projects[0]["name"] == Path(b).name
    assert projects[1]["path"] == a


# ─── Per-orchestrator scopes (workspace isolation v2) ─────────────────────────

def test_scopes_are_independent(tmp_path):
    """Each orchestrator owns its own shelf: folders never mix."""
    from core.recent_workspaces import add_recent_workspace, get_recent_workspaces

    dev_proj = tmp_path / "scope_dev"
    dev_proj.mkdir()
    te_proj = tmp_path / "scope_teacher"
    te_proj.mkdir()

    add_recent_workspace(str(dev_proj), slug="dev_agent")
    add_recent_workspace(str(te_proj), slug="teacher_assistant")

    assert get_recent_workspaces("dev_agent") == [str(dev_proj.resolve())]
    assert get_recent_workspaces("teacher_assistant") == [str(te_proj.resolve())]
    # Bare calls keep the legacy dev_agent default.
    assert get_recent_workspaces() == [str(dev_proj.resolve())]


def test_legacy_flat_list_read_as_dev_agent_scope(tmp_path):
    """The old flat-list ConfigKV format still feeds the dev_agent shelf."""
    from storage.repository import repo_load_config, repo_save_config
    from core.recent_workspaces import get_recent_workspaces, RECENT_WORKSPACES_KEY

    proj = tmp_path / "legacy_flat"
    proj.mkdir()
    cfg = repo_load_config()
    cfg[RECENT_WORKSPACES_KEY] = [str(proj)]  # legacy format
    repo_save_config(cfg)

    assert get_recent_workspaces("dev_agent") == [str(proj.resolve())]
    assert get_recent_workspaces("teacher_assistant") == []


def test_neutral_root_is_never_suggested(monkeypatch, tmp_path):
    """The empty-state placeholder is refused on write and filtered on read."""
    from storage.repository import repo_load_config, repo_save_config
    from dev_agent import config
    from core.recent_workspaces import (
        add_recent_workspace, get_recent_workspaces, RECENT_WORKSPACES_KEY,
    )

    neutral = tmp_path / "neutral_root"
    neutral.mkdir()
    monkeypatch.setattr(config, "NEUTRAL_ROOT", neutral)

    # Write-side guard: the placeholder is not worth remembering.
    add_recent_workspace(str(neutral), slug="dev_agent")
    assert get_recent_workspaces("dev_agent") == []

    # Read-side guard: a hand-edited / legacy entry is filtered out.
    cfg = repo_load_config()
    cfg[RECENT_WORKSPACES_KEY] = {"dev_agent": [str(neutral)]}
    repo_save_config(cfg)
    assert get_recent_workspaces("dev_agent") == []


def test_platform_root_hidden_from_foreign_scopes(tmp_path):
    """The SagaAI install root is hidden everywhere except dev_agent."""
    from storage.repository import repo_load_config, repo_save_config
    from dev_agent import config
    from core.recent_workspaces import get_recent_workspaces, RECENT_WORKSPACES_KEY

    install = str(config.INSTALL_ROOT.resolve())
    cfg = repo_load_config()
    cfg[RECENT_WORKSPACES_KEY] = {
        "dev_agent": [install],
        "teacher_assistant": [install],
    }
    repo_save_config(cfg)

    assert install in get_recent_workspaces("dev_agent")
    assert get_recent_workspaces("teacher_assistant") == []


def test_seed_from_own_threads_only(tmp_path):
    """Dialogs seed their OWN scope's menu and never a neighbour's."""
    from core.threads_devagent import create_devagent_thread
    from core.recent_workspaces import get_recent_workspaces

    dev_proj = tmp_path / "seed_dev"
    dev_proj.mkdir()
    te_proj = tmp_path / "seed_teacher"
    te_proj.mkdir()

    create_devagent_thread("dev dialog", orchestrator_slug="dev_agent",
                           orchestrator_name="DevAgent", workspace=str(dev_proj))
    create_devagent_thread("teacher dialog", orchestrator_slug="teacher_assistant",
                           orchestrator_name="Teacher", workspace=str(te_proj))

    assert get_recent_workspaces("dev_agent") == [str(dev_proj.resolve())]
    assert get_recent_workspaces("teacher_assistant") == [str(te_proj.resolve())]


def test_clear_one_scope_keeps_others(tmp_path):
    """Clearing one shelf leaves the other orchestrators' history intact."""
    from core.recent_workspaces import (
        add_recent_workspace, get_recent_workspaces, clear_recent_workspaces,
    )

    dev_proj = tmp_path / "clear_dev"
    dev_proj.mkdir()
    te_proj = tmp_path / "clear_teacher"
    te_proj.mkdir()
    add_recent_workspace(str(dev_proj), slug="dev_agent")
    add_recent_workspace(str(te_proj), slug="teacher_assistant")

    clear_recent_workspaces("dev_agent")

    assert get_recent_workspaces("dev_agent") == []
    assert get_recent_workspaces("teacher_assistant") == [str(te_proj.resolve())]
