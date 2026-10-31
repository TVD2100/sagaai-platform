# -*- coding: utf-8 -*-
"""tests/test_orchestration_sleep.py - the blocking wait for a background child.

The tool answers one question: "who of my children finished, or is the time
up?". These tests pin the answer's contract instead of the mechanism: the
event, the timeout, the cancellation, the ceiling, the untouched mailbox and
the identifier-only payload.

No test sleeps real time. ``time`` is replaced INSIDE
``core.orchestration.tools`` by a clock that advances by the requested amount
and returns at once, so a wait of 300 seconds costs microseconds; a hook run
on every tick lets a test change the tree between two polls, which is what
makes "a child finished during the sleep" reproducible instead of racy.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import service as S  # noqa: E402
from core.orchestration import tools as T  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentRunResult,
    ERR_INVALID_ARGS,
    STATUS_DONE,
    STOP_COMPLETED,
)


# ─── fixtures ──────────────────────────────────────────────────────────────


class FakeClock:
    """A monotonic clock that advances on sleep instead of blocking.

    ``on_tick`` runs after every advance with the 1-based tick number, so a
    test can finish a child, post a notice or cancel a run at an exact point
    of the wait without depending on thread timing.
    """

    def __init__(self, on_tick=None):
        self.now = 1000.0
        self.ticks = 0
        self.sleeps = []
        self._on_tick = on_tick

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        step = max(0.0, float(seconds or 0.0))
        self.sleeps.append(step)
        self.now += step
        self.ticks += 1
        if self._on_tick is not None:
            self._on_tick(self.ticks)

    @property
    def spent(self):
        return round(sum(self.sleeps), 6)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """A private service with its OWN store root (never the real one)."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    service = S.OrchestrationService()
    service.set_store_root(str(tmp_path / "orchestration"))
    return service


@pytest.fixture
def clock(monkeypatch):
    """Install a controllable clock into the tools module; return a factory."""
    installed = {}

    def _install(on_tick=None):
        fake = FakeClock(on_tick=on_tick)
        monkeypatch.setattr(T, "time", fake)
        installed["clock"] = fake
        return fake

    return _install


def _instant_runner(**kwargs):
    """A child that finishes immediately (no provider, no loop)."""
    return AgentRunResult(
        run_id=kwargs["run_id"], status=STATUS_DONE,
        stop_reason=STOP_COMPLETED, summary="done", steps_used=1,
    )


def _child(svc, root, run_id, *, parent=None, status=None):
    """Register a child of *root* and optionally force its status."""
    rec = svc.ensure_run(run_id, parent_run_id=parent or root, depth=1)
    if status is not None:
        rec.status = status
    return rec


# ─── argument validation ───────────────────────────────────────────────────


def test_missing_seconds_is_an_invalid_args_refusal(svc, clock):
    """1. Without a ceiling there is no wait: the call is refused by name."""
    clock()

    out = T.sleep_tool(service=svc)

    assert out["ok"] is False
    assert out["error_code"] == ERR_INVALID_ARGS
    assert out["missing"] == ["seconds"]


def test_non_positive_seconds_is_refused(svc, clock):
    """2. A zero or negative ceiling is a mistake, not a no-op wait."""
    clock()

    for value in (0, -5):
        out = T.sleep_tool(seconds=value, service=svc)
        assert out["ok"] is False, value
        assert out["error_code"] == ERR_INVALID_ARGS, value
        assert out["invalid"] == ["seconds"], value


def test_unknown_until_value_is_refused(svc, clock):
    """Only the documented modes exist; an invented one is refused."""
    clock()

    out = T.sleep_tool(seconds=1, until="some_children", service=svc)

    assert out["ok"] is False
    assert out["error_code"] == ERR_INVALID_ARGS
    assert out["allowed"] == ["any_child", "all_children", "timeout"]


