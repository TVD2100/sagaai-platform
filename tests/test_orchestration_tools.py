# -*- coding: utf-8 -*-
"""tests/test_orchestration_tools.py - the delegation tools and their rights.

``spawn_agent`` and ``invoke_assistant`` own the refusal wording, so the
refusals are asserted here by their machine codes and by the details the
model is supposed to read: ``allowed``, ``self_only``, ``hint``,
``available``, ``missing``, ``conflicting``. The allowlists are stubbed at
the ``core.orchestrators`` boundary, which keeps these tests about the
delegation layer itself rather than about SQLite.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    ERR_AGENT_NOT_ALLOWED,
    ERR_ASSISTANT_NOT_ALLOWED,
    ERR_INVALID_ARGS,
    ERR_INVALID_BRIEF,
    ERR_NOT_IMPLEMENTED,
    ERR_UNKNOWN_AGENT,
    ERR_UNKNOWN_ASSISTANT,
    InvokeRequest,
    SpawnRequest,
)


@pytest.fixture
def allowlists(monkeypatch):
    """Control both allowlists and the orchestrator/assistant cards."""
    state = {"agents": [], "assistants": [], "orchestrators": {}, "cards": {}}

    import core.assistants as assistants_mod
    import core.orchestrators as orch_mod

    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": list(state["agents"]))
    monkeypatch.setattr(orch_mod, "get_allowed_assistants",
                        lambda slug="dev_agent": list(state["assistants"]))
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: state["orchestrators"].get(slug))
    monkeypatch.setattr(assistants_mod, "get_assistant_by_id",
                        lambda aid: state["cards"].get(aid))
    return state


class FakeService:
    """Capture the request that reaches the runtime, without running it."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"ok": True, "result": {}}

    def run(self, req, **kwargs):
        self.calls.append(("run", req, kwargs))
        return self.result

    def start(self, req, **kwargs):
        """Background launch: the runtime validates now, runs later."""
        self.calls.append(("start", req, kwargs))
        return self.result

    def invoke(self, req, **kwargs):
        self.calls.append(("invoke", req, kwargs))
        return self.result


# ─── allowlist view and self-delegation ──────────────────────────────────────


def test_self_delegation_needs_no_configuration(allowlists):
    """An empty allowlist still allows the agent itself; nothing to configure."""
    assert tools.check_agent_allowed("dev_agent", "dev_agent") is None
    assert tools.check_agent_allowed("dev_agent", "  dev_agent  ") is None

    ctx = tools._allowlist_context("dev_agent")
    assert ctx == {"allowed": ["dev_agent"], "self_only": True}


def test_other_agent_refused_with_self_only_hint(allowlists):
    """Refusing a foreign slug explains WHY the list is short."""
    refusal = tools.check_agent_allowed("dev_agent", "other_agent")
    assert refusal["ok"] is False
    assert refusal["error_code"] == ERR_AGENT_NOT_ALLOWED
    assert refusal["allowed"] == ["dev_agent"]
    assert refusal["self_only"] is True
    assert "allowed_agents" in refusal["hint"]
    assert refusal["agent"] == "other_agent"


def test_configured_agent_passes_and_drops_the_self_only_flag(allowlists):
    allowlists["agents"] = ["other_agent"]

    assert tools.check_agent_allowed("dev_agent", "other_agent") is None
    ctx = tools._allowlist_context("dev_agent")
    assert ctx["allowed"] == ["dev_agent", "other_agent"]
    assert ctx["self_only"] is False

    refusal = tools.check_agent_allowed("dev_agent", "third_agent")
    assert refusal["error_code"] == ERR_AGENT_NOT_ALLOWED
    assert "self_only" not in refusal


def test_empty_agent_slug_is_invalid_args(allowlists):
    refusal = tools.check_agent_allowed("dev_agent", "   ")
    assert refusal["error_code"] == ERR_INVALID_ARGS
    assert refusal["missing"] == ["agent_slug"]


def test_allowlist_deduplicates_and_ignores_garbage(allowlists):
    allowlists["agents"] = ["other", "other", "  ", "dev_agent", None]
    ctx = tools._allowlist_context("dev_agent")
    assert ctx["allowed"] == ["dev_agent", "other"]


