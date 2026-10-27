# -*- coding: utf-8 -*-
"""tests/test_orchestration_service.py - the run registry, limits and cancellation.

Every test builds its OWN ``OrchestrationService`` instead of touching the
process-wide singleton: the registry is mutable per-tree state, and a shared
instance would leak a pool semaphore or a run record from one test into the
next. The child runner is a fake, so these tests measure the delegation
runtime - limits, refusals, event delivery, cancellation - and never a model
call.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import service as S  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    ERR_CANCELLED_BY_PARENT,
    ERR_LIMIT_EXCEEDED,
    SpawnRequest,
    STATUS_CANCELLED,
    STATUS_RUNNING,
    STATUS_DONE,
    STOP_ABORTED,
    STOP_COMPLETED,
)


@pytest.fixture
def svc(tmp_path):
    """A private service whose children return immediately."""
    service = S.OrchestrationService()
    service.set_store_root(str(tmp_path / "runs"))
    service.set_child_runner(_fast_runner)
    return service


def _fast_runner(**kwargs):
    """Fake child run: reports success and records which child it was."""
    return AgentRunResult(
        run_id=kwargs["run_id"], status=STATUS_DONE,
        stop_reason=STOP_COMPLETED,
        summary="done by " + str(kwargs["child_slug"]),
        steps_used=1, tokens_used={"in": 10, "out": 5},
    )


def _spawn(service, *, parent_run_id=None, thread_id="", slug="dev_agent",
           config=None, base_dir=None):
    """Call ``service.run`` with a minimal valid request."""
    return service.run(
        SpawnRequest(agent_slug=slug, request=AgentBrief(goal="do it"),
                     thread_id=thread_id, parent_run_id=parent_run_id),
        parent_config=config, base_dir=base_dir, lang="English")


# ─── limit resolution ───────────────────────────────────────────────────────


def test_read_limits_falls_back_to_documented_defaults():
    limits = S.read_limits(None)
    assert limits["max_depth"] == 1
    assert limits["max_active_subagents"] == 5
    assert limits["subagent_max_steps"] == 300
    assert limits["subagent_budget_tokens"] == 0
    assert limits["subagent_timeout_seconds"] == 3600
    assert limits["total_subagent_runs"] == 64


def test_read_limits_never_removes_a_limit():
    """Garbage and zero values clamp instead of switching a limit off."""
    limits = S.read_limits({"max_active_subagents": 0,
                            "subagent_timeout_seconds": -5,
                            "max_depth": "nonsense",
                            "total_subagent_runs": None})
    assert limits["max_active_subagents"] == 1
    assert limits["subagent_timeout_seconds"] == 3600
    assert limits["max_depth"] == 1
    assert limits["total_subagent_runs"] == 64


def test_read_limits_honours_explicit_values():
    limits = S.read_limits({"max_depth": 4, "max_active_subagents": 7,
                            "total_subagent_runs": 100})
    assert limits["max_depth"] == 4
    assert limits["max_active_subagents"] == 7
    assert limits["total_subagent_runs"] == 100


def test_a_timeout_above_the_ceiling_is_clamped_to_one_hour():
    """A run budget is bounded: a parent can wait an hour, not a day."""
    limits = S.read_limits({"subagent_timeout_seconds": 100_000})
    assert limits["subagent_timeout_seconds"] == S.MAX_SUBAGENT_TIMEOUT_SECONDS
    assert limits["subagent_timeout_seconds"] == 3600


# ─── registry ───────────────────────────────────────────────────────────────


def test_ensure_run_is_idempotent_and_links_children(svc):
    root = svc.ensure_run("root", thread_id="t1", agent_slug="dev_agent")
    again = svc.ensure_run("root", thread_id="other")
    assert again is root
    assert root.depth == 0
    assert root.root_run_id == "root"

    child = svc.ensure_run("child", parent_run_id="root", depth=1)
    assert child.root_run_id == "root"
    assert svc.get_record("root").children == ["child"]


def test_finish_run_stores_the_terminal_state(svc):
    svc.ensure_run("root")
    result = AgentRunResult(run_id="root", status=STATUS_DONE,
                            stop_reason=STOP_COMPLETED, summary="all good",
                            error_code=None, steps_used=4,
                            tokens_used={"in": 1})
    svc.finish_run("root", result)

    record = svc.get_record("root")
    assert record.status == STATUS_DONE
    assert record.summary == "all good"
    assert record.steps_used == 4
    assert record.tokens_used == {"in": 1}


def test_release_tree_forgets_a_finished_tree(svc):
    """A tree with no running run is dropped completely."""
    svc.ensure_run("root")
    svc.ensure_run("child", parent_run_id="root", depth=1)
    svc.finish_run("root", AgentRunResult(run_id="root", status=STATUS_DONE))
    svc.finish_run("child", AgentRunResult(run_id="child", status=STATUS_DONE))

    report = svc.release_tree("root")

    assert report == {"released": True, "deferred": False}
    assert svc.get_record("root") is None
    assert svc.get_record("child") is None
    assert svc.pool_active("root") == 0


def test_release_tree_is_deferred_while_a_child_still_runs(svc):
    """A background child must stay cancellable after the dialog finishes.

    This is the whole reason the release is conditional: wiping the records of
    a running child would make ``is_cancelled`` find nothing, and the child
    would become unkillable until the process exits.
    """
    svc.ensure_run("root")
    svc.ensure_run("child", parent_run_id="root", depth=1)

    report = svc.release_tree("root")

    assert report == {"released": False, "deferred": True}
    assert svc.get_record("child") is not None
    assert svc.get_record("child").status == STATUS_RUNNING


def test_derive_child_thread_id_is_unique_per_run():
    a = S.derive_child_thread_id("parent", "aaaaaaaa1111")
    b = S.derive_child_thread_id("parent", "bbbbbbbb2222")
    assert a == "parent__saaaaaaaa"
    assert b.startswith("parent__s")
    assert a != b
    assert S.derive_child_thread_id("", "cccccccc").startswith("nothread__s")


# ─── depth and total-run limits ─────────────────────────────────────────────


def test_depth_limit_refuses_a_grandchild(svc):
    """max_depth=1: a child may run, its own child may not."""
    config = {"max_depth": 1}
    svc.ensure_run("root", config=config)

    first = _spawn(svc, parent_run_id="root", config=config)
    assert first["ok"] is True

    child_run_id = first["result"]["run_id"]
    second = _spawn(svc, parent_run_id=child_run_id, config=config)
    assert second["ok"] is False
    assert second["error_code"] == ERR_LIMIT_EXCEEDED
    assert second["limit"] == "max_depth"
    assert second["value"] == 1


def test_total_run_limit_stops_the_tree(svc):
    """The root counts as the first run, so total=3 allows exactly 2 children."""
    config = {"total_subagent_runs": 3}
    svc.ensure_run("root", config=config)

    assert _spawn(svc, parent_run_id="root", config=config)["ok"] is True
    assert _spawn(svc, parent_run_id="root", config=config)["ok"] is True

    third = _spawn(svc, parent_run_id="root", config=config)
    assert third["ok"] is False
    assert third["limit"] == "total_subagent_runs"
    assert third["used"] == 3


def test_refused_spawn_does_not_consume_a_run_slot(svc):
    """A depth refusal happens before the counter is touched."""
    config = {"max_depth": 0, "total_subagent_runs": 5}
    svc.ensure_run("root", config=config)

    refused = _spawn(svc, parent_run_id="root", config=config)
    assert refused["limit"] == "max_depth"

    config_ok = {"max_depth": 2, "total_subagent_runs": 5}
    allowed = _spawn(svc, parent_run_id="root", config=config_ok)
    assert allowed["ok"] is True


def test_pool_slot_refusal_reports_max_active_subagents(svc):
    """An exhausted pool refuses immediately instead of queueing."""
    config = {"max_active_subagents": 1, "subagent_timeout_seconds": 1}
    svc.ensure_run("root", config=config)
    slot = svc.acquire_pool("root")
    assert slot is not None and bool(slot) is True
    try:
        refused = _spawn(svc, parent_run_id="root", config=config)
    finally:
        svc.release_pool("root", slot)

    assert refused["ok"] is False
    assert refused["limit"] == "max_active_subagents"
    assert refused["active"] == 1


def test_pool_active_tracks_slots(svc):
    svc.ensure_run("root", config={"max_active_subagents": 3})
    assert svc.pool_active("root") == 0
    svc.acquire_pool("root")
    svc.acquire_pool("root")
    assert svc.pool_active("root") == 2
    svc.release_pool("root")
    assert svc.pool_active("root") == 1


# ─── events ─────────────────────────────────────────────────────────────────


def test_child_events_reach_the_root_subscriber(svc):
    """A subscriber of the dialog receives its descendants' events."""
    svc.ensure_run("root")
    svc.ensure_run("child", parent_run_id="root", depth=1)
    seen = []
    svc.subscribe("root", seen.append)

    svc.publish({"type": "tool_call", "run_id": "child", "tool": "read_file"})

    assert seen == [{"type": "tool_call", "run_id": "child", "tool": "read_file"}]


