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

# --- layered repair cascade and parse-error policy (DevAgent) ---
import json

from dev_agent import agent_loop as al

_Q = chr(34)
_BS = chr(92)
_NL = chr(10)
_FENCE = chr(96) * 3

def test_valid_json_untouched_by_cascade():
    raw = json.dumps({'tool': 'list_files', 'args': {}})
    assert al._json_loads_lenient(raw) == {'tool': 'list_files', 'args': {}}
    assert al.parse_tool_calls(raw) == [{'tool': 'list_files', 'args': {}}]

def test_stray_closing_bracket_removed():
    good = json.dumps({'tool': 'apply_patch', 'args': {'edits': [{'new': 'a', 'old': 'b'}], 'path': 'x.py'}})
    pos = good.find('}]') + 1
    broken = good[:pos] + ']' + good[pos:]
    calls = al.parse_tool_calls(broken)
    assert len(calls) == 1
    assert calls[0]['tool'] == 'apply_patch'
    assert calls[0]['args']['edits'] == [{'new': 'a', 'old': 'b'}]
    assert calls[0]['args']['path'] == 'x.py'

def test_structural_quotes_escaped_one_level():
    inner = json.dumps({'tool': 'apply_patch', 'args': {'edits': [{'new': 'x = 1', 'old': 'y = 2'}]}})
    raw = _BS + 'n' + inner.replace(_Q, _BS + _Q)
    text = _FENCE + 'json' + _NL + raw + _NL + _FENCE
    calls = al.parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]['args']['edits'][0]['new'] == 'x = 1'
    assert calls[0]['args']['edits'][0]['old'] == 'y = 2'
    assert calls[0].get('_json_repaired') == 1

def test_json_string_wrapped_call_decoded():
    wrapped = json.dumps(json.dumps({'tool': 'list_files', 'args': {}}))
    text = _FENCE + 'json' + _NL + wrapped + _NL + _FENCE
    calls = al.parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]['tool'] == 'list_files'
    assert calls[0].get('_json_repaired') == 1

def test_collapse_value_escapes_unit():
    v = 's = ' + _BS + _Q + 'x' + _BS + _Q
    out = al._collapse_value_escapes({'k': v})
    assert out == {'k': 's = ' + _Q + 'x' + _Q}

# --- loop-level: parse-error hard-stop policy ---

def _lp_skill():
    return {'text': 'p', 'service': 'mock', 'model': 'm', 'temperature': 0.1}

def _lp_send(responses):
    it = iter(responses)

    def _send(*args, **kwargs):
        try:
            return next(it)
        except StopIteration:
            return ''

    return _send

class _RecDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch_json(self, call):
        self.calls.append(call)
        return {'ok': True, 'tool': call['tool'], 'message': 'ok'}

    def dispatch(self, tool, args):
        return {'ok': True}

def _broken_call(p):
    return '{' + _Q + 'tool' + _Q + ': ' + _Q + 'list_files' + _Q + ', ' + _Q + 'args' + _Q + ': {' + _Q + 'path' + _Q + ': ' + _Q + p + _Q + ',}}'

def test_varied_broken_calls_never_hard_stop(monkeypatch):
    broken = [_broken_call('f' + str(i) + '.py') for i in range(6)]
    sent = [_FENCE + 'json' + _NL + b + _NL + _FENCE for b in broken] + ['ok']
    monkeypatch.setattr(al, 'send_request', _lp_send(sent))
    disp = _RecDispatcher()
    result = al.run_agent_loop('task', _lp_skill(), disp, auto_apply=True, max_steps=200)
    assert result.status != 'error'
    assert disp.calls == []

def test_identical_broken_call_stops_the_loop(monkeypatch):
    text = _FENCE + 'json' + _NL + _broken_call('f.py') + _NL + _FENCE
    monkeypatch.setattr(al, 'send_request', _lp_send([text] * 5))
    disp = _RecDispatcher()
    result = al.run_agent_loop('task', _lp_skill(), disp, auto_apply=True, max_steps=40)
    assert result.status == 'error'
    assert 'SAME broken tool-call JSON' in result.text
    assert disp.calls == []