# ─── spawn_agent ────────────────────────────────────────────────────────────


def test_spawn_unknown_agent_lists_available_slugs(allowlists):
    """A caller mistake reports what the caller MAY address."""
    allowlists["agents"] = ["other_agent"]

    result = tools.spawn_agent(agent_slug="other_agent",
                               request=AgentBrief(goal="do it"),
                               parent_slug="dev_agent")

    assert result["ok"] is False
    assert result["error_code"] == ERR_UNKNOWN_AGENT
    assert result["agent"] == "other_agent"
    assert "dev_agent" in result["available"]
    assert "other_agent" in result["available"]


def test_spawn_requires_a_brief(allowlists):
    result = tools.spawn_agent(agent_slug="dev_agent", request=None,
                               parent_slug="dev_agent")
    assert result["error_code"] == ERR_INVALID_BRIEF
    assert result["expected"] == ["goal", "brief", "artifacts_in"]


def test_spawn_rejects_an_empty_goal(allowlists):
    result = tools.spawn_agent(agent_slug="dev_agent",
                               request=AgentBrief(goal="   "),
                               parent_slug="dev_agent")
    assert result["error_code"] == ERR_INVALID_BRIEF
    assert result["missing"] == ["goal"]


def test_inline_orchestrator_is_not_implemented(allowlists):
    result = tools.spawn_agent(agent_slug="dev_agent",
                               request=AgentBrief(goal="do it"),
                               inline_orchestrator={"prompt": "x"},
                               parent_slug="dev_agent")
    assert result["error_code"] == ERR_NOT_IMPLEMENTED
    assert result["planned"] == "phase 2"


def test_spawn_forwards_a_spawn_request_to_the_service(allowlists):
    """Rights and the brief are settled before the runtime is touched."""
    allowlists["orchestrators"]["dev_agent"] = {"slug": "dev_agent", "config": {}}
    service = FakeService({"ok": True, "result": {"run_id": "r1"}})

    result = tools.spawn_agent(agent_slug="dev_agent",
                               request=AgentBrief(goal="do it", brief="details"),
                               parent_slug="dev_agent", parent_run_id="root",
                               thread_id="t1", service=service)

    assert result == {"ok": True, "result": {"run_id": "r1"}}
    kind, req, kwargs = service.calls[0]
    assert kind == "run"
    assert isinstance(req, SpawnRequest)
    assert req.agent_slug == "dev_agent"
    assert req.request.brief == "details"
    assert req.parent_run_id == "root"
    assert req.thread_id == "t1"
    assert kwargs["parent_config"] is None


# ─── invoke_assistant argument validation ───────────────────────────────────


def test_assistant_id_and_system_are_mutually_exclusive(allowlists):
    result = tools.invoke_assistant(assistant_id="abcd1234", system="prompt",
                                    user="hi", parent_slug="dev_agent")
    assert result["error_code"] == ERR_INVALID_ARGS
    assert result["conflicting"] == ["assistant_id", "system"]


def test_invoke_needs_an_assistant_or_a_system_prompt(allowlists):
    result = tools.invoke_assistant(user="hi", parent_slug="dev_agent")
    assert result["error_code"] == ERR_INVALID_ARGS
    assert result["missing"] == ["assistant_id|system"]


def test_invoke_needs_input_to_work_on(allowlists):
    result = tools.invoke_assistant(system="prompt", user="   ",
                                    parent_slug="dev_agent")
    assert result["error_code"] == ERR_INVALID_ARGS
    assert result["missing"] == ["user|context_files"]


def test_assistant_outside_the_allowlist_is_refused(allowlists):
    """No assistant is callable by default - the list decides."""
    result = tools.invoke_assistant(assistant_id="abcd1234", user="hi",
                                    parent_slug="dev_agent")
    assert result["error_code"] == ERR_ASSISTANT_NOT_ALLOWED
    assert result["assistant"] == "abcd1234"
    assert result["allowed"] == []


def test_unknown_assistant_id_is_reported_after_the_rights_check(allowlists):
    allowlists["assistants"] = ["abcd1234"]

    result = tools.invoke_assistant(assistant_id="abcd1234", user="hi",
                                    parent_slug="dev_agent")
    assert result["error_code"] == ERR_UNKNOWN_ASSISTANT
    assert result["assistant"] == "abcd1234"