def test_unsubscribe_stops_delivery(svc):
    svc.ensure_run("root")
    seen = []
    off = svc.subscribe("root", seen.append)
    svc.publish({"type": "phase", "run_id": "root"})
    off()
    svc.publish({"type": "phase", "run_id": "root"})
    assert len(seen) == 1


def test_publish_ignores_events_of_unknown_runs(svc):
    svc.ensure_run("root")
    seen = []
    svc.subscribe("root", seen.append)
    svc.publish({"type": "phase", "run_id": "nobody"})
    svc.publish({"type": "phase"})
    svc.publish("not a dict")
    assert seen == []


def test_spawn_publishes_started_and_finished(svc):
    svc.ensure_run("root", config={"max_depth": 2})
    seen = []
    svc.subscribe("root", seen.append)

    _spawn(svc, parent_run_id="root", config={"max_depth": 2}, slug="helper")

    types = [ev.get("type") for ev in seen]
    assert types == ["run_started", "run_finished"]
    assert all(ev.get("agent_slug") == "helper" for ev in seen)
    assert seen[0]["depth"] == 1
    assert seen[1]["payload"]["status"] == STATUS_DONE


# ─── cancellation ───────────────────────────────────────────────────────────


def test_cancel_cascades_to_descendants(svc):
    svc.ensure_run("root")
    svc.ensure_run("child", parent_run_id="root", depth=1)
    svc.ensure_run("grandchild", parent_run_id="child", depth=2)

    report = svc.cancel("root")

    assert report.cancelled is True
    assert report.reason == "requested"
    assert set(report.children) == {"child", "grandchild"}
    assert svc.is_cancelled("root") is True
    assert svc.is_cancelled("grandchild") is True
    assert svc.get_record("grandchild").error_code == ERR_CANCELLED_BY_PARENT
    assert svc.get_record("grandchild").stop_reason == STOP_ABORTED


