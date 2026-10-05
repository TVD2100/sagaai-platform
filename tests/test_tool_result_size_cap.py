# -*- coding: utf-8 -*-
"""test_tool_result_size_cap.py - C1: tool-result size policy (spill/preview).

Regression tests for the context-overflow protection. Three tiers by the
serialized ``{"tool_result": ...}`` document size:

* up to ``_TOOL_RESULT_INLINE_LIMIT`` (35k) - passed through unchanged;
* above the inline limit - the full payload is SPILLED to
  ``<PROJECT_ROOT>/.dev_agent/tool_results/`` and replaced by a bounded
  head+tail preview carrying ``spill_path`` (readable back with the
  read_file tool), so the giant payload never enters the model context;
* above ``MAX_TOOL_RESULT_CHARS`` (200k) with a failed spill write -
  replaced by an explicit ok=False error carrying the measured size.
"""

import json

import pytest

from dev_agent import config
from dev_agent.agent_loop import (
    MAX_TOOL_RESULT_CHARS,
    _TOOL_RESULT_INLINE_LIMIT,
    _apply_tool_result_cap,
)
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


def _wire(result):
    """Serialize a result exactly like the agent loop does."""
    return json.dumps({"tool_result": result}, ensure_ascii=False)


# -- unit tests: pass-through tier -------------------------------------------


def test_cap_passes_small_results_unchanged():
    result = {"ok": True, "path": "src/a.py", "content": "hello"}
    assert _apply_tool_result_cap(result) == result


def test_cap_keeps_small_error_results():
    result = {"ok": False, "error": "File not found: x.py"}
    assert _apply_tool_result_cap(result) == result


# -- unit tests: spill tier ---------------------------------------------------


def test_cap_spills_oversized_result_and_returns_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    payload = "x" * (_TOOL_RESULT_INLINE_LIMIT + 5_000)
    result = {"ok": True, "path": "src/big.txt", "content": payload}

    capped = _apply_tool_result_cap(result, "read_file")

    assert capped["truncated"] is True
    assert capped["ok"] is True
    assert capped["path"] == "src/big.txt"
    assert capped["spill_size"] > _TOOL_RESULT_INLINE_LIMIT
    wire = _wire(capped)
    assert len(wire) <= _TOOL_RESULT_INLINE_LIMIT
    assert payload not in wire

    spill_path = capped["spill_path"]
    assert spill_path.startswith(".dev_agent/tool_results/tool_result_read_file_")
    spill_file = tmp_path / spill_path
    stored_doc = spill_file.read_text(encoding="utf-8")
    # The spill file holds the complete, untruncated document.
    assert stored_doc == _wire(result)
    assert payload in stored_doc


def test_cap_preview_bounds_giant_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    payload = "y" * 1_650_000

    capped = _apply_tool_result_cap({"ok": True, "content": payload}, "run_code")

    wire = _wire(capped)
    assert len(wire) <= _TOOL_RESULT_INLINE_LIMIT
    assert capped["head"].startswith('{"tool_result"')
    assert capped["tail"].endswith("}}")
    assert capped["head"] != capped["tail"]


def test_cap_rotates_spill_files(tmp_path, monkeypatch):
    from dev_agent import agent_loop as al

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(al, "_TOOL_RESULT_SPILL_KEEP", 3)

    for i in range(5):
        _apply_tool_result_cap(
            {"ok": True, "content": "z" * (_TOOL_RESULT_INLINE_LIMIT + 1), "i": i},
            "tool",
        )

    spill_dir = tmp_path / ".dev_agent" / "tool_results"
    files = list(spill_dir.glob("tool_result_*.json"))
    assert len(files) == 3


# -- unit tests: hard fallback tier (spill write failed) ----------------------


def test_cap_flags_oversized_result_when_spill_fails(monkeypatch):
    monkeypatch.setattr(
        "dev_agent.agent_loop._spill_tool_result_doc", lambda doc, name="": None
    )
    payload = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = {"ok": True, "content": payload}

    capped = _apply_tool_result_cap(result)

    assert capped["ok"] is False
    assert capped.get("result_too_large") is True
    assert capped["result_size"] > MAX_TOOL_RESULT_CHARS
    assert "content" not in capped
    assert "xxxxx" not in json.dumps(capped)


