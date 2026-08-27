"""
Tests for dev_agent.agent_loop - parse_tool_calls, step_agent_loop, helpers.
Uses monkeypatch to replace send_request with a deterministic script.
No Streamlit dependency.
"""
from __future__ import annotations

import json

import pytest

from dev_agent.agent_loop import (
    AgentLoopState,
    AgentResult,
    parse_tool_calls,
    run_agent_loop,
    step_agent_loop,
    approve_and_apply,
    discard,
    _prose_looks_like_question,
    _parse_loop_status,
    build_economy_context,
    carry_over_economy_cache,
    economy_cache_to_dict,
    apply_economy_cache,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_skill() -> dict:
    return {
        "text": "system prompt",
        "service": "mock",
        "model": "mock-model",
        "temperature": 0.1,
    }


def _scripted_send(responses: list):
    """Return a mock send_request that returns items from *responses* in order.

    No task classification phase - responses are consumed directly.
    """
    state = {"it": iter(responses)}

    def _send(*args, **kwargs):
        try:
            return next(state["it"])
        except StopIteration:
            return ""

    return _send


class FakeDispatcher:
    """A dispatcher that returns canned results for tool calls."""

    def __init__(self, canned: dict | None = None):
        self.canned = canned or {}

    def dispatch_json(self, call: dict) -> dict:
        tool = call["tool"]
        if tool in self.canned:
            return self.canned[tool]
        return {"ok": True, "tool": tool, "message": "ok"}

    def dispatch(self, tool: str, args: dict) -> dict:
        if tool in self.canned:
            return self.canned[tool]
        return {"ok": True, "tool": tool, "message": "ok"}


# ── _prose_looks_like_question ────────────────────────────────────────────────

class TestProseQuestion:
    def test_plan_approval_question_russian(self):
        text = "Утверждаете план? (напишите «ок», «да», «go» - и я начну реализацию)"
        assert _prose_looks_like_question(text) is True

    def test_plan_approval_question_english(self):
        text = "Approve? (write 'ok', 'yes', or 'go' and I'll start implementation)"
        assert _prose_looks_like_question(text) is True

    def test_shall_i_continue(self):
        text = "Shall I continue to the next step?"
        assert _prose_looks_like_question(text) is True

    def test_no_question_mark(self):
        text = "Here is my plan. Please approve."
        assert _prose_looks_like_question(text) is False

    def test_question_without_confirmation_markers(self):
        text = "What is the capital of France?"
        assert _prose_looks_like_question(text) is False

    def test_empty_text(self):
        assert _prose_looks_like_question("") is False
        assert _prose_looks_like_question(None) is False

    def test_intermediate_report_not_a_question(self):
        text = "Intermediate report. All steps are proceeding well."
        assert _prose_looks_like_question(text) is False


# ── parse_tool_calls ────────────────────────────────────────────────────────

class TestParse:
    def test_parse_empty(self):
        assert parse_tool_calls("") == []
        assert parse_tool_calls(None) == []

    def test_parse_json_fenced(self):
        text = '```json\n{"tool": "read_file", "args": {"path": "x.py"}}\n```'
        result = parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["tool"] == "read_file"
        assert result[0]["args"]["path"] == "x.py"

    def test_parse_plain_json(self):
        text = 'Prefix text {\n  "tool": "list_files",\n  "args": {}\n} suffix'
        result = parse_tool_calls(text)
        assert len(result) >= 1
        assert any(c["tool"] == "list_files" for c in result)

    def test_parse_prose_then_json(self):
        text = 'Let me read that file.\n```json\n{"tool":"read_file","args":{"path":"a.py"}}\n```'
        result = parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["tool"] == "read_file"

    def test_parse_multiple_blocks(self):
        text = (
            '```json\n{"tool":"read_file","args":{"path":"a.py"}}\n```\n'
            '```json\n{"tool":"read_file","args":{"path":"b.py"}}\n```'
        )
        result = parse_tool_calls(text)
        assert len(result) == 2

    def test_parse_flat_keys(self):
        """JSON without explicit tool name is discarded by normalize_call."""
        text = '{"path": "x.py", "content": "hello"}'
        result = parse_tool_calls(text)
        # normalize_call returns None for such objects → no tool calls.
        assert len(result) == 0

    def test_parse_name_key(self):
        text = '{"name": "read_file", "args": {"path": "x.py"}}'
        result = parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["tool"] == "read_file"
        assert result[0]["args"]["path"] == "x.py"

    def test_parse_arguments_key(self):
        text = '{"tool": "read_file", "arguments": {"path": "x.py"}}'
        result = parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["tool"] == "read_file"
        assert result[0]["args"]["path"] == "x.py"

    def test_parse_braces_in_string(self):
        text = '{"tool":"read_file","args":{"path":"test_{id}.py"}}'
        result = parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["args"]["path"] == "test_{id}.py"


# ── agent loop (via run_agent_loop) ──────────────────────────────────────────

class TestLoop:

    def test_loop_basic_single_turn(self, monkeypatch):
        """A tool call followed by empty prose stops with awaiting_user."""
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "list_files", "args": {"subdir": "."}}\n```',
            "",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        disp = FakeDispatcher()
        result = run_agent_loop("list", _make_skill(), disp)
        assert result.status == "awaiting_user"

    def test_loop_max_steps(self, monkeypatch):
        import dev_agent.agent_loop as al
        responses = ['```json\n{"tool": "list_files", "args": {}}\n```'] * 110
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        disp = FakeDispatcher()
        result = run_agent_loop("list", _make_skill(), disp, max_steps=3)
        assert result.status == "stopped_max_steps"
        assert result.steps == 3

    def test_loop_error_propagation(self, monkeypatch):
        import dev_agent.agent_loop as al
        monkeypatch.setattr(al, "send_request", lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")))
        result = run_agent_loop("fail", _make_skill(), FakeDispatcher())
        assert result.status == "error"
        assert "boom" in result.text

    def test_loop_tool_exception_does_not_stop_loop(self, monkeypatch):
        """A tool that RAISES during dispatch (e.g. search_in_files called with
        an unknown argument) must NOT kill the loop: the failure is fed back
        as a structured ok=False tool_result and the loop continues normally."""
        import dev_agent.agent_loop as al

        class RaisingDispatcher(FakeDispatcher):
            def dispatch_json(self, call):
                if call["tool"] == "search_in_files":
                    raise TypeError(
                        "search_in_files() got an unexpected keyword argument 'pathh'"
                    )
                return super().dispatch_json(call)

        responses = [
            '```json\n{"tool": "search_in_files", "args": {"query": "x", "pathh": "y"}}\n```',
            "All plan steps completed.",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        disp = RaisingDispatcher()
        result = run_agent_loop("search", _make_skill(), disp, auto_apply=True)
        # The loop continued to the second LLM turn and finished normally.
        assert result.status == "done"
        assert result.steps == 2
        # The LLM saw a structured error describing the cause.
        tool_results = [
            m["content"] for m in result.history
            if m["role"] == "user" and m["content"].startswith('{"tool_result"')
        ]
        assert tool_results
        last = json.loads(tool_results[-1])
        tr = last["tool_result"]
        assert tr["ok"] is False
        assert "search_in_files" in tr["error"]
        assert tr["invalid_arguments"] is True

    def test_loop_awaiting_plan_ok_manual(self, monkeypatch):
        """First-step prose-only response in MANUAL mode triggers awaiting_plan_ok."""
        import dev_agent.agent_loop as al
        monkeypatch.setattr(al, "send_request", _scripted_send(["Here is my plan..."]))
        result = run_agent_loop("refactor", _make_skill(), FakeDispatcher(), auto_apply=False)
        assert result.status == "awaiting_user"

    def test_loop_auto_approve_plan_now_awaits_plan_ok(self, monkeypatch):
        """NOW: first-step prose-only response in AUTONOMOUS mode also triggers
        awaiting_plan_ok (no more auto-approve of plans)."""
        import dev_agent.agent_loop as al
        responses = [
            "Here is my plan: step 1, step 2...",
            "All plan steps completed.",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("refactor", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"
        # Only first response consumed; plan was NOT auto-approved.
        assert result.steps == 1

    def test_loop_manual_approval(self, monkeypatch):
        """propose_file with auto_apply=False triggers awaiting_approval."""
        import dev_agent.agent_loop as al
        responses = ['```json\n{"tool": "propose_file", "args": {"path": "x.py", "content": "1"}}\n```']
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        disp = FakeDispatcher(canned={
            "propose_file": {"ok": True, "path": "x.py", "diff": "old\n\nnew", "new_text": "1"},
        })
        result = run_agent_loop("edit", _make_skill(), disp, auto_apply=False)
        assert result.status == "awaiting_approval"
        assert result.staged_path == "x.py"

    def test_loop_auto_apply(self, monkeypatch):
        """propose_file with auto_apply=True applies immediately and continues."""
        import dev_agent.agent_loop as al

        responses = [
            '```json\n{"tool": "propose_file", "args": {"path": "y.py", "content": "y = 2\\n"}}\n```',
            "All plan steps completed.",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))

        dispatcher = FakeDispatcher(canned={
            "propose_file": {"ok": True, "path": "y.py", "diff": "-old\n+new",
                             "new_text": "y = 2\n", "applied": True},
        })

        result = al.run_agent_loop("Edit", _make_skill(), dispatcher, auto_apply=True)

        assert result.status == "done"

    def test_loop_pending_apply(self, monkeypatch):
        import dev_agent.agent_loop as al
        monkeypatch.setattr(al, "send_request", _scripted_send([]))
        state = AgentLoopState(
            task="",
            auto_apply=False,
            strong_assistant=_make_skill(),
            weak_assistant=_make_skill(),
            pending_action="apply",
            pending_staged_path="x.py",
            staged_new_text="new content",
            staged_tool="propose_file",
        )
        disp = FakeDispatcher(canned={
            "propose_file": {"ok": True, "path": "x.py", "diff": "d", "new_text": "new content"},
            "apply_edit": {"ok": True, "backup_version": 1, "message": "applied"},
        })
        state = step_agent_loop(state, dispatcher=disp)
        assert state.phase == "done"
        assert state.final_status == "applied"
        assert state.final_applied is True

    def test_loop_pending_discard(self, monkeypatch):
        import dev_agent.agent_loop as al
        monkeypatch.setattr(al, "send_request", _scripted_send([]))
        state = AgentLoopState(
            task="",
            auto_apply=False,
            strong_assistant=_make_skill(),
            weak_assistant=_make_skill(),
            pending_action="discard",
            pending_staged_path="x.py",
        )
        disp = FakeDispatcher(canned={
            "discard_edit": {"ok": True, "message": "discarded"},
        })
        state = step_agent_loop(state, dispatcher=disp)
        assert state.phase == "done"
        assert state.final_status == "discarded"
        assert state.final_discarded is True

    def test_loop_pending_apply_history_indexed(self, monkeypatch):
        import dev_agent.agent_loop as al
        monkeypatch.setattr(al, "send_request", _scripted_send([]))
        state = AgentLoopState(
            task="",
            auto_apply=False,
            strong_assistant=_make_skill(),
            weak_assistant=_make_skill(),
            pending_action="apply",
            pending_staged_path="x.py",
            staged_new_text="new content",
            staged_tool="propose_file",
        )
        disp = FakeDispatcher(canned={
            "propose_file": {"ok": True, "path": "x.py", "diff": "d", "new_text": "new content"},
            "apply_edit": {"ok": True, "backup_version": 1, "message": "applied"},
        })
        state = step_agent_loop(state, dispatcher=disp)
        assert state.final_status == "applied"
        # Result-desync fix: every history entry must carry the index of its
        # own position so get_history_messages() resolves indices correctly.
        assert [m.get("_index") for m in state.history] == list(range(len(state.history)))

    def test_loop_pending_discard_history_indexed(self, monkeypatch):
        import dev_agent.agent_loop as al
        monkeypatch.setattr(al, "send_request", _scripted_send([]))
        state = AgentLoopState(
            task="",
            auto_apply=False,
            strong_assistant=_make_skill(),
            weak_assistant=_make_skill(),
            pending_action="discard",
            pending_staged_path="x.py",
        )
        disp = FakeDispatcher(canned={
            "discard_edit": {"ok": True, "message": "discarded"},
        })
        state = step_agent_loop(state, dispatcher=disp)
        assert state.final_status == "discarded"
        # Same result-desync guarantee as for the apply path.
        assert [m.get("_index") for m in state.history] == list(range(len(state.history)))

    def test_loop_resumed_history_starts_next_index_after_existing(self, monkeypatch):
        import dev_agent.agent_loop as al
        existing = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "working..."},
        ]
        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            "",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        # run_agent_loop mutates the passed list in place; give it a copy so
        # `existing` keeps its original length for the assertions below.
        result = run_agent_loop("list", _make_skill(), FakeDispatcher(),
                                history=[dict(m) for m in existing])
        # New entries appended after a resumed session must continue the
        # index sequence instead of restarting at 0 (index/position desync),
        # and the pre-existing entries must stay at the front.
        assert [m["content"] for m in result.history[:2]] == ["hello", "working..."]
        assert [m.get("_index") for m in result.history] == list(range(len(result.history)))

    def test_loop_events_emitted(self, monkeypatch):
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            "",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        events = []
        result = run_agent_loop("list", _make_skill(), FakeDispatcher(), on_event=events.append)
        assert result.status == "awaiting_user"
        types = [e["type"] for e in events]
        assert "tool_call" in types
        assert "tool_result" in types

    def test_loop_history_grows(self, monkeypatch):
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            '```json\n{"tool": "read_file", "args": {"path": "x.py"}}\n```',
            "",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("check", _make_skill(), FakeDispatcher())
        assert result.status == "awaiting_user"
        user_msgs = [m for m in result.history if m["role"] == "user"]
        assert len(user_msgs) >= 3

    def test_loop_history_continuity(self, monkeypatch):
        import dev_agent.agent_loop as al
        existing = [{"role": "user", "content": "hello"}]
        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            "",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("task", _make_skill(), FakeDispatcher(),
                                history=list(existing))
        assert result.status == "awaiting_user"
        assert result.history[0] == existing[0]
        # History includes existing + task message + assistant + tool result + assistant
        assert len(result.history) >= len(existing) + 2

    def test_approve_and_apply_calls_dispatcher(self):
        canned = {"apply_edit": {"ok": True, "backup_version": 2}}
        disp = FakeDispatcher(canned)
        res = approve_and_apply("x.py", disp)
        assert res == canned["apply_edit"]

    def test_approve_and_apply_no_note(self):
        canned = {"apply_edit": {"ok": True}}
        disp = FakeDispatcher(canned)
        res = approve_and_apply("x.py", disp)
        assert res["ok"] is True

    def test_discard_calls_dispatcher(self):
        canned = {"discard_edit": {"ok": True}}
        disp = FakeDispatcher(canned)
        res = discard("x.py", disp)
        assert res == canned["discard_edit"]


# ── DSML parsing ─────────────────────────────────────────────────────────────

class TestDSML:
    def test_parse_dsml_single_call(self):
        from dev_agent.agent_loop import _extract_dsml_calls
        text = '<invoke name="read_file"><parameter name="path">x.py</parameter></invoke>'
        calls = _extract_dsml_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "read_file"
        assert calls[0].get("path") == "x.py" or calls[0].get("args", {}).get("path") == "x.py"

    def test_parse_dsml_multiple_calls(self):
        from dev_agent.agent_loop import _extract_dsml_calls
        text = (
            '<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>'
            '<invoke name="list_files"><parameter name="subdir">.</parameter></invoke>'
        )
        calls = _extract_dsml_calls(text)
        assert len(calls) == 2

    def test_parse_dsml_falls_back_only_when_no_json(self):
        text = '```json\n{"tool": "read_file", "args": {"path": "x.py"}}\n```'
        result = parse_tool_calls(text)
        assert len(result) == 1
        assert result[0]["tool"] == "read_file"

    def test_parse_dsml_with_single_pipes(self):
        from dev_agent.agent_loop import _extract_dsml_calls
        text = '<invoke name="read_file"><parameter name="path">|x|.py</parameter></invoke>'
        calls = _extract_dsml_calls(text)
        assert len(calls) == 1
        assert calls[0].get("path") == "|x|.py" or calls[0].get("args", {}).get("path") == "|x|.py"

    def test_parse_dsml_coerces_typed_parameters(self):
        from dev_agent.agent_loop import _extract_dsml_calls
        text = (
            '<invoke name="read_file">'
            '<parameter name="path" string="true">core/api_layer.py</parameter>'
            '<parameter name="offset" string="false">30</parameter>'
            '<parameter name="limit" string="false">30</parameter>'
            '</invoke>'
        )
        calls = _extract_dsml_calls(text)
        assert len(calls) == 1
        args = calls[0]["args"]
        assert args["path"] == "core/api_layer.py"
        assert args["offset"] == 30 and isinstance(args["offset"], int)
        assert args["limit"] == 30 and isinstance(args["limit"], int)

    def test_parse_dsml_with_tool_calls_wrapper(self):
        from dev_agent.agent_loop import _extract_dsml_calls
        text = (
            '<tool_calls>\n'
            '<invoke name="list_files">\n'
            '<parameter name="subdir">.</parameter>\n'
            '</invoke>\n'
            '</tool_calls>'
        )
        calls = _extract_dsml_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "list_files"
        assert calls[0]["args"]["subdir"] == "."


# ── manual vs autonomous mode ────────────────────────────────────────────────

class TestManualMode:
    def test_manual_mode_rejects_self_apply_edit(self, monkeypatch):
        """apply_edit called by model in manual mode → awaiting_plan_ok
        (no tool call parsed from bare JSON without tool key)."""
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "apply_edit", "args": {"path": "x.py"}}\n```',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        disp = FakeDispatcher()
        result = run_agent_loop("apply", _make_skill(), disp, auto_apply=False)
        # In manual mode, first step with tool call that gets filtered →
        # the response is still consumed, but apply_edit is rejected by
        # the _USER_GATED guard. After the guard rejection, the tool result
        # error is fed back and the loop stops with awaiting_user or error.
        # The key assertion: result is NOT applied.
        assert result.status in ("error", "awaiting_user")

    def test_manual_mode_rejects_self_discard_edit(self, monkeypatch):
        """discard_edit called by model in manual mode → rejected."""
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "discard_edit", "args": {"path": "x.py"}}\n```',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        disp = FakeDispatcher()
        result = run_agent_loop("discard", _make_skill(), disp, auto_apply=False)
        assert result.status in ("error", "awaiting_user")

    def test_autonomous_mode_apply_still_filtered_from_model(self, monkeypatch):
        """apply_edit/discard_edit called by the model in auto_apply mode
        should still be filtered (model shouldn't call them)."""
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "apply_edit", "args": {"path": "x.py"}}\n```',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        disp = FakeDispatcher()
        result = run_agent_loop("apply_now", _make_skill(), disp, auto_apply=True, max_steps=2)
        assert result.status == "stopped_max_steps"

    def test_loop_propose_file_awaits_approval_in_manual(self, monkeypatch):
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "propose_file", "args": {"path": "x.py", "content": "a"}}\n```',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        disp = FakeDispatcher(canned={
            "propose_file": {"ok": True, "path": "x.py", "diff": "d", "new_text": "a"},
        })
        result = run_agent_loop("fix", _make_skill(), disp, auto_apply=False)
        assert result.status == "awaiting_approval"
        assert result.staged_path == "x.py"

    def test_loop_propose_file_auto_apply(self, monkeypatch):
        """In autonomous mode propose_file immediately triggers apply_edit."""
        import dev_agent.agent_loop as al

        responses = [
            '```json\n{"tool": "propose_file", "args": {"path": "y.py", "content": "y=2\\n"}}\n```',
            "All plan steps completed.",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))

        dispatcher = FakeDispatcher(canned={
            "propose_file": {"ok": True, "path": "y.py", "diff": "-old\n+y=2",
                             "new_text": "y=2\n", "applied": True},
        })

        result = al.run_agent_loop(
            "Rewrite", _make_skill(), dispatcher, auto_apply=True
        )
        assert result.status == "done"

    def test_loop_auto_continue_without_final_phrase(self, monkeypatch):
        """In autonomous mode, a prose response (AFTER the first step) triggers
        an automatic Continue, then the next response with final phrase stops."""
        import dev_agent.agent_loop as al

        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            "Intermediate report.",
            "All plan steps completed.",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))

        result = al.run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "done"
        assert result.steps >= 3

    def test_loop_auto_stops_on_user_question(self, monkeypatch):
        """In autonomous mode, when the agent asks the user a question
        (e.g. 'Approve?'), the loop MUST stop with awaiting_user instead
        of injecting 'Continue' and letting the model answer itself."""
        import dev_agent.agent_loop as al

        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            "Утверждаете план? (напишите «ок», «да», «go» - и я начну реализацию)",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))

        result = al.run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"

    def test_loop_auto_stops_on_approve_question_english(self, monkeypatch):
        """Same as above but with English question text."""
        import dev_agent.agent_loop as al

        responses = [
            '```json\n{"tool": "read_file", "args": {"path": "x.py"}}\n```',
            "Do you approve this change? (write 'ok' or 'yes' to continue)",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))

        result = al.run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"


# ── loop_status-based continuation ───────────────────────────────────────────

class TestLoopStatus:
    def test_parse_loop_status_continue(self):
        text = 'Some prose.\n```json\n{"loop_status": "continue"}\n```'
        assert _parse_loop_status(text) == "continue"

    def test_parse_loop_status_awaiting_user(self):
        text = 'Completed.\n```json\n{"loop_status": "awaiting_user"}\n```'
        assert _parse_loop_status(text) == "awaiting_user"

    def test_parse_loop_status_missing(self):
        text = 'No status field here.'
        assert _parse_loop_status(text) is None

    def test_parse_loop_status_case_insensitive(self):
        text = '{"loop_status": "CONTINUE"}'
        assert _parse_loop_status(text) == "continue"

    def test_loop_status_continue_trumps_heuristics(self, monkeypatch):
        """When loop_status: continue is present, old heuristics are bypassed."""
        import dev_agent.agent_loop as al
        responses = [
            'Shall I continue?\n```json\n{"loop_status": "continue"}\n```',
            'All plan steps completed.\n```json\n{"loop_status": "awaiting_user"}\n```',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"

    def test_loop_status_awaiting_user_stops_immediately(self, monkeypatch):
        """loop_status: awaiting_user stops the loop, even with progress phrases."""
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            'Step 2 completed. All plan steps completed.\n```json\n{"loop_status": "awaiting_user"}\n```',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"
        assert result.steps >= 2

    def test_loop_status_fallback_to_legacy_marker(self, monkeypatch):
        """When loop_status is absent, legacy _requires_user_response still works."""
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            '_requires_user_response: false\nProceeding...',
            'All plan steps completed.',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "done"

    def test_loop_status_takes_precedence_over_legacy(self, monkeypatch):
        """When both loop_status and _requires_user_response are present,
        loop_status wins."""
        import dev_agent.agent_loop as al
        responses = [
            '```json\n{"tool": "list_files", "args": {}}\n```',
            '_requires_user_response: false\n{"loop_status": "awaiting_user"}\nDone.',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"


# ── cache-friendly economy mode ──────────────────────────────────────────────

class TestEconomyCacheMode:
    def _state(self, history_len: int, *, tail=5, multiplier=2,
               cache_enabled=True, workspace="ws", web_search=False):
        state = AgentLoopState(economy_mode=True)
        state.economy_cache_enabled = cache_enabled
        state.economy_cache_multiplier = multiplier
        state.workspace_info = workspace
        state.web_search_enabled = web_search
        state.history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(history_len)
        ]
        return state

    def _shown(self, ctx):
        """Return history messages from a built context (excluding meta)."""
        return [m for m in ctx if m.get("role") in ("user", "assistant")]

    def _patch_tail(self, monkeypatch, tail):
        import dev_agent.agent_loop as al
        monkeypatch.setattr(al, "_get_economy_tail_messages", lambda: tail)

    def test_cache_enabled_full_thread_when_within_window(self, monkeypatch):
        self._patch_tail(monkeypatch, 5)
        # total 8 <= 5*2
        state = self._state(8)
        ctx = build_economy_context(state)
        shown = self._shown(ctx)
        assert len(shown) == 8
        assert shown[0]["content"] == "m0"
        assert state.economy_anchor == 0

    def test_cache_enabled_reaches_end_of_window(self, monkeypatch):
        self._patch_tail(monkeypatch, 5)
        # total exactly 10 == 5*2
        state = self._state(10)
        ctx = build_economy_context(state)
        assert len(self._shown(ctx)) == 10
        assert state.economy_anchor == 0

    def test_cache_enabled_exceeding_window_fixes_anchor(self, monkeypatch):
        self._patch_tail(monkeypatch, 5)
        state = self._state(11)
        ctx = build_economy_context(state)
        shown = self._shown(ctx)
        # anchor = total - tail = 6 → messages 6..10 (5 messages)
        assert len(shown) == 5
        assert shown[0]["content"] == "m6"
        assert state.economy_anchor == 6

    def test_cache_enabled_anchor_stays_stable_across_requests(self, monkeypatch):
        self._patch_tail(monkeypatch, 5)
        state = self._state(11)
        first = build_economy_context(state)
        anchor_after_first = state.economy_anchor
        assert anchor_after_first == 6

        # growth still within window (total - anchor <= 10)
        state.history.append({"role": "assistant", "content": "m11"})
        second = build_economy_context(state)
        assert state.economy_anchor == anchor_after_first
        assert [m["content"] for m in self._shown(second)][0] == "m6"

    def test_cache_enabled_anchor_shifts_after_window_overflow(self, monkeypatch):
        self._patch_tail(monkeypatch, 5)
        state = self._state(11)  # anchor 6
        build_economy_context(state)
        # push to total 20; sent len 14 > 10
        while len(state.history) < 20:
            state.history.append({"role": "assistant", "content": f"m{len(state.history)}"})
        ctx = build_economy_context(state)
        assert state.economy_anchor == 20 - 5
        shown = self._shown(ctx)
        assert len(shown) == 5
        assert shown[0]["content"] == "m15"

    def test_cache_enabled_meta_change_resets_anchor(self, monkeypatch):
        self._patch_tail(monkeypatch, 5)
        state = self._state(12)
        build_economy_context(state)
        assert state.economy_anchor == 7

        # change workspace → anchor should reset to total - tail (7)
        state.workspace_info = "other-ws"
        ctx = build_economy_context(state)
        assert state.economy_anchor == 12 - 5
        assert [m["content"] for m in self._shown(ctx)][0] == "m7"

    def test_legacy_mode_still_returns_tail(self, monkeypatch):
        self._patch_tail(monkeypatch, 5)
        state = self._state(20, cache_enabled=False)
        ctx = build_economy_context(state)
        shown = self._shown(ctx)
        assert len(shown) == 5
        assert shown[0]["content"] == "m15"

    def test_multiplier_one_disables_cache_mode(self, monkeypatch):
        self._patch_tail(monkeypatch, 5)
        state = self._state(20, multiplier=1)
        ctx = build_economy_context(state)
        shown = self._shown(ctx)
        assert len(shown) == 5
        assert shown[0]["content"] == "m15"

    def test_carry_over_economy_cache_preserves_anchor(self, monkeypatch):
        """A new AgentLoopState created for a follow-up message in the same
        thread must continue the cache-window accumulation from the previous
        state instead of restarting from the bare tail."""
        self._patch_tail(monkeypatch, 5)
        state = self._state(11)
        build_economy_context(state)
        assert state.economy_anchor == 6

        # New state for the next user turn; same thread/history.
        new_state = self._state(len(state.history))
        assert new_state.economy_anchor is None

        carry_over_economy_cache(state, new_state)
        assert new_state.economy_anchor == 6

        ctx = build_economy_context(new_state)
        shown = self._shown(ctx)
        assert len(shown) == 5
        assert shown[0]["content"] == "m6"

        # One more agent step: the window grows (6 messages now).
        new_state.history.append({"role": "assistant", "content": "m11"})
        ctx2 = build_economy_context(new_state)
        shown2 = self._shown(ctx2)
        assert len(shown2) == 6
        assert new_state.economy_anchor == 6

    def test_first_workspace_detection_does_not_reset_anchor(self, monkeypatch):
        """The first current_workspace() reply fills workspace_info but must
        NOT reset the accumulation anchor - it is the initial query, not a
        workspace switch. Otherwise every fresh thread restarts from the bare
        tail and the window never grows past tail messages (31..32..90)."""
        self._patch_tail(monkeypatch, 30)
        # Fresh loop: the workspace is not known yet on the first LLM calls.
        state = self._state(31, workspace="")
        build_economy_context(state)
        assert state.economy_anchor == 0

        # The model answered current_workspace(): workspace now known.
        state.workspace_info = "root=/tmp/project"
        _ = build_economy_context(state)
        shown = self._shown(build_economy_context(state))
        # Anchor must stay 0 and keep accumulating, not reset to
        # total - tail = 1 and send only the last 30 messages.
        assert state.economy_anchor == 0
        assert len(shown) == 31
        assert shown[0]["content"] == "m0"

    def test_initial_workspace_info_does_not_reset_anchor(self, monkeypatch):
        """Same protection when the very first build already knows the
        workspace: no reset happens because economy_anchor is still None."""
        self._patch_tail(monkeypatch, 5)
        state = self._state(6, workspace="root=/tmp/project")
        ctx = build_economy_context(state)
        assert state.economy_anchor == 0
        assert len(self._shown(ctx)) == 6

    def test_real_workspace_switch_resets_anchor(self, monkeypatch):
        """A genuine workspace change mid-accumulation still resets the anchor."""
        self._patch_tail(monkeypatch, 5)
        state = self._state(11, workspace="root=/tmp/proj-a")
        build_economy_context(state)
        assert state.economy_anchor == 6

        state.workspace_info = "root=/tmp/proj-b"
        _ = build_economy_context(state)
        assert state.economy_anchor == 11 - 5
        shown = self._shown(build_economy_context(state))
        assert shown[0]["content"] == "m6"

        # Keep accumulating and switch again: the anchor must track the new
        # total, not stay pinned at the old position.
        while len(state.history) < 16:
            state.history.append({"role": "assistant", "content": f"m{len(state.history)}"})
        state.workspace_info = "root=/tmp/proj-c"
        _ = build_economy_context(state)
        assert state.economy_anchor == 16 - 5
        shown = self._shown(build_economy_context(state))
        assert shown[0]["content"] == "m11"

    def test_per_state_tail_overrides_global_default(self, monkeypatch):
        """The per-state tail (set from the active orchestrator config)
        wins over the config-derived default used as fallback."""
        self._patch_tail(monkeypatch, 5)
        state = self._state(20)
        state.economy_tail_messages = 7
        state.economy_cache_multiplier = 2
        state.economy_anchor = None
        ctx = build_economy_context(state)
        # window = 7 * 2 = 14 < 20 → anchor = 20 - 7 = 13
        assert state.economy_anchor == 13
        shown = self._shown(ctx)
        assert len(shown) == 7
        assert shown[0]["content"] == "m13"

    def test_carry_over_preserves_tail(self, monkeypatch):
        """A follow-up turn keeps the per-state tail length."""
        self._patch_tail(monkeypatch, 5)
        state = self._state(10)
        state.economy_tail_messages = 8
        new_state = self._state(10)
        carry_over_economy_cache(state, new_state)
        assert new_state.economy_tail_messages == 8

    def test_economy_cache_dict_roundtrip_through_cleared_state(self, monkeypatch):
        """economy_cache_to_dict/apply_economy_cache must preserve the
        accumulation anchor across a full loop-state loss (terminal
        status): serialize from the old state, build a fresh state, apply."""
        self._patch_tail(monkeypatch, 5)
        old_state = self._state(12)
        build_economy_context(old_state)
        assert old_state.economy_anchor == 7

        data = economy_cache_to_dict(old_state)
        new_state = self._state(12)
        assert new_state.economy_anchor is None

        apply_economy_cache(data, new_state)
        assert new_state.economy_anchor == 7
        assert new_state.economy_meta_key == old_state.economy_meta_key
        assert new_state.workspace_info == old_state.workspace_info

    def test_apply_economy_cache_ignores_empty_data(self):
        """None/empty payloads are no-ops so a fresh dialog starts clean."""
        state = self._state(5)
        apply_economy_cache(None, state)
        apply_economy_cache({}, state)
        assert state.economy_anchor is None
        assert state.economy_meta_key == ""

    def test_full_accumulation_cycle_ui_style(self):
        """The full cache-friendly cycle as the UI drives it across turns:
        grow from last 30 → 90, reset to 30 when the tail*multiplier
        window overflows, grow again → 90, reset again.

        Between turns the loop state is lost (terminal status cleared it),
        so each new state restores the anchor from the serialized cache.
        Window = 30 * 3 = 90.
        """
        tail = 30
        total = 40
        ws = "root=/tmp/proj"
        sent_lengths = []
        cache: dict = {}

        def make_state(n):
            st_ = AgentLoopState(economy_mode=True)
            st_.economy_tail_messages = tail
            st_.economy_cache_enabled = True
            st_.economy_cache_multiplier = 3
            st_.workspace_info = ws
            st_.history = [
                {"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"m{i}"}
                for i in range(n)
            ]
            return st_

        def turn(n):
            """Simulate _do_step across turns: fresh state (previous loop
            state cleared), restore the persisted cache, rebuild."""
            nonlocal cache
            st_ = make_state(n)
            apply_economy_cache(cache or None, st_)
            build_economy_context(st_)
            cache = economy_cache_to_dict(st_)
            return len(st_.history) - st_.economy_anchor

        # Initial accumulation: 40, 42, ...
        n = total
        sent_lengths.append(turn(n))
        n += 2
        sent_lengths.append(turn(n))
        assert sent_lengths[-2:] == [40, 42]

        # Cross the window (90): reset to the bare tail of 30.
        n = 92
        sent_lengths.append(turn(n))
        assert sent_lengths[-1] == 30

        # Growth phase: the window must now GROW: 32, 34, ... up to 90.
        n += 2
        sent_lengths.append(turn(n))
        n += 2
        sent_lengths.append(turn(n))
        assert sent_lengths[-2:] == [32, 34]

        # Reach exactly 90.
        n = 62 + 90
        sent_lengths.append(turn(n))
        assert sent_lengths[-1] == 90

        # Overflow 90 → second reset to 30, then growth resumes.
        n += 2
        sent_lengths.append(turn(n))
        assert sent_lengths[-1] == 30
        n += 2
        sent_lengths.append(turn(n))
        assert sent_lengths[-1] == 32

# ── plan / confirmation-request stopping (without loop_status) ─────────────

class TestPlanConfirmationStops:
    def test_plan_after_tool_result_stops_even_without_loop_status(self, monkeypatch):
        '''A plan presented after read tools stops the loop with awaiting_user
        even when loop_status is absent.'''
        import dev_agent.agent_loop as al
        responses = [
            '''```json
{"tool": "read_file", "args": {"path": "x.py"}}
```''',
            '''План:
1. Прочитать файл
2. Внести правку
3. Проверить тесты
Жду подтверждения.''',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("edit", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"
        assert result.steps == 2

    def test_plan_without_question_mark_stops(self, monkeypatch):
        '''A numbered plan with no '?' and no loop_status stops the loop.'''
        import dev_agent.agent_loop as al
        responses = [
            '''План правки:
1. Изменить файл
2. Обновить тесты
3. Запустить проверку
Напишите «ок» для продолжения''',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("edit", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"
        assert result.steps == 1

    def test_confirmation_request_without_question_mark_stops(self, monkeypatch):
        '''"Жду подтверждения." and "Please approve" (no '?') stop the loop.'''
        import dev_agent.agent_loop as al
        responses = [
            '''```json
{"tool": "list_files", "args": {}}
```''',
            "Жду подтверждения.",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"
        assert result.steps == 2

    def test_bullet_plan_stops(self, monkeypatch):
        '''A bullet-list plan with the word "план" stops the loop.'''
        import dev_agent.agent_loop as al
        responses = [
            '''Мой план:
- Шаг 1: изучить код
- Шаг 2: внести правку
- Шаг 3: прогнать тесты''',
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("edit", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "awaiting_user"

    def test_intermediate_report_without_plan_still_continues(self, monkeypatch):
        '''A plain progress report without plan/confirmation markers continues.'''
        import dev_agent.agent_loop as al
        responses = [
            '''```json
{"tool": "list_files", "args": {}}
```''',
            "Продолжаю выполнение.",
            "All plan steps completed.",
        ]
        monkeypatch.setattr(al, "send_request", _scripted_send(responses))
        result = run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status == "done"

    def test_confirmation_request_word_forms_positive(self):
        """Word forms of confirmation requests are recognized."""
        import dev_agent.agent_loop as al
        for text in (
            "Жду подтверждения.",
            "Подтвердите план",
            "Ожидаю одобрения.",
            "Требуется подтверждение операции.",
            "Please approve the plan.",
            "I am waiting for your approval.",
        ):
            assert al._looks_like_confirmation_request(text) is True, text

    def test_confirmation_request_negative(self):
        """Ordinary progress messages are not confirmation requests."""
        import dev_agent.agent_loop as al
        for text in (
            "Все тесты прошли успешно.",
            "Продолжаю выполнение.",
            "Шаг 2 завершён.",
        ):
            assert al._looks_like_confirmation_request(text) is False, text




# ── broken-call handling: diagnostics, signatures, duplicate guard ────────────

class TestUnparsedDiagnostics:
    """Coverage for the new broken-tool-call handling helpers."""

    def test_diagnostics_describes_parse_failure(self):
        """A block with a trailing comma in args is diagnosed precisely."""
        import dev_agent.agent_loop as al
        text = (
            '```json\n{"tool": "propose_file", "args": {"path": "a.py",},}\n```'
        )
        diags = al._unparsed_tool_json_diagnostics(text)
        assert diags, 'expected at least one diagnostic'
        assert '"tool"' in diags[0]["snippet"]
        assert 'cause' in diags[0]

    def test_signature_stable_and_distinct(self):
        """Signature must be stable across identical blocks and vary across
        modified blocks. Empty diagnostics -> empty signature."""
        import dev_agent.agent_loop as al
        block_a = '```json\n{"tool": "x", "args": {"k": "v",},}\n```'
        block_b = '```json\n{"tool": "x", "args": {"k": "w",},}\n```'
        da1 = al._unparsed_tool_json_diagnostics(block_a)
        da2 = al._unparsed_tool_json_diagnostics(block_a)
        db = al._unparsed_tool_json_diagnostics(block_b)
        assert al._unparsed_block_signature(da1) == al._unparsed_block_signature(da2)
        assert al._unparsed_block_signature(da1) != al._unparsed_block_signature(db)
        assert al._unparsed_block_signature([]) == ""

    def test_parse_failure_feeds_diagnostics_to_model(self, monkeypatch):
        """After a genuinely malformed call the loop feeds diagnostics back
        instead of silently spinning."""
        import dev_agent.agent_loop as al
        malformed = '```json\n{"tool": "list_files", "args": {"subdir": "x",},}\n```'
        monkeypatch.setattr(al, "send_request", _scripted_send([malformed, "ok"]))
        result = run_agent_loop("task", _make_skill(), FakeDispatcher(), auto_apply=True)
        assert result.status in ("done", "stopped_max_steps")

    def test_duplicate_failed_call_blocked(self, monkeypatch):
        """The same failing dispatch_json call is only sent once; its literal
        repeat is blocked with duplicate_call_blocked=True and guidance."""
        import dev_agent.agent_loop as al
        dispatcher = FakeDispatcher({"write_doc": {"ok": False, "error": "boom"}})
        call = '''```json\n{"tool": "write_doc", "args": {"doc": "readme", "content": "x"}}\n```'''
        monkeypatch.setattr(al, "send_request", _scripted_send([call, call, "done"]))
        result = run_agent_loop("task", _make_skill(), dispatcher, auto_apply=True)
        joined = "\n".join(
            str(e.get("content", "")) for e in result.history
            if e.get("role") == "user"
        )
        assert "duplicate_call_blocked" in joined
        assert "write_doc" in joined

    def test_failure_adds_guidance(self, monkeypatch):
        """A failed dispatch_json result is annotated with next_action so the
        model switches down the fallback chain instead of retrying blindly."""
        import dev_agent.agent_loop as al
        dispatcher = FakeDispatcher({"read_file": {"ok": False, "error": "missing"}})
        call = '```json\n{"tool": "read_file", "args": {"path": "nope.md"}}\n```'
        monkeypatch.setattr(al, "send_request", _scripted_send([call, "done"]))
        result = run_agent_loop("task", _make_skill(), dispatcher, auto_apply=True)
        joined = "\n".join(
            str(e.get("content", "")) for e in result.history
            if e.get("role") == "user"
        )
        assert "next_action" in joined
        assert "missing" in joined

    def test_truncated_fenced_json_detected(self):
        """A fenced tool-call JSON whose closing fence was cut off is
        detected as a truncated segment (odd fence count after the last
        paired block) and diagnosed with an actionable cause."""
        import dev_agent.agent_loop as al
        f = chr(96) * 3
        nl = chr(10)
        truncated = f + "json" + nl + '{"tool": "read_file", "args": {"path": "main.py"'
        segs = al._truncated_tool_json_segments(truncated)
        assert segs, "truncated tool-call JSON must be detected"
        assert '"tool"' in segs[0]
        assert al._unparsed_tool_json_blocks(truncated) >= 1
        diags = al._unparsed_tool_json_diagnostics(truncated)
        joined = " | ".join(d.get("cause", "") for d in diags)
        assert "TRUNCATED" in joined

    def test_truncated_detection_negatives(self):
        """Plain text, validly paired fences and text without the tool marker
        never produce truncated segments."""
        import dev_agent.agent_loop as al
        f = chr(96) * 3
        nl = chr(10)
        assert al._truncated_tool_json_segments("just prose") == []
        valid = f + "json" + nl + '{"tool": "list_files", "args": {}}' + nl + f
        assert al._truncated_tool_json_segments(valid) == []
        assert al._truncated_tool_json_segments(f + "json" + nl + '{"k": 1} prose') == []

    def test_unbalanced_json_details_reports_unclosed_constructs(self):
        '''The unbalanced analyser must report open braces/brackets/strings.'''
        import dev_agent.agent_loop as al
        q = chr(34)
        raw = ('{' + q + 'tool' + q + ': ' + q + 'propose_file' + q + ', '
               + q + 'args' + q + ': {' + q + 'path' + q + ': ' + q + 'a.py' + q
               + ', ' + q + 'content' + q + ': ' + q + 'x=[1,2')
        details = al._unbalanced_json_details(raw)
        assert details['brace_depth'] == 2
        assert details['bracket_depth'] == 0
        assert details['unterminated_string'] is True
        summary = al._unclosed_summary(details)
        assert 'unterminated string' in summary
        assert 'brace depth 2' in summary

    def test_truncated_diagnostic_names_unclosed_constructs(self):
        '''A truncated call must name the unclosed braces/quotes in the cause.'''
        import dev_agent.agent_loop as al
        f = chr(96) * 3
        nl = chr(10)
        q = chr(34)
        truncated = (f + 'json' + nl + '{' + q + 'tool' + q + ': ' + q
                     + 'read_file' + q + ', ' + q + 'args' + q + ': {'
                     + q + 'path' + q + ': ' + q + 'main.py' + q)
        diags = al._unparsed_tool_json_diagnostics(truncated)
        joined = ' | '.join(d.get('cause', '') for d in diags)
        assert 'TRUNCATED' in joined
        assert 'unclosed brace depth' in joined

    def test_multiple_tool_call_blocks_get_explicit_diagnostic(self):
        """Two fenced tool-call blocks in ONE message produce the explicit
        'one tool call per message' cause, so the model is told the real
        problem instead of a generic parse error."""
        import dev_agent.agent_loop as al
        multi = (
            '```json\n{"tool": "list_files", "args": {"subdir": "."}}\n```\n'
            '```json\n{"tool": "read_file", "args": {"path": "main.py"}}\n```'
        )
        diags = al._unparsed_tool_json_diagnostics(multi)
        assert diags, "expected at least one diagnostic for a multi-call message"
        joined = " | ".join(d.get("cause", "") for d in diags)
        assert "exactly ONE tool call per message" in joined


def test_live_loop_history_entries_get_ts(monkeypatch):
    """Every non-hidden user/assistant entry created by the live loop carries
    an ISO ``ts`` for the datetime captions in the chat UI."""
    import dev_agent.agent_loop as al
    monkeypatch.setattr(al, "send_request", _scripted_send([
        '```json\n{"tool": "list_files", "args": {}}\n```',
        "All plan steps completed.",
    ]))
    result = run_agent_loop("check ts", _make_skill(), FakeDispatcher())
    visible = [
        m for m in result.history
        if not m.get("hidden") and m.get("role") in ("user", "assistant")
    ]
    assert visible, "expected at least one visible history entry"
    for m in visible:
        assert isinstance(m.get("ts"), str) and m["ts"], f"missing ts: {m}"


# ── DSML call validation ────────────────────────────────────────────────────

class TestDsmlValidation:
    def test_dsml_read_file_requires_path(self):
        from dev_agent.agent_loop import _dsml_validation_error
        err = _dsml_validation_error("read_file", {})
        assert err is not None and "JSON" in err
        assert _dsml_validation_error("read_file", {"path": "x.py"}) is None

    def test_dsml_search_in_files_requires_query(self):
        from dev_agent.agent_loop import _dsml_validation_error
        err = _dsml_validation_error("search_in_files", {"subdir": "."})
        assert err is not None and "query" in err
        assert _dsml_validation_error("search_in_files", {"query": "abc"}) is None

    def test_dsml_run_code_requires_mode_param(self):
        from dev_agent.agent_loop import _dsml_validation_error
        err = _dsml_validation_error("run_code", {"confirmed_by_user": False})
        assert err is not None and "code" in err
        assert _dsml_validation_error("run_code", {"code": "print(1)"}) is None
        assert _dsml_validation_error("run_code", {"path": "x.py"}) is None

    def test_dsml_rag_search_requires_slug(self):
        from dev_agent.agent_loop import _dsml_validation_error
        err = _dsml_validation_error("rag_search", {"query": "abc"})
        assert err is not None and "slug" in err
        assert _dsml_validation_error("rag_search", {"slug": "b", "query": "abc"}) is None

    def test_dsml_optional_args_tool_valid(self):
        from dev_agent.agent_loop import _dsml_validation_error
        assert _dsml_validation_error("list_files", {}) is None


class TestDsmlStepLoop:
    def test_invalid_dsml_not_dispatched_guides_json(self):
        seen = []

        class RecDispatcher(FakeDispatcher):
            def dispatch_json(self, call):
                seen.append(call)
                return {"ok": True}

            def dispatch(self, tool, args):
                seen.append((tool, args))
                return {"ok": True}

        disp = RecDispatcher()
        state = AgentLoopState()
        state.phase = "executing"
        state.parsed_calls = [{"tool": "read_file", "args": {}, "_dsml": True}]
        step_agent_loop(state, dispatcher=disp)
        assert seen == []
        assert "JSON" in state.user_message
        assert "```json" in state.user_message

    def test_valid_dsml_is_dispatched(self):
        seen = []

        class RecDispatcher(FakeDispatcher):
            def dispatch_json(self, call):
                seen.append(call)
                return {"ok": True}

            def dispatch(self, tool, args):
                seen.append((tool, args))
                return {"ok": True}

        disp = RecDispatcher()
        state = AgentLoopState()
        state.phase = "executing"
        state.parsed_calls = [{"tool": "read_file", "args": {"path": "x.py"}, "_dsml": True}]
        step_agent_loop(state, dispatcher=disp)
        assert seen, "валидный DSML-вызов должен диспатчиться"
        first = seen[0]
        if isinstance(first, dict):
            assert first["tool"] == "read_file" and first["args"]["path"] == "x.py"
        else:
            assert first[0] == "read_file" and first[1]["path"] == "x.py"