def test_cancel_without_cascade_leaves_children_running(svc):
    svc.ensure_run("root")
    svc.ensure_run("child", parent_run_id="root", depth=1)

    report = svc.cancel("root", cascade=False)

    assert report.children == []
    assert svc.is_cancelled("child") is False


def test_cancel_is_idempotent(svc):
    svc.ensure_run("root")
    assert svc.cancel("root").cancelled is True

    second = svc.cancel("root")
    assert second.cancelled is False
    assert second.reason == "already_terminal"
    assert second.previous_status == STATUS_CANCELLED


def test_cancel_of_unknown_run_reports_unknown_run(svc):
    report = svc.cancel("nope")
    assert report.cancelled is False
    assert report.reason == "unknown_run"


# ─── fan-out ────────────────────────────────────────────────────────────────


def test_spawn_many_keeps_call_order(svc):
    """Results are positional, so the parent pairs them with its own calls."""
    config = {"max_depth": 1, "max_active_subagents": 5}
    svc.ensure_run("root", config=config)

    requests = [
        SpawnRequest(agent_slug="agent_" + str(i),
                     request=AgentBrief(goal="task " + str(i)),
                     parent_run_id="root")
        for i in range(4)
    ]
    results = svc.spawn_many(requests, parent_config=config, lang="English")

    assert len(results) == 4
    for i, result in enumerate(results):
        assert result["ok"] is True
        assert result["result"]["summary"] == "done by agent_" + str(i)


def test_spawn_many_runs_children_in_parallel(tmp_path):
    """N children with a common barrier finish only if they truly overlap."""
    service = S.OrchestrationService()
    service.set_store_root(str(tmp_path / "runs"))
    barrier = threading.Barrier(3, timeout=5)

    def _runner(**kwargs):
        # Every child waits for its siblings: sequential execution would
        # time out the barrier and surface as a failure instead of a pass.
        barrier.wait()
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED, summary="ok")

    service.set_child_runner(_runner)
    config = {"max_depth": 1, "max_active_subagents": 3}
    service.ensure_run("root", config=config)

    requests = [SpawnRequest(agent_slug="a", request=AgentBrief(goal="g"),
                             parent_run_id="root") for _ in range(3)]
    started = time.monotonic()
    results = service.spawn_many(requests, parent_config=config)
    elapsed = time.monotonic() - started

    assert [r["ok"] for r in results] == [True, True, True]
    assert elapsed < 5


def test_spawn_many_is_empty_for_an_empty_batch(svc):
    assert svc.spawn_many([]) == []


# ─── child tool bans ────────────────────────────────────────────────────────