def test_ephemeral_call_reaches_the_runtime_without_assistant_rights(allowlists):
    """The ephemeral form addresses no assistant, so no allowlist applies."""
    service = FakeService({"ok": True, "result": {"text": "answer"}})

    result = tools.invoke_assistant(system="be terse", user="2+2?",
                                    parent_slug="dev_agent", service=service)

    assert result == {"ok": True, "result": {"text": "answer"}}
    kind, req, kwargs = service.calls[0]
    assert kind == "invoke"
    assert isinstance(req, InvokeRequest)
    assert req.assistant_id is None
    assert req.system == "be terse"
    assert req.user == "2+2?"
    assert kwargs["parent_slug"] == "dev_agent"


def test_allowed_assistant_reaches_the_runtime(allowlists):
    allowlists["assistants"] = ["abcd1234"]
    allowlists["cards"]["abcd1234"] = {"id": "abcd1234", "name": "Helper"}
    service = FakeService({"ok": True, "result": {"text": "answer"}})

    result = tools.invoke_assistant(assistant_id="abcd1234", user="hi",
                                    context_files=["a.txt"],
                                    parent_slug="dev_agent", service=service)

    assert result["ok"] is True
    req = service.calls[0][1]
    assert req.assistant_id == "abcd1234"
    assert req.context_files == ["a.txt"]


# ─── catalog entries ────────────────────────────────────────────────────────


def test_spawn_catalog_entry_states_the_boundaries():
    entry = tools.spawn_tool_entry()
    assert entry["name"] == "spawn_agent"
    description = entry["desc"]
    assert "own slug is always allowed" in description
    assert "Blocking" in description
    assert "CLEAN history" in description
    assert "max_active_subagents" in description


def test_invoke_catalog_entry_states_the_one_shot_boundary():
    entry = tools.invoke_tool_entry()
    assert entry["name"] == "invoke_assistant"
    description = entry["desc"]
    assert "no agent loop" in description
    assert "NOT applied" in description
    assert "never truncated" in description


# ─── status explanations ────────────────────────────────────────────────────
#
# A status word alone is not actionable: ``failed`` may mean "retry once" or
# "never retry", and an expired wait looks exactly like a hang. The tool
# results carry the explanation, so no orchestrator needs a prompt edit.


def test_an_internal_failure_tells_the_caller_not_to_retry():
    note = tools.run_note({"status": "failed", "error_code": "internal_error",
                           "retryable": False, "steps_used": 0})

    assert "platform defect" in note
    assert "do not spawn" in note.lower()


def test_a_retryable_failure_asks_for_a_changed_attempt():
    note = tools.run_note({"status": "failed", "error_code": "provider_error",
                           "retryable": True})

    assert "retryable" in note
    assert "instead of repeating" in note


def test_a_max_steps_failure_points_at_the_budget():
    note = tools.run_note({"status": "failed", "error_code": "max_steps"})

    assert "step budget" in note
    assert "resuming does not add steps" in note


def test_an_expired_wait_is_explained_as_still_running():
    note = tools.run_note({"status": "running"}, timed_out=True)

    assert "STILL RUNNING" in note
    assert "not hung" in note


def test_a_running_child_is_not_a_hang():
    note = tools.run_note({"status": "running"})

    assert "still working" in note


def test_an_awaiting_input_child_points_at_the_journal():
    note = tools.run_note({"status": "awaiting_input"})

    assert "read_subagent_state" in note
    assert "resume_subagent" in note


def test_a_done_run_needs_no_second_thought():
    note = tools.run_note({"status": "done"})

    assert "finished" in note


def test_an_unknown_status_produces_no_noise():
    assert tools.run_note({"status": ""}) == ""


def test_a_status_payload_keeps_the_note_next_to_the_data(allowlists):
    """The explanation travels INSIDE the result, not in a separate call."""
    class _Svc:
        def poll(self, rid):
            return {"ok": True, "run": {"run_id": rid, "status": "failed",
                                         "error_code": "internal_error",
                                         "retryable": False}}

    out = tools.subagent_status(run_id="r1", service=_Svc())

    assert out["status"] == "failed"
    assert "platform defect" in out["note"]