def test_cap_passes_midsize_result_when_spill_fails(monkeypatch):
    monkeypatch.setattr(
        "dev_agent.agent_loop._spill_tool_result_doc", lambda doc, name="": None
    )
    result = {"ok": True, "content": "a" * (_TOOL_RESULT_INLINE_LIMIT + 1_000)}
    # Emergency degrade: without a spill file the legacy pass-through
    # behaviour below the hard cap is preserved.
    assert _apply_tool_result_cap(result) == result


def test_cap_reports_exact_size(monkeypatch):
    monkeypatch.setattr(
        "dev_agent.agent_loop._spill_tool_result_doc", lambda doc, name="": None
    )
    capped = _apply_tool_result_cap(
        {"ok": True, "x": "a" * (MAX_TOOL_RESULT_CHARS + 50)}
    )
    assert capped["result_size"] - (MAX_TOOL_RESULT_CHARS + 50) < 100


# -- integration tests: the policy is applied by the dispatcher ---------------


def test_read_file_oversized_result_is_spilled_to_preview(sandbox):
    # ~300k chars of content -> the dispatcher spills the full payload and
    # hands the model a bounded preview with a recoverable path.
    (sandbox / "src" / "big.txt").write_text("line\n" * 60_000, encoding="utf-8")
    te = ToolExecutor()

    res = te.dispatch("read_file", {"path": "src/big.txt"})

    assert res["ok"] is True
    assert res["truncated"] is True
    assert res["bulk_sizes"]["content"] > 200_000
    assert "content" not in res
    wire = _wire(res)
    assert len(wire) <= _TOOL_RESULT_INLINE_LIMIT
    # The model recovers the full payload through the read_file tool.
    recovered = te.read_file(res["spill_path"])
    assert recovered["ok"] is True
    assert "line" in recovered["content"]


def test_read_file_small_file_passes(sandbox):
    (sandbox / "src" / "small.txt").write_text("hello\n" * 10, encoding="utf-8")
    te = ToolExecutor()
    res = te.read_file("src/small.txt")
    assert res["ok"] is True
    assert "hello" in res["content"]


def test_dispatch_applies_spill_policy_to_custom_tool_result(sandbox, monkeypatch):
    # A tool returning a huge payload goes through the same spill policy.
    te = ToolExecutor()

    def huge_tool(self, **kwargs):
        return {"ok": True, "content": "z" * (MAX_TOOL_RESULT_CHARS + 100)}

    monkeypatch.setattr(ToolExecutor, "huge_tool", huge_tool, raising=False)
    res = te.dispatch("huge_tool", {})
    assert res["truncated"] is True
    assert res["bulk_sizes"]["content"] > MAX_TOOL_RESULT_CHARS
    wire = _wire(res)
    assert len(wire) <= _TOOL_RESULT_INLINE_LIMIT
    assert "z" * (MAX_TOOL_RESULT_CHARS + 100) not in wire


def test_universal_agent_extra_tool_result_goes_through_spill_policy(sandbox, monkeypatch):
    # Custom orchestrator functions and connection tools are dispatched by
    # UniversalDevAgent._dispatch_impl; their results must go through the
    # same spill/preview policy as core tools.
    from dev_agent.universal_agent import UniversalDevAgent

    monkeypatch.setattr(
        UniversalDevAgent, "_is_disabled_for_orchestrator", lambda self, name: False
    )
    payload = "w" * (MAX_TOOL_RESULT_CHARS + 100)
    agent = UniversalDevAgent()
    agent._extra["huge_extra_tool"] = lambda **kwargs: {"ok": True, "content": payload}

    res = agent.dispatch("huge_extra_tool", {})

    assert res["ok"] is True
    assert res["truncated"] is True
    wire = _wire(res)
    assert len(wire) <= _TOOL_RESULT_INLINE_LIMIT
    assert payload not in wire
    spill_text = (sandbox / res["spill_path"]).read_text(encoding="utf-8")
    assert payload in spill_text
