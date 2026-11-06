"""Byte-exact snapshots of every payload the agent loop sends to the model.

The loop is being split into steps. That split is only safe if the wire
payload does not change, and "it still looks right" is not an argument - so
this module records every ``send_request`` call of a scenario and compares it
with a snapshot in ``tests/fixtures/loop_golden/``.

What is pinned for each call:

- the outgoing user message (verbatim);
- the assistant dict - system prompt text, service, model, tuning;
- the history window that goes on the wire, in order;
- ``file_context``, ``lang`` and the injection-protection flag;
- the NAMES of the callbacks that were wired (usage, retry, sanitized,
  incomplete), because a refactor that forgets one silently disables token
  accounting or sanitization reporting.

The dispatch side is pinned too: which tool was called with which arguments
and in what order. A refactor that reorders tool execution or drops the
duplicate guard fails here.

Volatile data is NORMALISED, not dropped: a timestamp becomes ``<ts>`` and
the workspace root becomes ``<ws>``. A field that changes type or position
still fails the comparison.

Regenerate after an INTENTIONAL payload change::

    UPDATE_GOLDEN=1 pytest tests/test_loop_golden_payloads.py

The regenerated files are reviewed in the diff - that review is the point.
"""
from __future__ import annotations

import json
import os

import pytest

from dev_agent import agent_loop

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "loop_golden")
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"

#: Every event published during the scenario being recorded. Cleared by
#: ``_install`` so one scenario can never see another scenario's events.
EVENTS: list = []

#: A small assistant dict. The real system prompt is assembled by
#: ``core.orchestrators`` and reaches the loop as ``text``; pinning the
#: passthrough here keeps the snapshot readable and the boundary honest.
ASSISTANT = {
    "text": "SYSTEM PROMPT: you are a test agent.",
    "service": "mock",
    "model": "mock-model",
    "temperature": 0.0,
}


# ── Recording ────────────────────────────────────────────────────────────────


def _normalise(value, ws_root: str):
    """Return *value* with volatile parts replaced by stable markers."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "ts":
                out[key] = "<ts>"
            else:
                out[key] = _normalise(item, ws_root)
        return out
    if isinstance(value, list):
        return [_normalise(item, ws_root) for item in value]
    if isinstance(value, tuple):
        return [_normalise(item, ws_root) for item in value]
    if isinstance(value, str):
        return value.replace(ws_root, "<ws>") if ws_root else value
    return value


def _install(monkeypatch, responses):
    """Replace send_request with a scripted recorder; return the recorded list."""
    sent = []
    script = iter(responses)
    EVENTS.clear()

    def fake_send(user_message, assistant=None, **kwargs):
        sent.append({
            "user_message": user_message,
            "assistant": assistant,
            "file_context": kwargs.get("file_context", ""),
            "history": kwargs.get("history"),
            "lang": kwargs.get("lang"),
            "enable_injection_protection": kwargs.get("enable_injection_protection"),
            "sanitized_approved_paths": sorted(kwargs.get("sanitized_approved_paths") or []),
            "callbacks": sorted(name for name, value in kwargs.items() if callable(value)),
        })
        try:
            return next(script)
        except StopIteration:
            return ""

    monkeypatch.setattr(agent_loop, "send_request", fake_send)
    # The task-state journal and the thread-context block read real process
    # state; a snapshot must not depend on the machine it was taken on.
    monkeypatch.setattr(agent_loop, "_maybe_task_state_context", lambda: None)
    monkeypatch.setattr(agent_loop, "_maybe_thread_context", lambda state: None)
    monkeypatch.setattr(agent_loop, "drain_subagent_notices", lambda state: [])

    # Every event the loop publishes is part of the UI contract: the feed
    # renders phases, tool calls and results from these dicts, and a step
    # split that reorders two emits changes what the user sees. The spy
    # forwards the caller's callback unchanged, so a scenario that wires one
    # still receives it.
    real_step = agent_loop.step_agent_loop

    def spy(state, **kwargs):
        user_cb = kwargs.get("on_event")

        def both(event):
            EVENTS.append(event)
            if callable(user_cb):
                user_cb(event)

        kwargs["on_event"] = both
        return real_step(state, **kwargs)

    monkeypatch.setattr(agent_loop, "step_agent_loop", spy)
    return sent


class RecordingDispatcher:
    """A dispatcher that returns canned results and records every call."""

    def __init__(self, canned=None):
        self.canned = canned or {}
        self.calls = []

    def _result(self, tool):
        value = self.canned.get(tool)
        if isinstance(value, dict):
            return dict(value)
        if value is not None:
            return value
        return {"ok": True, "tool": tool, "message": "ok"}

    def dispatch(self, tool, args):
        self.calls.append({"via": "dispatch", "tool": tool, "args": _copy(args)})
        return self._result(tool)

    def dispatch_json(self, call):
        self.calls.append({"via": "dispatch_json", "tool": call.get("tool"), "args": _copy(call.get("args"))})
        return self._result(call.get("tool"))


def _copy(value):
    """Best-effort copy of tool arguments for the snapshot."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return value