def test_an_empty_journal_does_not_read_as_an_empty_run():
    note = tools._journal_note({}, "done")

    assert "does not prove" in note
    assert "subagent_status" in note


def test_a_journal_of_a_running_child_is_a_snapshot():
    note = tools._journal_note({"task": "do it"}, "running")

    assert "snapshot" in note


def test_a_journal_with_a_runtime_outcome_needs_no_note():
    note = tools._journal_note(
        {"task": "do it", "handoff": "Runtime outcome (run r1)\n- status: done"},
        "done")

    assert note == ""


def test_a_journal_without_the_runtime_paragraph_says_so():
    note = tools._journal_note({"task": "do it", "handoff": "my own notes"},
                               "done")

    assert "no runtime outcome paragraph" in note


def test_a_failed_spawn_carries_the_explanation_of_its_result(allowlists):
    """spawn_agent explains a failed child in the same result."""
    allowlists["orchestrators"]["dev_agent"] = {"slug": "dev_agent"}
    service = FakeService({"ok": True, "result": {
        "run_id": "r2", "status": "failed", "error_code": "internal_error",
        "retryable": False, "steps_used": 3}})

    out = tools.spawn_agent(agent_slug="dev_agent",
                            request=AgentBrief(goal="do it"),
                            parent_slug="dev_agent", service=service)

    assert out["result"]["status"] == "failed"
    assert "platform defect" in out["note"]


def test_a_background_spawn_tells_the_caller_not_to_spawn_again(allowlists):
    allowlists["orchestrators"]["dev_agent"] = {"slug": "dev_agent"}
    service = FakeService({"ok": True, "run_id": "r3", "status": "running",
                           "background": True})

    out = tools.spawn_agent(agent_slug="dev_agent",
                            request=AgentBrief(goal="do it"), background=True,
                            parent_slug="dev_agent", service=service)

    assert out["background"] is True
    assert "Do not spawn it again" in out["note"]


def test_a_tree_with_running_children_cannot_be_reported_as_finished(allowlists):
    class _Svc:
        def list_runs(self, root=None):
            return [{"run_id": "a", "status": "done"},
                    {"run_id": "b", "status": "running"}]

    out = tools.subagent_status(root_run_id="root", service=_Svc())

    assert out["count"] == 2
    assert "not terminal yet" in out["note"]


def test_the_open_dialog_itself_does_not_hold_its_tree_up(allowlists):
    """The root record of an open dialog is running by design.

    Counting it as unfinished reported every healthy dialog as incomplete, so
    the caller could never be told that its children had all finished.
    """
    class _Svc:
        def list_runs(self, root=None):
            return [{"run_id": "root", "status": "running"},
                    {"run_id": "b", "status": "done"}]

    out = tools.subagent_status(root_run_id="root", service=_Svc())

    assert "note" not in out or "not terminal yet" not in out["note"]


def test_a_dead_child_of_the_tree_is_reported_as_failed(allowlists):
    """A demoted child is terminal, so the tree reports as finished."""
    class _Svc:
        def list_runs(self, root=None):
            return [{"run_id": "root", "status": "running"},
                    {"run_id": "z", "status": "failed",
                     "error_code": "stale_run"}]

    out = tools.subagent_status(root_run_id="root", service=_Svc())

    assert "note" not in out or "not terminal yet" not in out["note"]


def test_a_refused_resume_explains_why_a_retry_will_not_help(allowlists):
    """A resume that cannot start says what to do INSTEAD of resuming."""
    class _Svc:
        def get_record(self, rid):
            return None

        def record_from_store(self, rid):
            return {"run_id": rid, "status": "failed", "journal_path": ""}

    import core.orchestration.resume as R

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(R, "build_resume",
                   lambda record, read_state=None: {
                       "ok": False, "error": "no journal",
                       "error_code": "resume_unavailable"})
        out = tools.resume_subagent(run_id="r9", service=_Svc())

    assert out["ok"] is False
    assert "no journal to continue from" in out["note"]
    assert "fresh " in out["note"]
