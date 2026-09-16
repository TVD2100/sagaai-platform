"""
Scenario tests for the loop-stuck protections of dev_agent/agent_loop.py,
walking the full agent loop end-to-end like a developer would:
  - three consecutive failures of the same tool (with different arguments)
    add fallback-chain guidance on the 3rd failure and the loop does not
    spin or hard-stop;
  - a flood of identical tool calls in one message yields ONE compact
    error and dispatches nothing;
  - a prose-only turn that ends with loop_status "continue" keeps the
    loop alive instead of a false stop.
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


def _tool_results(result) -> list:
    """Extract parsed tool_result payloads from the user messages of history."""
    payloads = []
    for msg in result.history:
        content = msg.get("content", "")
        if msg.get("role") == "user" and '"tool_result"' in content:
            payloads.append(json.loads(content)["tool_result"])
    return payloads


def test_scenario_three_consecutive_failures_add_guidance_and_end_cleanly(monkeypatch):
    """Given an LLM that fails apply_patch three times in a row with different
    arguments, when the loop runs, then the 3rd failure's error carries the
    fallback-chain guidance (run_code), exactly three attempts are dispatched,
    no hard error event is emitted and the loop stops cleanly awaiting the user."""
    import dev_agent.agent_loop as al

    responses = [
        '{"tool": "apply_patch", "args": {"path": "x.py", "edits": [{"old": "a", "new": "b"}]}}',
        '{"tool": "apply_patch", "args": {"path": "x.py", "edits": [{"old": "c", "new": "d"}]}}',
        '{"tool": "apply_patch", "args": {"path": "x.py", "edits": [{"old": "e", "new": "f"}]}}',
        "Stopped: the tool keeps failing.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    events = []
    disp = RecordingDispatcher({"apply_patch": {"ok": False, "error": "anchor not found"}})

    result = run_agent_loop(
        "patch x.py", _make_skill(), disp, auto_apply=True,
        max_steps=12, on_event=events.append,
    )

    patches = [c for c in disp.recorded if c.get("tool") == "apply_patch"]
    assert len(patches) == 3
    assert result.steps == 4
    assert result.status == "awaiting_user"
    assert not [e for e in events if e.get("type") == "error"]

    failures = _tool_results(result)
    assert len(failures) == 3
    assert all(f.get("ok") is False for f in failures)
    assert "run_code" not in failures[0]["error"]
    assert "run_code" not in failures[1]["error"]
    assert "run_code" in failures[2]["error"]
    assert "switch to the next tool" in failures[2]["error"]


def test_scenario_duplicate_call_flood_yields_one_compact_error(monkeypatch):
    """Given an LLM message with 200 identical tool calls, when the loop runs,
    then NOTHING is dispatched, exactly one compact error mentioning
    identical_calls is fed back, and the loop stops awaiting the user."""
    import dev_agent.agent_loop as al

    one = '{"tool": "list_files", "args": {}}'
    spam = one * 200
    responses = [
        spam,
        "Stopped.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop(
        "list files", _make_skill(), disp, auto_apply=True, max_steps=12,
    )

    assert disp.recorded == []
    assert result.steps == 2
    assert result.status == "awaiting_user"

    compact_msgs = [
        msg["content"] for msg in result.history
        if msg.get("role") == "user" and "identical_calls" in msg.get("content", "")
    ]
    assert len(compact_msgs) == 1
    payload = json.loads(compact_msgs[0])["tool_result"]
    assert payload.get("ok") is False
    assert payload["identical_calls"] >= 190
    assert "IDENTICAL tool calls" in payload["error"]
    # Compactness: one short error instead of re-echoing the flood.
    assert len(compact_msgs[0]) < len(spam) // 10


def test_scenario_prose_with_continue_keeps_loop_alive(monkeypatch):
    """Given a prose-only turn ending with loop_status continue during
    execution, when the loop runs, then the loop continues to the next step,
    dispatches the next tool call, and only stops on an explicit awaiting_user."""
    import dev_agent.agent_loop as al

    responses = [
        "Progress note: step 1 applied and verified.\n```json\n{\"loop_status\": \"continue\"}\n```",
        '{"tool": "list_files", "args": {}}',
        "Final report sent.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    events = []
    disp = RecordingDispatcher()

    result = run_agent_loop(
        "flow", _make_skill(), disp, auto_apply=True,
        max_steps=12, on_event=events.append,
    )

    assert result.steps == 3
    assert result.status == "awaiting_user"
    assert [c["tool"] for c in disp.recorded] == ["list_files"]
    reasons = [
        e.get("reason") for e in events
        if e.get("type") == "phase" and e.get("phase") == "auto_continuing"
    ]
    assert "loop_status: continue" in reasons