def _result_view(result):
    """Return the deterministic projection of an AgentResult."""
    return {
        "status": result.status,
        "text": result.text,
        "steps": result.steps,
        "applied": result.applied,
        "discarded": result.discarded,
        "staged_path": result.staged_path,
        "staged_tool": result.staged_tool,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "history_len": len(result.history),
        "pending_confirmation_tool": result.pending_confirmation_tool,
    }


def _drive(state, dispatcher, steps=8):
    """Run step_agent_loop until it parks, and return the final state."""
    for _ in range(steps):
        if state.phase in ("done", "error"):
            break
        state = agent_loop.step_agent_loop(state, dispatcher=dispatcher)
    return state


# ── Scenarios ────────────────────────────────────────────────────────────────


def scenario_read_tool(monkeypatch):
    """One tool call, its result, then a final answer."""
    sent = _install(monkeypatch, [
        '```json\n{"tool": "read_file", "args": {"path": "module.py"}}\n```',
        "The file defines one constant. Final answer.",
    ])
    dispatcher = RecordingDispatcher({
        "read_file": {"ok": True, "tool": "read_file", "path": "module.py", "content": "x = 1\n"},
    })
    result = agent_loop.run_agent_loop("Read module.py", ASSISTANT, dispatcher, max_steps=6)
    return {"sent": sent, "dispatched": dispatcher.calls, "result": _result_view(result)}


def scenario_batch_calls(monkeypatch):
    """Two independent tool calls in one message execute in order."""
    sent = _install(monkeypatch, [
        ('```json\n{"tool": "list_files", "args": {"subdir": "."}}\n```\n'
         '```json\n{"tool": "read_file", "args": {"path": "a.py"}}\n```'),
        "Both calls are done. Final answer.",
    ])
    dispatcher = RecordingDispatcher({
        "list_files": {"ok": True, "tool": "list_files", "files": ["a.py"]},
        "read_file": {"ok": True, "tool": "read_file", "path": "a.py", "content": "y = 2\n"},
    })
    result = agent_loop.run_agent_loop("List and read", ASSISTANT, dispatcher, max_steps=6)
    return {"sent": sent, "dispatched": dispatcher.calls, "result": _result_view(result)}


def scenario_propose_staged(monkeypatch):
    """propose_file without auto_apply parks the step for user approval."""
    sent = _install(monkeypatch, [
        '```json\n{"tool": "propose_file", "args": {"path": "new.py", "content": "z = 3\n"}}\n```',
    ])
    dispatcher = RecordingDispatcher({
        "propose_file": {"ok": True, "applied": False, "path": "new.py", "diff": "+z = 3"},
    })
    result = agent_loop.run_agent_loop("Write new.py", ASSISTANT, dispatcher, max_steps=4)
    return {"sent": sent, "dispatched": dispatcher.calls, "result": _result_view(result)}


def scenario_plan_awaiting(monkeypatch):
    """A numbered plan in prose stops the loop for approval."""
    sent = _install(monkeypatch, [
        "План:\n1. Прочитать модуль.\n2. Разделить функцию.\nУтверждаете план?",
    ])
    dispatcher = RecordingDispatcher()
    result = agent_loop.run_agent_loop("Do the work", ASSISTANT, dispatcher, max_steps=4)
    return {"sent": sent, "dispatched": dispatcher.calls, "result": _result_view(result)}


def scenario_tool_error_feedback(monkeypatch):
    """A failing tool feeds a structured result back and the loop continues."""
    sent = _install(monkeypatch, [
        '```json\n{"tool": "apply_patch", "args": {"path": "a.py", "edits": []}}\n```',
        "The patch failed; I will switch tools. Final answer.",
    ])
    dispatcher = RecordingDispatcher({
        "apply_patch": {"ok": False, "error": "anchor not found"},
    })
    result = agent_loop.run_agent_loop("Patch a.py", ASSISTANT, dispatcher, max_steps=6)
    return {"sent": sent, "dispatched": dispatcher.calls, "result": _result_view(result)}


def scenario_dsml_fallback(monkeypatch):
    """A raw DSML call is parsed, validated and executed."""
    mark = "\uff5c\uff5cDSML\uff5c\uff5c"
    call = (
        '<' + mark + ' invoke name="read_file">\n'
        '<' + mark + ' parameter name="path">dsml.py</' + mark + ' parameter>\n'
        '</' + mark + ' invoke>'
    )
    sent = _install(monkeypatch, [call, "Read through the fallback dialect. Final answer."])
    dispatcher = RecordingDispatcher({
        "read_file": {"ok": True, "tool": "read_file", "path": "dsml.py", "content": "w = 4\n"},
    })
    result = agent_loop.run_agent_loop("Read dsml.py", ASSISTANT, dispatcher, max_steps=6)
    return {"sent": sent, "dispatched": dispatcher.calls, "result": _result_view(result)}