def test_all_children_waits_for_the_whole_fanout(svc, clock):
    """A fan-out is waited on as a batch, not child by child.

    ``any_child`` returns after the first completion and makes the model call
    sleep once per child; ``all_children`` ends only when no child of the tree
    is still running, which is what a parent that launched several auditors
    actually wants.
    """
    svc.ensure_run("root", thread_id="thread-1")
    kids = [_child(svc, "root", name)
            for name in ("child-a", "child-b", "child-c")]

    def _finish(tick):
        # One child finishes on every tick: the wait must not end before the
        # tree is quiet, and must end as soon as it is.
        if tick <= len(kids):
            kids[tick - 1].status = STATUS_DONE

    clock(on_tick=_finish)

    out = T.sleep_tool(seconds=60, parent_run_id="root",
                       until="all_children", service=svc)

    assert out["ok"] is True
    assert out["woken_by"] == "all_done"
    assert out["finished"] == ["child-a", "child-b", "child-c"]
    assert out["slept"] < 60


def test_all_children_returns_at_once_when_nothing_is_running(svc, clock):
    """An already-quiet tree must not burn the whole wait."""
    fake = clock()
    svc.ensure_run("root", thread_id="thread-1")
    _child(svc, "root", "child-a").status = STATUS_DONE

    out = T.sleep_tool(seconds=60, parent_run_id="root",
                       until="all_children", service=svc)

    assert out["woken_by"] == "all_done"
    assert out["finished"] == []
    assert out["slept"] == 0.0
    assert fake.spent == 0


def test_seconds_above_the_ceiling_is_clamped_not_refused(svc, clock):
    """3. A too-long wait is shortened, and the answer says so.

    The ceiling is an hour: a child agent is a full agent, not a run_code
    command, so a wait of many minutes is legitimate. Ten thousand seconds
    is still above it and must be clamped.
    """
    fake = clock()

    out = T.sleep_tool(seconds=10_000, until="timeout", service=svc)

    assert out["ok"] is True
    assert out["clamped"] is True
    assert out["woken_by"] == "timeout"
    assert out["slept"] <= T.MAX_SLEEP_SECONDS
    assert fake.spent <= T.MAX_SLEEP_SECONDS
    assert T.MAX_SLEEP_SECONDS == 3600.0


def test_the_sleep_ceiling_matches_the_child_run_budget():
    """Two numbers, one meaning: how long a child may work.

    A parent that waits longer than the child budget wastes the wait, and a
    child allowed to outlive the wait is killed while the parent still
    believes it is working. The two constants therefore share one source.
    """
    from core.orchestration import contracts as C

    assert T.MAX_SLEEP_SECONDS == float(C.MAX_SUBAGENT_TIMEOUT_SECONDS)
    assert C.DEFAULT_SUBAGENT_TIMEOUT_SECONDS <= C.MAX_SUBAGENT_TIMEOUT_SECONDS


def test_a_long_wait_within_the_hour_is_not_clamped(svc, clock):
    """3b. A wait of many minutes is a normal wait, not a hang.

    The ceiling used to be the five-minute run_code limit, which shortened a
    perfectly reasonable wait for a working child agent. Thirty minutes is
    below the one-hour ceiling and must run to completion unrescaled.
    """
    fake = clock()

    out = T.sleep_tool(seconds=1800, until="timeout", service=svc)

    assert out["ok"] is True
    assert out["clamped"] is False
    assert out["woken_by"] == "timeout"
    assert abs(fake.spent - 1800.0) < 0.05
    assert abs(out["slept"] - 1800.0) < 0.05


# ─── timeout ───────────────────────────────────────────────────────────────


def test_timeout_mode_waits_the_full_time_without_children(svc, clock):
    """4. No children: the wait ends exactly at the ceiling it was given."""
    fake = clock()
    svc.ensure_run("root", thread_id="thread-1")

    out = T.sleep_tool(seconds=2, until="timeout", parent_run_id="root",
                       service=svc)

    assert out["ok"] is True
    assert out["woken_by"] == "timeout"
    assert out["finished"] == []
    assert out["clamped"] is False
    assert abs(fake.spent - 2.0) < 0.05
    assert abs(out["slept"] - 2.0) < 0.05


