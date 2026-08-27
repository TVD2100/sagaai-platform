"""
Scenario tests for the automatic repair of truncated tool-call JSON,
walking the agent loop end-to-end like a developer would:
  - happy path: a tool call cut off mid-JSON is recovered, dispatched
    and the loop finishes normally;
  - edge case: a deeply truncated call inside a closed ```json fence
    is recovered as well;
  - error state: a truncation inside a string cannot be repaired, so
    no tool is dispatched and the loop stops awaiting the user.
"""
import os
import sys

import pytest

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
    state = {"it": iter(responses)}

    def _send(*args, **kwargs):
        try:
            return next(state["it"])
        except StopIteration:
            return ""

    return _send


class RecordingDispatcher:
    """A dispatcher that records every tool call it receives."""

    def __init__(self):
        self.recorded = []

    def dispatch_json(self, call: dict) -> dict:
        self.recorded.append(call)
        return {"ok": True, "tool": call["tool"], "message": "ok"}

    def dispatch(self, tool: str, args: dict) -> dict:
        return {"ok": True, "tool": tool, "message": "ok"}


def test_scenario_happy_path_truncated_call_is_repaired_and_executed(monkeypatch):
    """Given an LLM reply whose tool call is cut off mid-JSON, when the
    loop runs, then the call is repaired, dispatched, and the loop ends
    with status 'done'."""
    import dev_agent.agent_loop as al

    responses = [
        '{"tool": "list_files", "args": {"subdir": "."}',
        "All plan steps completed.",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop("list", _make_skill(), disp, auto_apply=True)

    assert result.status == "done"
    assert len(disp.recorded) == 1
    assert disp.recorded[0]["tool"] == "list_files"
    assert disp.recorded[0]["args"] == {"subdir": "."}
    assert disp.recorded[0]["_json_repaired"] == 1


def test_scenario_edge_case_truncation_inside_closed_fence(monkeypatch):
    """Given a tool call truncated inside a ```json fence that IS closed,
    when the loop runs, then the inner brace is appended and the call
    is dispatched."""
    import dev_agent.agent_loop as al

    responses = [
        '```json\n{"tool": "list_files", "args": {}\n```',
        "All plan steps completed.",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop("list", _make_skill(), disp, auto_apply=True)

    assert result.status == "done"
    assert len(disp.recorded) == 1
    assert disp.recorded[0]["tool"] == "list_files"
    assert disp.recorded[0]["args"] == {}
    assert disp.recorded[0]["_json_repaired"] == 1


def test_scenario_error_state_unrepairable_truncation_stops_loop(monkeypatch):
    """Given a truncation inside a JSON string (no safe repair), when
    the loop runs, then no tool is dispatched and the loop stops
    awaiting the user instead of crashing."""
    import dev_agent.agent_loop as al

    responses = ['{"tool": "list_files", "args": {"subdir": ".']
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop("list", _make_skill(), disp, auto_apply=True)

    assert result.status == "awaiting_user"
    assert disp.recorded == []
