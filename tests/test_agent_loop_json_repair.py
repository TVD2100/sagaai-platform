"""
Unit tests for the automatic repair of truncated tool-call JSON in
dev_agent.agent_loop (unclosed-brace recovery for cut-off
{"tool": ...} payloads from the LLM).

Covers:
  - repair of one and several missing closing braces, incl. nested objects;
  - repair inside ```json fences and truncated fence tails;
  - refusal to repair foreign damage (unterminated strings, unclosed
    lists, balanced-but-broken JSON, flat objects, non-string tool);
  - valid calls are returned without the _json_repaired marker;
  - the DevAgent system prompt documents the JSON self-check rule.
"""
import pytest

from dev_agent import config
from dev_agent.agent_loop import (
    parse_tool_calls,
    _repair_unclosed_braces,
    _repair_unclosed_tool_json,
)


# ── _repair_unclosed_braces (helper unit) ────────────────────────────────────

class TestRepairUnclosedBracesUnit:
    def test_repairs_single_missing_brace(self):
        raw = '{"tool": "read_file", "args": {"path": "main.py"}'
        fixed = _repair_unclosed_braces(raw)
        assert fixed == '{"tool": "read_file", "args": {"path": "main.py"}}'

    def test_refuses_unterminated_string(self):
        assert _repair_unclosed_braces('{"tool": "read_file", "args": {"path": "ma') is None

    def test_refuses_unclosed_list(self):
        raw = '{"tool": "apply_patch", "args": {"path": "x.py", "edits": [{"old": "a", "new": "b"}'
        assert _repair_unclosed_braces(raw) is None

    def test_refuses_balanced_but_broken_json(self):
        assert _repair_unclosed_braces('{"tool": "read_file", "args": {"path": "x.py",}}') is None

    def test_refuses_flat_object_without_tool(self):
        assert _repair_unclosed_braces('{"path": "x.py", "content": "hello"}') is None


# ── parse_tool_calls end-to-end repair ──────────────────────────────────────

class TestParseToolCallsRepair:
    def test_repairs_one_missing_brace(self):
        calls = parse_tool_calls('{"tool": "read_file", "args": {"offset": 10}')
        assert len(calls) == 1
        assert calls[0]["tool"] == "read_file"
        assert calls[0]["args"] == {"offset": 10}
        assert calls[0]["_json_repaired"] == 1

    def test_repairs_multiple_missing_braces_nested(self):
        raw = '{"tool": "read_file", "args": {"path": "x.py", "win": {"start": 1, "end": 9}'
        calls = parse_tool_calls(raw)
        assert len(calls) == 1
        assert calls[0]["args"] == {"path": "x.py", "win": {"start": 1, "end": 9}}
        assert calls[0]["_json_repaired"] == 2

    def test_repairs_deep_nesting_without_any_closing_brace(self):
        raw = '{"tool": "read_file", "args": {"path": "x.py", "win": {"start": 1, "end": {"l": 2}'
        calls = parse_tool_calls(raw)
        assert len(calls) == 1
        assert calls[0]["args"] == {"path": "x.py", "win": {"start": 1, "end": {"l": 2}}}
        assert calls[0]["_json_repaired"] == 3

    def test_repairs_cut_off_call_inside_closed_fence(self):
        raw = '```json\n{"tool": "list_files", "args": {}\n```'
        calls = parse_tool_calls(raw)
        assert len(calls) == 1
        assert calls[0]["tool"] == "list_files"
        assert calls[0]["args"] == {}
        assert calls[0]["_json_repaired"] == 1

    def test_repairs_truncated_fence_tail(self):
        raw = 'Intro text\n```json\n{"tool": "list_files", "args": {}'
        calls = parse_tool_calls(raw)
        assert len(calls) == 1
        assert calls[0]["tool"] == "list_files"
        assert calls[0]["args"] == {}
        assert calls[0]["_json_repaired"] == 1

    def test_refuses_unterminated_string(self):
        assert parse_tool_calls('{"tool": "read_file", "args": {"path": "ma') == []

    def test_refuses_unclosed_list(self):
        raw = '{"tool": "apply_patch", "args": {"path": "x.py", "edits": [{"old": "a", "new": "b"}'
        assert parse_tool_calls(raw) == []

    def test_refuses_balanced_broken_json(self):
        assert parse_tool_calls('{"tool": "read_file", "args": {"path": "x.py",}}') == []

    def test_refuses_flat_object_without_tool(self):
        assert parse_tool_calls('{"path": "x.py", "content": "hello"}') == []

    def test_refuses_non_string_tool(self):
        assert parse_tool_calls('{"tool": 1, "args": {}}') == []

    def test_valid_call_carries_no_marker(self):
        calls = parse_tool_calls('{"tool": "list_files", "args": {}}')
        assert len(calls) == 1
        assert "_json_repaired" not in calls[0]

    def test_repair_tool_json_direct(self):
        raw = '```json\n{"tool": "read_file", "args": {"path": "a.py"}\n```'
        calls = _repair_unclosed_tool_json(raw)
        assert len(calls) == 1
        assert calls[0]["args"] == {"path": "a.py"}
        assert calls[0]["_json_repaired"] == 1


# ── system prompt documents the companion self-check rule ───────────────────

class TestSystemPromptDocumentsJsonSelfCheck:
    def test_canonical_prompt_documents_self_check(self):
        prompt = config.SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Self-check each tool-call JSON" in prompt