def test_any_child_without_children_ends_by_timeout(svc, clock):
    """11. The default mode behaves like a plain timeout when nobody runs."""
    fake = clock()
    svc.ensure_run("root", thread_id="thread-1")

    out = T.sleep_tool(seconds=1, parent_run_id="root", service=svc)

    assert out["ok"] is True
    assert out["woken_by"] == "timeout"
    assert out["finished"] == []
    assert abs(fake.spent - 1.0) < 0.05


def test_a_child_finished_before_the_sleep_does_not_wake_it(svc, clock):
    """7. The snapshot is taken at the start: old news is not an event."""
    clock()
    svc.ensure_run("root", thread_id="thread-1")
    _child(svc, "root", "child-old", status=STATUS_DONE)

    out = T.sleep_tool(seconds=1, parent_run_id="root", service=svc)

    assert out["woken_by"] == "timeout"
    assert out["finished"] == []


# ─── events ────────────────────────────────────────────────────────────────


def test_a_child_finishing_during_the_sleep_wakes_it(svc, clock):
    """5. The child becomes terminal mid-wait: event, and its id is reported."""
    svc.ensure_run("root", thread_id="thread-1")
    rec = _child(svc, "root", "child-1")

    def _finish(tick):
        if tick == 3:
            rec.status = STATUS_DONE

    clock(on_tick=_finish)

    out = T.sleep_tool(seconds=10, parent_run_id="root", service=svc)

    assert out["ok"] is True
    assert out["woken_by"] == "event"
    assert out["finished"] == ["child-1"]
    assert out["slept"] < 10


def test_only_the_child_that_finished_is_reported(svc, clock):
    """6. Two children, one terminal: exactly one id comes back."""
    svc.ensure_run("root", thread_id="thread-1")
    first = _child(svc, "root", "child-a")
    _child(svc, "root", "child-b")

    def _finish(tick):
        if tick == 2:
            first.status = STATUS_DONE

    clock(on_tick=_finish)

    out = T.sleep_tool(seconds=10, parent_run_id="root", service=svc)

    assert out["woken_by"] == "event"
    assert out["finished"] == ["child-a"]


def test_a_pending_notice_wakes_the_sleep_without_ids(svc, clock):
    """8. A notice with no new terminal run is still an event, with no ids."""
    fake = clock()
    svc.ensure_run("root", thread_id="thread-1")
    assert svc.post_notification("root", {"type": "run_finished",
                                          "run_id": "child-x"}) is True

    out = T.sleep_tool(seconds=5, parent_run_id="root", service=svc)

    assert out["woken_by"] == "event"
    assert out["finished"] == []
    assert fake.ticks == 0  # the very first check already saw the notice


def test_a_child_resolves_the_tree_root_from_its_parent_record(svc, clock):
    """8 (from a child). A child knows only its parent; the root is derived."""
    fake = clock()
    svc.ensure_run("root", thread_id="thread-1")
    _child(svc, "root", "child-1", parent="root")
    grandchild = svc.ensure_run("grand-1", parent_run_id="child-1", depth=2)
    assert grandchild.root_run_id == "root"
    assert svc.post_notification("root", {"type": "run_finished",
                                          "run_id": "child-1"}) is True

    out = T.sleep_tool(seconds=5, parent_run_id="grand-1", service=svc)

    assert out["woken_by"] == "event"
    assert fake.ticks == 0


def test_the_mailbox_is_inspected_and_never_drained(svc, clock):
    """9. Sleeping must not steal the notice from the next step."""
    clock()
    svc.ensure_run("root", thread_id="thread-1")
    svc.post_notification("root", {"type": "run_finished", "run_id": "c"})

    T.sleep_tool(seconds=5, parent_run_id="root", service=svc)

    assert svc.has_notifications("root") is True
    assert len(svc.take_notifications("root")) == 1


# ─── cancellation ──────────────────────────────────────────────────────────


