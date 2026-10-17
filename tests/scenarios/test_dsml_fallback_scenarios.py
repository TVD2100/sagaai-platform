"""tests/scenarios/test_dsml_fallback_scenarios.py - user-level scenarios
for the DSML fallback tool-call dialect.

DeepSeek models periodically emit tool calls in their reserved-token dialect
instead of the fenced-JSON contract:

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="read_file">
    <｜｜DSML｜｜ parameter name="args" string="false">{"path": "a.py"}</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>
    </｜｜DSML｜｜ calls>

Scenarios (given -> when -> then):

  Scenario 1 - a dialog message in that dialect (argument object wrapped in
               ``name="args"``, closing brace duplicated) is parsed and the
               tool actually EXECUTES with the real arguments, so the step is
               not lost to a format complaint.

  Scenario 2 - a batch of two marker-dialect calls executes both calls in
               their emission order.

  Scenario 3 - truncated DSML markup (invoke tag without any parameter) is
               NOT executed with empty arguments: the model gets the format
               guidance back and the loop continues to a clean stop.

  Scenario 4 - the same assistant message is rendered in the chat without
               any leftover ``DSML`` markup.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent.agent_loop import parse_tool_calls, run_agent_loop  # noqa: E402
from ui.pages.orchestrator import _strip_tool_calls  # noqa: E402

MARK = "\uff5c\uff5cDSML\uff5c\uff5c"


def _dsml_call(tool: str, args_json: str) -> str:
    """Return one marker-dialect call exactly as the model emitted it."""
    return (
        f'<{MARK} invoke name="{tool}">\n'
        f'<{MARK} parameter name="args" string="false">{args_json}</{MARK} parameter>\n'
        f'</{MARK} invoke>\n'
    )


def _dsml_block(*calls: str) -> str:
    return f'<{MARK} calls>\n' + "".join(calls) + f'</{MARK} calls>\n'


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
    """Dispatcher that records every executed call."""

    def __init__(self, canned=None):
        self.canned = canned or {}
        self.recorded = []

    def dispatch_json(self, call):
        self.recorded.append(call)
        tool = call["tool"]
        if tool in self.canned:
            return self.canned[tool]
        return {"ok": True, "tool": tool}

    def dispatch(self, tool, args=None):
        self.recorded.append({"tool": tool, "args": args or {}})
        if tool in self.canned:
            return self.canned[tool]
        return {"ok": True, "tool": tool, "message": "ok", "applied": True}


def test_scenario_marker_call_executes_with_real_arguments(monkeypatch):
    """Given a DSML message whose arguments sit in one wrapped JSON object
    (with a duplicated closing brace), when the loop runs, then the tool is
    dispatched with the real arguments - the step is not lost."""
    import dev_agent.agent_loop as al

    message = (
        "{\"path\": \"tests/conftest.py\"}}"
    )
    first = "Reading the fixtures file.\n" + _dsml_block(
        _dsml_call("read_file", message)
    )
    responses = [
        first,
        "Done.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop("read fixtures", _make_skill(), disp, auto_apply=True, max_steps=8)

    assert [c["tool"] for c in disp.recorded] == ["read_file"], disp.recorded
    assert disp.recorded[0]["args"] == {"path": "tests/conftest.py"}
    assert result.status == "awaiting_user"


def test_scenario_marker_batch_executes_in_order(monkeypatch):
    """Given a batch of two marker-dialect calls, when the loop runs, then
    both execute in emission order with complete arguments."""
    import dev_agent.agent_loop as al

    first = _dsml_block(
        _dsml_call("read_file", '{"path": "a.py"}'),
        _dsml_call("read_file", '{"path": "b.py", "limit": 20}'),
    )
    responses = [
        first,
        "Both read.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop("batch read", _make_skill(), disp, auto_apply=True, max_steps=8)

    assert [c["args"] for c in disp.recorded] == [
        {"path": "a.py"},
        {"path": "b.py", "limit": 20},
    ]
    assert result.status == "awaiting_user"


def test_scenario_truncated_marker_call_is_not_executed_empty(monkeypatch):
    """Given an invoke tag with no parameter block, when the loop runs, then
    nothing is executed with empty arguments and the model receives the
    format guidance."""
    import dev_agent.agent_loop as al

    truncated = f'<{MARK} calls>\n<{MARK} invoke name="run_test">\n</{MARK} invoke>\n</{MARK} calls>\n'
    responses = [
        truncated,
        "Retrying as JSON.\n```json\n{\"tool\": \"run_test\", \"args\": {\"path\": \"tests/x.py\"}}\n```",
        "Green.\n```json\n{\"loop_status\": \"awaiting_user\"}\n```",
    ]
    monkeypatch.setattr(al, "send_request", _scripted_send(responses))
    disp = RecordingDispatcher()

    result = run_agent_loop("run test", _make_skill(), disp, auto_apply=True, max_steps=8)

    empty = [c for c in disp.recorded if not c.get("args")]
    assert empty == [], empty
    assert [c["args"] for c in disp.recorded] == [{"path": "tests/x.py"}]
    joined = "\n".join(m.get("content", "") for m in result.history)
    assert "dsml_invalid" in joined


def test_scenario_chat_renders_no_marker_markup():
    """Given an assistant message with DSML markup, when the message is
    rendered for the chat, then no DSML markup is left behind."""
    text = "Готово.\n\n" + _dsml_block(_dsml_call("run_test", '{"path": "tests/x.py"}'))
    rendered = _strip_tool_calls(text)
    assert "DSML" not in rendered
    assert rendered.strip() == "Готово."
    # The parser still sees the call in the ORIGINAL text.
    assert parse_tool_calls(text)[0]["args"] == {"path": "tests/x.py"}
