"""Integration tests of the memory tools and the child write barrier.

The contract module (``core.memory_profile``) is tested on its own; here the
tool surface is exercised the way an employee reaches it: a resolved folder,
a declared profile in the config, and the refusal a delegated child receives.
"""
import pytest

from dev_agent.tool_executor import ToolExecutor

PROFILE = {
    "levels": [
        {"id": "state", "kind": "always", "path": "registry.md",
         "columns": ["date", "item", "status"], "read_lines": 4},
        {"id": "history", "kind": "search", "path": "journal.md"},
    ]
}


@pytest.fixture()
def executor(tmp_path, monkeypatch):
    """Return a ToolExecutor whose employee folder is a temporary directory."""
    folder = tmp_path / "agents" / "test_employee"
    folder.mkdir(parents=True)
    (folder / "registry.md").write_text(
        "# Registry\n\n| date | item | status |\n|---|---|---|\n| 2026-11-01 | a | done |\n",
        encoding="utf-8",
    )
    (folder / "journal.md").write_text(
        "# Journal\n\n" + "\n".join(f"- note {i}" for i in range(30)) + "\n",
        encoding="utf-8",
    )

    import core.orchestrator_folders as of
    monkeypatch.setattr(
        of, "resolve_data_dir",
        lambda slug, workspace="", prompt_text=None, config=None: {
            "path": str(folder), "source": "config", "scope": "central",
            "exists": True,
        },
    )
    import core.orchestrators as orch
    monkeypatch.setattr(
        orch, "get_orchestrator",
        lambda slug: {"config": {"memory_profile": PROFILE}},
    )

    ex = ToolExecutor()
    ex.set_orchestrator_slug("test_employee")
    ex._test_folder = folder
    return ex


def test_status_lists_levels_without_bodies(executor):
    result = executor.memory_status()
    assert result["ok"] is True
    assert result["level_count"] == 2
    state = next(level for level in result["levels"] if level["id"] == "state")
    assert state["lines"] == 5
    assert state["columns"] == ["date", "item", "status"]
    assert result["subagent"] is False


def test_read_applies_the_declared_budget(executor):
    result = executor.memory_read("state")
    assert result["ok"] is True
    assert result["lines_returned"] == 4
    assert result["lines_total"] == 5


def test_read_refuses_to_load_a_search_level_whole(executor):
    result = executor.memory_read("history")
    assert result["ok"] is False
    assert "search-only" in result["error"]


def test_read_of_an_unknown_level_names_the_declared_ones(executor):
    result = executor.memory_read("nope")
    assert result["ok"] is False
    assert "state" in result["error"] and "history" in result["error"]


def test_append_writes_one_row_and_keeps_the_rest(executor):
    result = executor.memory_append("state", row={"date": "2026-11-19",
                                                  "item": "b", "status": "open"})
    assert result["ok"] is True
    text = (executor._test_folder / "registry.md").read_text(encoding="utf-8")
    assert "| 2026-11-19 | b | open |" in text
    assert "| 2026-11-01 | a | done |" in text


def test_append_rejects_a_row_that_does_not_match_the_columns(executor):
    result = executor.memory_append("state", row={"date": "2026-11-19"})
    assert result["ok"] is False
    assert "memory_append failed" in result["error"]


def test_search_answers_without_loading_the_journal(executor):
    result = executor.memory_search("history", "note 29")
    assert result["ok"] is True
    assert result["hit_count"] == 1
    assert result["lines_total"] == 32


def test_child_cannot_append_to_the_registries(executor):
    executor.child_mode = True
    result = executor.memory_append("state", row={"date": "d", "item": "i",
                                                  "status": "s"})
    assert result["ok"] is False
    assert result["error_code"] == "child_memory_write"
    text = (executor._test_folder / "registry.md").read_text(encoding="utf-8")
    assert "2026-11-19" not in text


def test_child_cannot_rotate_a_level(executor):
    executor.child_mode = True
    result = executor.memory_rotate("history")
    assert result["ok"] is False
    assert result["error_code"] == "child_memory_write"


def test_child_can_still_read_and_search(executor):
    executor.child_mode = True
    assert executor.memory_read("state")["ok"] is True
    assert executor.memory_search("history", "note 3")["ok"] is True


def test_rotate_without_max_rows_reports_the_missing_declaration(executor):
    result = executor.memory_rotate("state")
    assert result["ok"] is False
    assert "max_rows" in result["error"]


def test_status_survives_an_unresolvable_folder(executor, monkeypatch):
    import core.orchestrator_folders as of
    monkeypatch.setattr(
        of, "resolve_data_dir",
        lambda *a, **kw: {"path": "", "source": "default", "scope": "central"},
    )
    result = executor.memory_status()
    assert result["ok"] is False
    assert "cannot be resolved" in result["error"]


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
