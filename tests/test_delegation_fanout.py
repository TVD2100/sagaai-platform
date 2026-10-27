# -*- coding: utf-8 -*-
"""tests/test_delegation_fanout.py - the loop-side execution of delegation.

``execute_delegation_calls`` is where a batch of model calls becomes real
work, so the properties pinned here are the ones a refactor would silently
lose: the WHOLE batch is validated before any slot is taken, one refusal
does not cancel its neighbours, and results keep the order of the calls.

The tools themselves are replaced at the ``core.orchestration.tools``
boundary - this file is about the fan-out, not about provider calls.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import agent_loop as L  # noqa: E402


class FakeDispatcher:
    """Minimal dispatcher surface used by the fan-out."""

    def __init__(self, slug="dev_agent", child_disabled=None):
        self._slug = slug
        self.child_disabled_tools = set(child_disabled or ())

    def _current_orchestrator_slug(self):
        return self._slug


@pytest.fixture
def delegation_env(monkeypatch):
    """Replace the tool layer and the allowlists with recording fakes.

    ``spawn`` and ``invoke`` record their arguments and return a result that
    names the call, so a test can pair a result with the call that produced
    it without knowing which thread ran it.
    """
    import core.orchestration.tools as tools
    import core.orchestrators as orch_mod

    state = {
        "agents": ["dev_agent"],
        "assistants": ["abcd1234"],
        "calls": [],
        "delay": 0.0,
        "active": 0,
        "max_active": 0,
        "lock": threading.Lock(),
    }

    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": list(state["agents"]))
    monkeypatch.setattr(orch_mod, "get_allowed_assistants",
                        lambda slug="dev_agent": list(state["assistants"]))
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": {}})
    monkeypatch.setattr(orch_mod, "get_disabled_tools", lambda slug="dev_agent": [])

    def _track(label):
        with state["lock"]:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        try:
            if state["delay"]:
                time.sleep(state["delay"])
            return {"ok": True, "result": {"echo": label}}
        finally:
            with state["lock"]:
                state["active"] -= 1

    def _spawn(agent_slug=None, request=None, inline_orchestrator=None, **kwargs):
        state["calls"].append(("spawn_agent", agent_slug, request, kwargs))
        return _track("spawn:" + str(agent_slug))

    def _invoke(assistant_id=None, user="", **kwargs):
        state["calls"].append(("invoke_assistant", assistant_id, user, kwargs))
        return _track("invoke:" + str(assistant_id or "ephemeral"))

    monkeypatch.setattr(tools, "spawn_agent", _spawn)
    monkeypatch.setattr(tools, "invoke_assistant", _invoke)
    return state


def _state():
    return L.AgentLoopState(task="test", thread_id="thread-1", run_id="run-1")


def _spawn_call(slug="dev_agent", goal="do it"):
    return {"tool": "spawn_agent",
            "args": {"agent_slug": slug, "request": {"goal": goal}}}


def _invoke_call(aid="abcd1234"):
    return {"tool": "invoke_assistant", "args": {"assistant_id": aid, "user": "hi"}}


# ─── batch validation ──────────────────────────────────────────────────────


def test_empty_batch_does_no_work(delegation_env):
    assert L.execute_delegation_calls([], state=_state(),
                                      dispatcher=FakeDispatcher()) == []
    assert delegation_env["calls"] == []


def test_valid_calls_run_and_keep_their_order(delegation_env):
    """A fan-out returns results positionally, matching the model's calls."""
    calls = [_invoke_call(), _invoke_call(), _invoke_call()]
    results = L.execute_delegation_calls(calls, state=_state(),
                                         dispatcher=FakeDispatcher(),
                                         service=object())

    assert [r["result"]["echo"] for r in results] == [
        "invoke:abcd1234", "invoke:abcd1234", "invoke:abcd1234"]


def test_unknown_argument_is_refused_before_any_work(delegation_env):
    """Test 14: a bad argument name costs no provider round-trip."""
    calls = [{"tool": "spawn_agent",
              "args": {"agent_slug": "dev_agent", "goal": "do it"}}]
    results = L.execute_delegation_calls(calls, state=_state(),
                                         dispatcher=FakeDispatcher(),
                                         service=object())

    assert results[0]["ok"] is False
    assert results[0]["unknown_args"] == ["goal"]
    assert "suggestion" in results[0]
    assert delegation_env["calls"] == []


def test_agent_outside_the_allowlist_is_refused_before_any_work(delegation_env):
    delegation_env["agents"] = []

    results = L.execute_delegation_calls([_spawn_call(slug="intruder")],
                                         state=_state(),
                                         dispatcher=FakeDispatcher(),
                                         service=object())

    assert results[0]["error_code"] == "agent_not_allowed"
    assert results[0]["self_only"] is True
    assert delegation_env["calls"] == []


def test_assistant_rights_are_enforced_on_the_loop_branch(delegation_env):
    """Test 15: the rights check runs on the tool-call layer, not only in
    the service - a call refused here never reaches the runtime."""
    delegation_env["assistants"] = []

    results = L.execute_delegation_calls([_invoke_call("forbidden1")],
                                         state=_state(),
                                         dispatcher=FakeDispatcher(),
                                         service=object())

    assert results[0]["error_code"] == "assistant_not_allowed"
    assert results[0]["assistant"] == "forbidden1"
    assert delegation_env["calls"] == []