def test_invoke_takes_a_pool_slot(svc):
    """Test 16: a one-shot call is provider load, so it obeys the pool."""
    from core.orchestration.contracts import InvokeRequest

    config = {"max_active_subagents": 1, "subagent_timeout_seconds": 1}
    svc.ensure_run("root", config=config)
    slot = svc.acquire_pool("root")
    assert bool(slot) is True

    calls = []
    try:
        result = svc.invoke(
            InvokeRequest(system="p", user="hi", parent_run_id="root"),
            parent_config=config,
            invoke_fn=lambda req, **kw: calls.append(req) or {"ok": True})
    finally:
        svc.release_pool("root", slot)

    assert result["error_code"] == ERR_LIMIT_EXCEEDED
    assert result["limit"] == "max_active_subagents"
    assert calls == []


def test_invoke_releases_its_slot_after_the_call(svc):
    from core.orchestration.contracts import InvokeRequest

    config = {"max_active_subagents": 2}
    svc.ensure_run("root", config=config)

    svc.invoke(InvokeRequest(system="p", user="hi", parent_run_id="root"),
               parent_config=config, invoke_fn=lambda req, **kw: {"ok": True})

    assert svc.pool_active("root") == 0


def test_child_disabled_tools_forbid_entity_creation():
    """A child never multiplies platform entities in v1."""
    for name in ("create_orchestrator", "update_orchestrator",
                 "delete_orchestrator", "create_assistant_for_task",
                 "update_assistant_by_id"):
        assert name in S.CHILD_DISABLED_TOOLS
    assert "spawn_agent" not in S.CHILD_DISABLED_TOOLS
# ─── child task text: absolute paths ────────────────────────────────────────


def test_child_task_text_carries_goal_brief_and_artifacts():
    """The child sees the goal, the brief and every input artifact."""
    brief = AgentBrief(goal="Сделай отчёт", brief="по программе курса",
                       artifacts_in={"программа": "/tmp/program.docx"})

    text = S._build_child_task_text(brief, "/tmp/workspace", None)

    assert "Сделай отчёт" in text
    assert "по программе курса" in text
    assert "программа: /tmp/program.docx" in text


def test_child_task_text_states_absolute_workspace():
    """The shared workspace reaches the child as an ABSOLUTE path."""
    text = S._build_child_task_text(AgentBrief(goal="go"), "/tmp/workspace", "")

    assert "Рабочая папка: /tmp/workspace" in text


def test_child_task_text_lists_parent_dialog_files(monkeypatch):
    """The parent's uploads folder is stated so no path guessing is needed."""
    monkeypatch.setattr(S, "_thread_files_dir_of",
                        lambda tid: "/data/history/tid/files" if tid else "")

    text = S._build_child_task_text(AgentBrief(goal="go"), None, "parent-1")

    assert "Файлы диалога родителя: /data/history/tid/files" in text


def test_child_task_text_without_context_stays_minimal():
    """No workspace and no thread: the text is just the goal."""
    text = S._build_child_task_text(AgentBrief(goal="go"), None, None)

    assert text == "go"
    assert "Рабочая папка" not in text


def test_child_task_text_ignores_a_blank_workspace():
    """A whitespace workspace must not produce an empty path line."""
    text = S._build_child_task_text(AgentBrief(goal="go"), "   ", "")

    assert text == "go"
    assert "Рабочая папка" not in text


# ─── failure classification ────────────────────────────────────────────────


def _raising_runner(**kwargs):
    """A runner that hits a defect inside the loop, not a provider error."""
    raise TypeError("'dict' object has no attribute 'steps'")


def test_a_defect_in_the_loop_is_not_reported_as_a_provider_error(svc):
    """An internal defect gets its own code, and a retry is discouraged.

    The two failures demand opposite reactions: a provider hiccup may pass on
    a second attempt, a platform defect never does. Sharing one code made the
    parent retry a deterministic crash and hid the real cause behind a
    plausible one.
    """
    svc.set_child_runner(_raising_runner)
    svc.ensure_run("root", config={"max_depth": 1})

    out = _spawn(svc, parent_run_id="root")

    assert out["ok"] is True
    result = out["result"]
    assert result["status"] == "failed"
    assert result["error_code"] == S.ERR_INTERNAL_ERROR
    assert result["retryable"] is False
    assert "TypeError" in result["summary"]


def test_an_internal_failure_keeps_the_exception_type_in_the_summary(svc):
    """The summary names the exception class: without it diagnosis is guesswork."""
    svc.set_child_runner(_raising_runner)
    svc.ensure_run("root", config={"max_depth": 1})

    summary = _spawn(svc, parent_run_id="root")["result"]["summary"]

    assert summary.startswith("TypeError: ")


