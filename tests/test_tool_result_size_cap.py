# -*- coding: utf-8 -*-
"""test_tool_result_size_cap.py - M1: tool-result size cap (200k chars).

Regression tests for the context-overflow protection introduced after an
incident where a single 1.65M-character tool_result was persisted into the
dialog history and later pushed the LLM request over the context window
(HTTP 400). Results up to MAX_TOOL_RESULT_CHARS are returned unchanged;
larger ones are replaced by an explicit ok=False error carrying the
measured size, and the payload is never passed to the model.
"""

import json

import pytest

from dev_agent import config
from dev_agent.agent_loop import MAX_TOOL_RESULT_CHARS, _apply_tool_result_cap
from dev_agent.tool_executor import ToolExecutor


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
    return root


# ── unit tests: the cap helper itself ──────────────────────────────────────


def test_cap_passes_small_results_unchanged():
    result = {"ok": True, "path": "src/a.py", "content": "hello"}
    assert _apply_tool_result_cap(result) == result


def test_cap_keeps_small_error_results():
    result = {"ok": False, "error": "File not found: x.py"}
    assert _apply_tool_result_cap(result) == result


def test_cap_blocks_oversized_result():
    payload = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = {"ok": True, "content": payload}
    capped = _apply_tool_result_cap(result)
    assert capped["ok"] is False
    assert capped.get("result_too_large") is True
    assert capped["result_size"] > MAX_TOOL_RESULT_CHARS
    assert "content" not in capped
    assert "xxxxx" not in json.dumps(capped)


def test_cap_reports_exact_size():
    capped = _apply_tool_result_cap(
        {"ok": True, "x": "a" * (MAX_TOOL_RESULT_CHARS + 50)}
    )
    assert capped["result_size"] - (MAX_TOOL_RESULT_CHARS + 50) < 100


# ── integration tests: the cap is applied by the dispatcher ────────────────


def test_read_file_oversized_result_is_error(sandbox):
    # 300k chars of content -> far above the 200k cap once line numbers
    # and JSON escaping are added by the tool.
    (sandbox / "src" / "big.txt").write_text("line\n" * 60_000, encoding="utf-8")
    te = ToolExecutor()
    res = te.dispatch("read_file", {"path": "src/big.txt"})
    assert res["ok"] is False
    assert res.get("result_too_large") is True
    assert res["result_size"] > MAX_TOOL_RESULT_CHARS
    assert "content" not in res


def test_read_file_small_file_passes(sandbox):
    (sandbox / "src" / "small.txt").write_text("hello\n" * 10, encoding="utf-8")
    te = ToolExecutor()
    res = te.read_file("src/small.txt")
    assert res["ok"] is True
    assert "hello" in res["content"]


def test_dispatch_applies_cap_to_custom_tool_result(sandbox, monkeypatch):
    # A tool returning a huge payload must be capped at dispatch time.
    te = ToolExecutor()
    def huge_tool(self, **kwargs):
        return {"ok": True, "content": "z" * (MAX_TOOL_RESULT_CHARS + 100)}
    monkeypatch.setattr(ToolExecutor, "huge_tool", huge_tool, raising=False)
    res = te.dispatch("huge_tool", {})
    assert res["ok"] is False
    assert res.get("result_too_large") is True
    assert "content" not in res
