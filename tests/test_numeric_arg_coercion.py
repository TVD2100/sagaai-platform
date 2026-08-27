# -*- coding: utf-8 -*-
"""test_numeric_arg_coercion.py - tests for automatic coercion of stringified
numeric/bool tool arguments.

The LLM occasionally emits JSON like ``{"offset": "1182"}`` instead of
``{"offset": 1182}``. UniversalDevAgent.dispatch now normalizes such string
values for parameters annotated as int/bool in both core and workspace tools.
"""

import pytest

from dev_agent import config
from dev_agent.tool_executor import ToolExecutor, _coerce_numeric_args
from dev_agent.universal_agent import UniversalDevAgent


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect DevAgent state into a temp sandbox."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(config, "PROTECTED_FILES", ())
    config.ensure_runtime_dirs()
    lines = [f"line {i} content" for i in range(1, 60)]
    (root / "src" / "module.py").write_text("\n".join(lines), encoding="utf-8")
    (root / "notes.txt").write_text("line 1 note\nline 2 note\n", encoding="utf-8")
    return root


def test_read_file_coerces_string_offset_and_limit(sandbox):
    te = ToolExecutor()
    res = te.dispatch("read_file", {"path": "src/module.py", "offset": "10", "limit": "5"})
    assert res["ok"], res
    assert res["offset"] == 10
    assert res["limit"] == 5
    assert res["window_lines"] == 5
    assert "10|line 10 content" in res["content"]
    assert "14|line 14 content" in res["content"]


def test_list_files_coerces_string_max_depth(sandbox):
    te = ToolExecutor()
    res = te.dispatch("list_files", {"subdir": "src", "max_depth": "2"})
    assert res["ok"], res


def test_apply_patch_coerces_string_occurrence(sandbox):
    te = ToolExecutor()
    res = te.dispatch(
        "apply_patch",
        {
            "path": "notes.txt",
            "edits": [{"old": "line 1 note", "new": "line one note", "occurrence": "1"}],
        },
    )
    assert res["ok"], res
    assert res["replacements"] == 1


def test_search_in_files_coerces_bools_and_ints(sandbox):
    agent = UniversalDevAgent()
    res = agent.dispatch(
        "search_in_files",
        {"query": "line 1", "path": "notes.txt", "regex": "false", "context_before": "1", "context_after": "1", "case_sensitive": "true"},
    )
    assert res["ok"], res
    assert res["match_count"] >= 1


def test_coerce_numeric_args_rejects_garbage_strings(sandbox):
    te = ToolExecutor()
    res = te.dispatch("read_file", {"path": "src/module.py", "offset": "abc"})
    assert not res["ok"]
    assert "integer" in res["error"]


def test_coerce_numeric_args_keeps_unknown_params(sandbox):
    te = ToolExecutor()
    res = te.dispatch("read_file", {"path": "src/module.py", "unknown_param": "x"})
    assert not res["ok"]
    assert "unknown_args" in res
