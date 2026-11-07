"""The dialog files folder is a third allowed root of the file tools.

A dialog's uploads and produced artifacts live in ``HISTORY_DIR/<tid>/files``,
which is inside the installation and outside every project. Without a root of
its own the only way to reach them was to point the workspace at the
installation root, which then made the whole platform the "project".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.paths as core_paths
from dev_agent import config
from dev_agent.tools import workspace as te

THREAD_ID = "20260101_000000_abcdef"


@pytest.fixture()
def dialog_root(tmp_path, monkeypatch):
    """Point HISTORY_DIR at a temporary install and bind a thread to it."""
    history = tmp_path / "history"
    (history / THREAD_ID / "files").mkdir(parents=True)
    monkeypatch.setattr(core_paths, "HISTORY_DIR", str(history))
    monkeypatch.setattr(config, "ACTIVE_THREAD_ID", THREAD_ID)
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", False)
    return (history / THREAD_ID / "files").resolve()


# ─── the root itself ─────────────────────────────────────────────────────────


def test_thread_files_root_follows_the_active_thread(dialog_root):
    assert config.thread_files_root() == dialog_root


def test_thread_files_root_is_none_without_a_thread(monkeypatch):
    """No thread means no folder: the root is disabled, never guessed."""
    monkeypatch.setattr(config, "ACTIVE_THREAD_ID", "")
    assert config.thread_files_root() is None


def test_thread_files_root_rejects_a_traversing_thread_id(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_THREAD_ID", "../../etc")
    assert config.thread_files_root() is None


# ─── addressing ─────────────────────────────────────────────────────────────


def test_thread_prefix_resolves_inside_the_folder(dialog_root):
    target = config.resolve_in_project("thread:report.pdf")
    assert target == dialog_root / "report.pdf"


def test_thread_prefix_accepts_nested_paths(dialog_root):
    target = config.resolve_in_project("thread:charts/figure.png")
    assert target == dialog_root / "charts" / "figure.png"


def test_an_absolute_path_inside_the_folder_is_accepted(dialog_root):
    absolute = str(dialog_root / "upload.txt")
    assert config.resolve_in_project(absolute) == dialog_root / "upload.txt"


def test_an_absolute_path_reports_the_thread_prefix(dialog_root):
    rel = config.to_project_relative(str(dialog_root / "upload.txt"))
    assert rel == "thread:upload.txt"


def test_thread_prefix_cannot_escape_the_folder(dialog_root):
    with pytest.raises(ValueError):
        config.resolve_in_project("thread:../../../secret.txt")


def test_thread_prefix_without_a_thread_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_THREAD_ID", "")
    with pytest.raises(ValueError):
        config.resolve_in_project("thread:report.pdf")


def test_a_project_path_is_not_reported_as_a_dialog_path(dialog_root, tmp_path, monkeypatch):
    """The root order matters: a project file keeps its project address."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", project)
    assert config.to_project_relative(str(project / "README.md")) == "README.md"


# ─── the empty state ────────────────────────────────────────────────────────


def test_dialog_paths_are_allowed_without_a_selected_project(dialog_root):
    """Reading an attachment is the first thing a new task often does."""
    assert te.workspace_guard_error("read_file", {"path": "thread:upload.txt"}) is None
    assert te.workspace_guard_error("propose_file", {"path": "thread:out.md"}) is None
    assert te.workspace_guard_error("list_files", {"subdir": "thread:"}) is None


def test_an_absolute_dialog_path_is_allowed_without_a_project(dialog_root):
    absolute = str(dialog_root / "upload.txt")
    assert te.workspace_guard_error("read_file", {"path": absolute}) is None


def test_project_paths_stay_blocked_without_a_selected_project(dialog_root):
    blocked = te.workspace_guard_error("read_file", {"path": "README.md"})
    assert blocked is not None
    assert blocked.get("workspace_not_selected") is True


def test_mixing_a_project_target_with_a_dialog_target_stays_blocked(dialog_root):
    """One project target in the batch is enough to require a folder."""
    blocked = te.workspace_guard_error(
        "search_in_files", {"files": ["thread:upload.txt", "README.md"]})
    assert blocked is not None


# ─── end to end: the tool really writes there ───────────────────────────────


def test_propose_file_writes_into_the_dialog_folder(dialog_root, tmp_path, monkeypatch):
    """The prefix survives the whole write path, not only the guard.

    The draft staging and the apply step each resolve the path again; a
    resolver that knows the prefix but a writer that does not would leave the
    file in the project root instead.
    """
    from dev_agent.tool_executor import ToolExecutor

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", project)
    monkeypatch.setattr(config, "BACKUPS_DIR", project / ".dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", project / ".dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", project / "CHANGELOG.md")
    monkeypatch.setattr(config, "PROTECTED_FILES", ())
    config.ensure_runtime_dirs()

    executor = ToolExecutor()
    result = executor.propose_file("thread:never.txt", "# report\n")

    assert result.get("ok"), result
    assert (dialog_root / "never.txt").read_text(encoding="utf-8") == "# report\n"
    # Nothing leaked into the project, and no literal 'thread:' file appeared.
    assert not (project / "never.txt").exists()
    assert not (project / "thread:never.txt").exists()


def test_read_file_reads_an_upload_through_the_prefix(dialog_root, tmp_path, monkeypatch):
    from dev_agent.tool_executor import ToolExecutor

    (dialog_root / "upload.txt").write_text("hello", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", project)
    monkeypatch.setattr(config, "PROTECTED_FILES", ())

    result = ToolExecutor().read_file("thread:upload.txt")

    assert result.get("ok"), result
    assert "hello" in result.get("content", "")