def test_the_durable_record_carries_the_retryable_flag(svc):
    """A caller reading the store after a restart must see the same verdict."""
    svc.set_child_runner(_raising_runner)
    svc.ensure_run("root", config={"max_depth": 1})

    run_id = _spawn(svc, parent_run_id="root")["result"]["run_id"]

    stored = svc.record_from_store(run_id)
    assert stored is not None
    assert stored["retryable"] is False
    assert stored["error_code"] == S.ERR_INTERNAL_ERROR


# ─── frozen run identity ───────────────────────────────────────────────────


def test_child_record_carries_child_thread_id_and_workspace(svc):
    """The record answers where the child worked and which thread it used.

    Both fields are resolved at spawn time and frozen on the record: without
    them a background child is a black box - its thread cannot be addressed
    and its working folder cannot be told from the parent's.
    """
    svc.ensure_run("root", config={"max_depth": 1})

    out = _spawn(svc, parent_run_id="root", base_dir="/tmp/workspace")

    run_id = out["result"]["run_id"]
    stored = svc.record_from_store(run_id)
    assert stored["child_thread_id"]
    assert stored["workspace"] == "/tmp/workspace"


def test_the_child_thread_differs_from_the_parent_thread(svc):
    """Delegated work never writes into the parent's journal."""
    svc.ensure_run("parent-thread", thread_id="parent-thread",
                   config={"max_depth": 1})

    out = _spawn(svc, parent_run_id="parent-thread", thread_id="parent-thread")

    child_thread = svc.record_from_store(out["result"]["run_id"])["child_thread_id"]
    assert child_thread
    assert child_thread != "parent-thread"


def test_an_explicit_workspace_wins_over_the_base_dir(svc):
    """The spawn argument is the truth when the caller stated it."""
    svc.ensure_run("root", config={"max_depth": 1})

    out = svc.run(
        SpawnRequest(agent_slug="dev_agent", request=AgentBrief(goal="go"),
                     parent_run_id="root", workspace="/tmp/explicit"),
        parent_config={"max_depth": 1}, base_dir="/tmp/fallback", lang="English")

    stored = svc.record_from_store(out["result"]["run_id"])
    assert stored["workspace"] == "/tmp/explicit"


# ─── runtime handoff ───────────────────────────────────────────────────────


def test_a_finished_run_leaves_its_outcome_in_the_child_journal(svc, monkeypatch, tmp_path):
    """The parent can read HOW a child ended even if the child wrote nothing.

    A child keeps a journal only when it decides to. Without the runtime
    paragraph the documented handoff channel answered nothing, and an empty
    journal looked like a child that did nothing rather than one that never
    wrote its work down.
    """
    seen = {}

    def _fake_handoff(thread_id, **kwargs):
        seen["thread_id"] = thread_id
        seen.update(kwargs)
        return {"ok": True, "wrote": True}

    import dev_agent.task_state as TS
    monkeypatch.setattr(TS, "record_runtime_handoff", _fake_handoff)
    svc.ensure_run("root", config={"max_depth": 1})

    out = _spawn(svc, parent_run_id="root")

    assert seen["thread_id"] == out["result"]["child_thread_id"]
    assert seen["run_id"] == out["result"]["run_id"]
    assert seen["status"] == "done"
    assert seen["steps_used"] == 1
    assert seen["retryable"] is True


def test_the_handoff_records_the_failure_classification(svc, monkeypatch):
    """A failed run records its code and whether a retry is worth trying."""
    seen = {}

    def _fake_handoff(thread_id, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "wrote": True}

    import dev_agent.task_state as TS
    monkeypatch.setattr(TS, "record_runtime_handoff", _fake_handoff)
    svc.set_child_runner(_raising_runner)
    svc.ensure_run("root", config={"max_depth": 1})

    _spawn(svc, parent_run_id="root")

    assert seen["error_code"] == S.ERR_INTERNAL_ERROR
    assert seen["retryable"] is False
    assert seen["summary"].startswith("TypeError: ")


def test_a_broken_journal_does_not_fail_a_finished_run(svc, monkeypatch):
    """The handoff is an aid: its failure must not change the outcome."""
    import dev_agent.task_state as TS

    def _boom(*args, **kwargs):
        raise OSError("journal is unwritable")

    monkeypatch.setattr(TS, "record_runtime_handoff", _boom)
    svc.ensure_run("root", config={"max_depth": 1})

    out = _spawn(svc, parent_run_id="root")

    assert out["result"]["status"] == "done"