def test_child_disabled_tools_block_a_delegation_call(delegation_env):
    """A child cannot reach a tool its parent banned for it."""
    dispatcher = FakeDispatcher(child_disabled=["invoke_assistant"])

    results = L.execute_delegation_calls([_invoke_call()], state=_state(),
                                         dispatcher=dispatcher,
                                         service=object())

    assert results[0]["ok"] is False
    assert results[0]["disabled"] is True
    assert delegation_env["calls"] == []


# ─── partial refusals ──────────────────────────────────────────────────────


def test_one_refusal_does_not_cancel_its_neighbours(delegation_env):
    """Test 17: 19 valid calls run, the 20th fails in ITS position."""
    calls = [_invoke_call() for _ in range(19)]
    calls.insert(7, {"tool": "spawn_agent",
                     "args": {"agent_slug": "nobody",
                              "request": {"goal": "x"}}})

    results = L.execute_delegation_calls(calls, state=_state(),
                                         dispatcher=FakeDispatcher(),
                                         service=object())

    assert len(results) == 20
    assert results[7]["ok"] is False
    assert results[7]["error_code"] == "agent_not_allowed"
    assert all(r["ok"] is True for i, r in enumerate(results) if i != 7)
    assert len(delegation_env["calls"]) == 19


def test_refusal_emits_a_tool_result_event(delegation_env):
    """A refused branch still appears in the feed, in its own position."""
    events = []
    calls = [{"tool": "spawn_agent", "args": {"agent_slug": ""}}]

    L.execute_delegation_calls(calls, state=_state(), dispatcher=FakeDispatcher(),
                               on_event=events.append, service=object())

    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["result"]["error_code"] == "invalid_args"


# ─── concurrency ───────────────────────────────────────────────────────────


def test_parallel_calls_actually_overlap(delegation_env):
    """A batch of independent calls runs concurrently, not one by one."""
    delegation_env["delay"] = 0.15
    calls = [_invoke_call() for _ in range(4)]

    started = time.monotonic()
    results = L.execute_delegation_calls(calls, state=_state(),
                                         dispatcher=FakeDispatcher(),
                                         service=object())
    elapsed = time.monotonic() - started

    assert [r["ok"] for r in results] == [True] * 4
    assert delegation_env["max_active"] > 1
    assert elapsed < 0.15 * 4


def test_single_call_runs_inline_without_a_thread_pool(delegation_env):
    """One call is not worth a pool; it must still return its result."""
    results = L.execute_delegation_calls([_invoke_call()], state=_state(),
                                         dispatcher=FakeDispatcher(),
                                         service=object())
    assert results[0]["ok"] is True
    assert delegation_env["max_active"] == 1


# ─── argument conversion ───────────────────────────────────────────────────


def test_brief_is_built_from_the_model_arguments(delegation_env):
    """The model sends JSON; the contract wants an AgentBrief."""
    calls = [{"tool": "spawn_agent",
              "args": {"agent_slug": "dev_agent",
                       "request": {"goal": "audit", "brief": "look at a.py",
                                   "artifacts_in": {"input": "a.py"}}}}]
    L.execute_delegation_calls(calls, state=_state(), dispatcher=FakeDispatcher(),
                               service=object())

    _tool, _slug, brief, _kwargs = delegation_env["calls"][0]
    assert brief.goal == "audit"
    assert brief.brief == "look at a.py"
    assert brief.artifacts_in == {"input": "a.py"}


def test_brief_accepts_a_bare_list_of_artifacts(delegation_env):
    """A list of paths is normalised into a name -> path map."""
    brief = L._agent_brief_from_args(
        {"request": {"goal": "audit", "artifacts_in": ["src/a.py", "src/b.py"]}})
    assert brief is not None
    assert brief.artifacts_in == {"a.py": "src/a.py", "b.py": "src/b.py"}
    assert hasattr(brief.artifacts_in, "items")


def test_brief_without_goal_is_left_to_the_tool_layer(delegation_env):
    """The loop returns None; the tool layer owns the refusal wording."""
    assert L._agent_brief_from_args({"request": {"brief": "no goal"}}) is None
    assert L._agent_brief_from_args({}) is None
    assert L._agent_brief_from_args({"request": "a string"}) is None


def test_brief_passes_through_unchanged(delegation_env):
    from core.orchestration.contracts import AgentBrief

    original = AgentBrief(goal="g")
    assert L._agent_brief_from_args({"request": original}) is original


def test_delegation_call_names_are_the_documented_set():
    """The fan-out phase recognizes every call that touches the run tree.

    Launching is not the only delegation call: observing and resuming a
    child also belongs to the tree, and so does waiting for one to finish,
    so all six names have to be routed by the same phase instead of falling
    into the plain tool path.
    """
    assert L._DELEGATION_CALL_NAMES == (
        "spawn_agent", "invoke_assistant", "subagent_status",
        "read_subagent_state", "resume_subagent", "sleep")
