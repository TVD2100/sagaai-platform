"""
Scenario tests for the batched tool-call protocol of dev_agent/agent_loop.py,
walking the full agent loop end-to-end like a developer would:
  - a batch of independent read-only calls in ONE message executes every call
    in order and returns all results together;
  - a partially parsed batch executes the valid calls AND reports the broken
    block(s) in a partial_batch warning next to their results;
  - a truncated tail after a valid paired block is reported the same way;
  - a message whose only block is unparseable returns per-block diagnostics
    and never claims that a message must carry exactly one tool call.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent.agent_loop import run_agent_loop


def _make_skill() -> dict:
    return {
        "text": "system prompt",
        "service": "mock",
        "model": "mock-model",
        "temperature": 0.1,
    }


def _scripted_send(responses: list):
    """Return a mock send_request that returns items from *responses* in order."""
    state = {"it": iter(responses)}

    def _send(*args, **kwargs):
        try:
            return next(state["it"])
        except StopIteration:
            return ""

    return _send


class RecordingDispatcher:
    """A dispatcher that records every dispatched call and returns canned results."""

    def __init__(self, canned=None):
        self.canned = canned or {}
        self.recorded = []

    def dispatch_json(self, call):
        self.recorded.append(call)
        tool = call["tool"]
        if tool in self.canned:
            return self.canned[tool]
        return {"ok": True, "tool": tool, "message": "ok"}

    def dispatch(self, tool, args=None):
        self.recorded.append({"tool": tool, "args": args or {}})
        if tool in self.canned:
            return self.canned[tool]
        return {"ok": True, "tool": tool, "message": "ok", "applied": True}


def _result_lines_per_message(result) -> list:
    """Return parsed tool_result payloads grouped per user message.

    Batched executions join several tool_result JSON objects into ONE user
    message separated by newlines, so json.loads must run per line.
    """
    per_message = []
    for msg in result.history:
        content = msg.get("content", "")
        if msg.get("role") != "user" or '"tool_result"' not in content:
            continue
        lines = [
            json.loads(line.strip())["tool_result"]
            for line in content.split("\n")
            if line.strip().startswith('{"tool_result"')
        ]
        if lines:
            per_message.append(lines)
    return per_message


def test_scenario_batch_of_independent_reads_executes_all_in_order(monkeypatch):
    """Given an LLM message carrying a batch of three independent read-only
    calls, when the loop runs, then every call is dispatched in emission
    order, all three results arrive in ONE user message, and the loop stops
    cleanly on the explicit awaiting_user turn."""
    import dev_agent.agent_loop as al

    batch = (
        "Batching three independent reads.\n"
        "```json\n{\"tool\": \"read_file\", \"args\": {\"path\": \"README.md\"}}\n```\n"
        "```json\n{\"tool\": \"list_files\", \"args\": {}}\n```\n"
        "```json\n{\"tool\": \"search_in_files\", \"args\": {\"query\": \"def main\"}}\n```"
    )
    responses = [
        batch,
        "All results processed.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop(
        "batch reads", _make_skill(), disp, auto_apply=True, max_steps=12,
    )

    assert [c["tool"] for c in disp.recorded] == [
        "read_file", "list_files", "search_in_files",
    ]
    assert result.steps == 2
    assert result.status == "awaiting_user"

    per_message = _result_lines_per_message(result)
    assert len(per_message) == 1, per_message
    payloads = per_message[0]
    assert len(payloads) == 3
    assert all(p.get("ok") is True for p in payloads)
    assert [p.get("tool") for p in payloads] == [
        "read_file", "list_files", "search_in_files",
    ]
    assert not any(p.get("partial_batch") for p in payloads)


def test_scenario_partially_parsed_batch_warns_next_to_results(monkeypatch):
    """Given a batch where one block is valid and one is malformed, when the
    loop runs, then the valid call is dispatched, its result and a
    partial_batch warning (with per-block diagnostics, no 'exactly ONE'
    claim) are returned together, and the broken call is NOT executed."""
    import dev_agent.agent_loop as al

    batch = (
        '```json\n{"tool": "list_files", "args": {}}\n```\n'
        '```json\n{"tool": "read_file", "args": {"path": "main.py",}}\n```'
    )
    responses = [
        batch,
        "Noted.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop(
        "partial batch", _make_skill(), disp, auto_apply=True, max_steps=12,
    )

    assert [c["tool"] for c in disp.recorded] == ["list_files"]
    assert result.steps == 2
    assert result.status == "awaiting_user"

    per_message = _result_lines_per_message(result)
    assert len(per_message) == 1, per_message
    payloads = per_message[0]
    assert len(payloads) == 2
    warnings = [p for p in payloads if p.get("partial_batch")]
    assert len(warnings) == 1, payloads
    warn = warnings[0]
    assert warn.get("ok") is False
    assert warn.get("failed_constructs", 0) >= 1
    assert warn.get("executed_calls") == 1
    assert warn.get("diagnostics"), "diagnostics must name the broken block"
    joined = json.dumps(warn, ensure_ascii=False)
    assert "exactly ONE" not in joined
    assert "main.py" in joined


def test_scenario_truncated_tail_block_is_warned_after_valid_calls(monkeypatch):
    """Given a message with a valid paired block followed by a truncated
    (unclosed-fence) tool call, when the loop runs, then the paired call
    executes and the truncation is reported as a partial_batch warning."""
    import dev_agent.agent_loop as al

    message = (
        '```json\n{"tool": "list_files", "args": {}}\n```\n'
        'Truncated:\n```json\n{"tool": "read_file", "args": {"path": "SPEC.md"}}'
    )
    responses = [
        message,
        "Noted.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop(
        "truncated tail", _make_skill(), disp, auto_apply=True, max_steps=12,
    )

    assert [c["tool"] for c in disp.recorded] == ["list_files"]
    assert result.steps == 2
    assert result.status == "awaiting_user"

    per_message = _result_lines_per_message(result)
    assert len(per_message) == 1, per_message
    warnings = [p for p in per_message[0] if p.get("partial_batch")]
    assert len(warnings) == 1, per_message
    causes = " | ".join(
        d.get("cause", "") for d in warnings[0].get("diagnostics", [])
    )
    assert "TRUNCATED" in causes


def test_scenario_unparseable_single_block_gets_diagnostics_without_exactly_one(monkeypatch):
    """Given a message whose only tool-call block is unparseable, when the
    loop runs, then nothing is dispatched, ONE diagnostics error is fed
    back, and it never claims that the runtime accepts exactly one call
    per message."""
    import dev_agent.agent_loop as al

    broken = '```json\n{"tool": "read_file", "args": {"path": "main.py",}}\n```'
    responses = [
        broken,
        "Stopping here.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop(
        "broken block", _make_skill(), disp, auto_apply=True, max_steps=12,
    )

    assert disp.recorded == []
    assert result.steps == 2
    assert result.status == "awaiting_user"

    per_message = _result_lines_per_message(result)
    assert len(per_message) == 1 and len(per_message[0]) == 1, per_message
    payload = per_message[0][0]
    assert payload.get("ok") is False
    assert payload.get("diagnostics"), "per-block diagnostics expected"
    joined = json.dumps(payload, ensure_ascii=False)
    assert "exactly ONE" not in joined
    assert "could not be parsed" in payload.get("error", "")