def test_a_cancelled_parent_leaves_the_sleep_at_once(svc, clock):
    """10. A cancellation is a reason to stop waiting, not to keep sleeping."""
    fake = clock()
    svc.ensure_run("root", thread_id="thread-1")

    def _cancel(tick):
        if tick == 2:
            svc.cancel("root")

    clock(on_tick=_cancel)

    out = T.sleep_tool(seconds=60, parent_run_id="root", service=svc)

    assert out["ok"] is True
    assert out["woken_by"] == "cancelled"
    assert out["finished"] == []
    assert out["slept"] < 60
    assert fake.spent < 60


# ─── registration ──────────────────────────────────────────────────────────


def test_the_tool_is_enabled_in_the_catalog():
    """12. The catalog offers sleep to any orchestrator, like spawn_agent."""
    from core.orchestrator_tools import is_conditional_tool_enabled

    assert is_conditional_tool_enabled("sleep", "probe_orch") is True
    assert is_conditional_tool_enabled("sleep", "dev_agent") is True


def test_the_catalog_entry_says_what_the_tool_answers():
    """The description names both the stop conditions and the id-only rule."""
    entry = T.sleep_tool_entry()

    assert entry["name"] == "sleep"
    assert "any_child" in entry["desc"]
    assert "timeout" in entry["desc"]
    assert "subagent_status" in entry["desc"]
    assert "does NOT cancel" in entry["desc"]


def test_the_name_is_routed_by_the_delegation_phase():
    """13. The loop must not send sleep down the plain tool path."""
    from dev_agent import agent_loop as L
    from dev_agent import universal_agent as UA

    assert "sleep" in L._DELEGATION_CALL_NAMES
    assert "sleep" in UA.DELEGATION_TOOL_NAMES


def test_unknown_argument_names_are_refused_before_the_call():
    """14. sleep(sec=10) is a naming mistake, reported as one."""
    from dev_agent.universal_agent import validate_tool_call

    refusal = validate_tool_call("sleep", {"sec": 10}, "dev_agent")

    assert refusal is not None
    assert refusal["unknown_args"] == ["sec"]
    assert "seconds" in refusal["suggestion"]
    assert validate_tool_call("sleep", {"seconds": 10}, "dev_agent") is None
    assert validate_tool_call("sleep", {"seconds": 10, "until": "timeout"},
                              "dev_agent") is None


# ─── the end-to-end step ───────────────────────────────────────────────────


def test_a_background_spawn_and_a_sleep_in_one_step(monkeypatch, tmp_path):
    """15. The acceptance scenario: launch in the background, then wait.

    A real service, a real ``spawn_agent`` and a real ``sleep`` in ONE step:
    the sleep is interrupted by the child's finish and reports its run id.
    Only the child runner is a fake, so no provider call happens.
    """
    from dev_agent import agent_loop as L
    import core.orchestrators as orch_mod

    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    service = S.OrchestrationService()
    service.set_store_root(str(tmp_path / "orchestration"))
    service.set_child_runner(_instant_runner)
    service.ensure_run("root", thread_id="thread-1")

    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": {}})
    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": ["dev_agent"])
    monkeypatch.setattr(orch_mod, "get_disabled_tools",
                        lambda slug="dev_agent": [])

    class _Dispatcher:
        child_disabled_tools = set()

        @staticmethod
        def _current_orchestrator_slug():
            return "dev_agent"

    state = L.AgentLoopState(task="t", thread_id="thread-1", run_id="root")
    calls = [
        {"tool": "spawn_agent",
         "args": {"agent_slug": "dev_agent",
                  "request": {"goal": "do it"},
                  "background": True}},
        {"tool": "sleep", "args": {"seconds": 120, "until": "any_child"}},
    ]

    results = L.execute_delegation_calls(calls, state=state,
                                         dispatcher=_Dispatcher(),
                                         service=service)

    spawned, slept = results
    assert spawned["ok"] is True, spawned
    run_id = spawned["run_id"]
    assert slept["ok"] is True, slept
    assert slept["woken_by"] == "event"
    assert run_id in slept["finished"]
    assert slept["slept"] < 120
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