def scenario_manual_mode_gated(monkeypatch):
    """apply_edit is refused in manual mode without touching the dispatcher."""
    sent = _install(monkeypatch, [
        '```json\n{"tool": "apply_edit", "args": {"path": "a.py"}}\n```',
        "I must not apply it myself. Final answer.",
    ])
    dispatcher = RecordingDispatcher()
    result = agent_loop.run_agent_loop("Apply a.py", ASSISTANT, dispatcher, max_steps=6, auto_apply=False)
    return {"sent": sent, "dispatched": dispatcher.calls, "result": _result_view(result)}


def scenario_economy_window(monkeypatch):
    """Economy mode trims the sent window while the history keeps growing.

    Four model calls, three tool executions: by the last call the history is
    longer than the configured tail, so the sent window is a strict suffix of
    it. A step split that rebuilt the window differently (or dropped the
    ``economy_tail_messages`` override) changes these snapshots.
    """
    sent = _install(monkeypatch, [
        '```json\n{"tool": "read_file", "args": {"path": "e1.py"}}\n```',
        '```json\n{"tool": "read_file", "args": {"path": "e2.py"}}\n```',
        '```json\n{"tool": "read_file", "args": {"path": "e3.py"}}\n```',
        "Final answer after economy trimming.",
    ])
    dispatcher = RecordingDispatcher({
        "read_file": {"ok": True, "tool": "read_file", "path": "e.py", "content": "v = 5\n"},
    })
    state = agent_loop.AgentLoopState(
        task="Economy task",
        max_steps=8,
        strong_assistant=dict(ASSISTANT),
        weak_assistant=dict(ASSISTANT),
        economy_mode=True,
        economy_tail_messages=2,
    )
    state.user_message = state.task
    state.phase = "calling_llm"
    state = _drive(state, dispatcher, steps=24)
    return {"sent": sent, "dispatched": dispatcher.calls,
            "history_len": len(state.history),
            "result": _result_view(
                agent_loop.AgentResult(status=state.final_status, text=state.final_text,
                                       history=state.history, steps=state.steps,
                                       error_code=state.error_code,
                                       error_message=state.error_message))}


SCENARIOS = {
    "read_tool": scenario_read_tool,
    "batch_calls": scenario_batch_calls,
    "propose_staged": scenario_propose_staged,
    "plan_awaiting": scenario_plan_awaiting,
    "tool_error_feedback": scenario_tool_error_feedback,
    "dsml_fallback": scenario_dsml_fallback,
    "manual_mode_gated": scenario_manual_mode_gated,
    "economy_window": scenario_economy_window,
}


# ── The check ────────────────────────────────────────────────────────────────


def _workspace_root() -> str:
    """Return the project root as the loop sees it, or an empty string."""
    try:
        from dev_agent import config as dev_config
        return str(dev_config.PROJECT_ROOT)
    except Exception:  # pragma: no cover - configuration is optional here
        return ""


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_loop_payload_snapshot(name, monkeypatch):
    """The recorded payload of one scenario matches its snapshot byte for byte."""
    recorded = SCENARIOS[name](monkeypatch)
    recorded = _normalise(recorded, _workspace_root())
    # Events are recorded by the spy installed in ``_install``; they are part
    # of the observable contract and are compared with the same rules.
    recorded["events"] = _normalise(list(EVENTS), _workspace_root())
    path = os.path.join(GOLDEN_DIR, name + ".json")

    if UPDATE:
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(recorded, handle, ensure_ascii=False, indent=1, sort_keys=False)
            handle.write("\n")
        pytest.skip("snapshot regenerated: " + path)

    assert os.path.isfile(path), (
        "no snapshot for %r - run UPDATE_GOLDEN=1 pytest tests/test_loop_golden_payloads.py" % name
    )
    with open(path, encoding="utf-8") as handle:
        expected = json.load(handle)

    assert recorded == expected, _first_difference(expected, recorded, name)


def _first_difference(expected, actual, name) -> str:
    """Return a readable report of the first divergence between two snapshots."""
    expected_text = json.dumps(expected, ensure_ascii=False, indent=1, sort_keys=False)
    actual_text = json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=False)
    if expected_text == actual_text:
        return "snapshot %r differs in a way json cannot show" % name
    exp_lines = expected_text.splitlines()
    act_lines = actual_text.splitlines()
    for index in range(min(len(exp_lines), len(act_lines))):
        if exp_lines[index] != act_lines[index]:
            return (
                "snapshot %r diverges at line %d\n  expected: %s\n  actual:   %s"
                % (name, index + 1, exp_lines[index], act_lines[index])
            )
    return (
        "snapshot %r changed length: %d lines expected, %d recorded"
        % (name, len(exp_lines), len(act_lines))
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
