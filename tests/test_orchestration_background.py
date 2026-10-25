# -*- coding: utf-8 -*-
"""tests/test_orchestration_background.py - background runs of the runtime.

What separates a background run from a normal one is that the dialog may be
gone by the time it ends: the step finished, ``release_tree`` ran, and the
child is still working. These tests pin the four behaviours that makes such a
run usable rather than a leak:

- ``start`` returns at once and refuses OVER-limit work synchronously;
- the outcome stays readable after the tree was released (``poll`` falls back
  to the durable store);
- ``release_tree`` is deferred, never destructive, while a child runs;
- a cancellation that lands during the child's own finish is not overwritten
  by its ``done`` (the compare-and-set).

The child runner is a fake controlled by an ``Event``, so every state below is
reproducible: no test depends on how fast a thread happens to be.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import service as S  # noqa: E402
from core.orchestration import runs as R  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    ERR_LIMIT_EXCEEDED,
    ERR_NO_RUN_IDENTITY,
    ERR_RUN_NOT_FOUND,
    SpawnRequest,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_RUNNING,
    STOP_ABORTED,
    STOP_COMPLETED,
)


@pytest.fixture
def svc(tmp_path):
    """A private service with its OWN store root (never the real one)."""
    service = S.OrchestrationService()
    service.set_store_root(str(tmp_path / "orchestration"))
    service.set_child_runner(_instant_runner)
    return service


def _instant_runner(**kwargs):
    """A child that finishes immediately."""
    return AgentRunResult(
        run_id=kwargs["run_id"], status=STATUS_DONE,
        stop_reason=STOP_COMPLETED, summary="done by " + str(kwargs["child_slug"]),
        steps_used=1, tokens_used={"in": 3, "out": 2},
    )


def _gated_runner(gate: threading.Event, started: threading.Event):
    """Build a child runner that blocks until *gate* is set.

    ``started`` is set as soon as the child is inside the runner, so a test can
    wait for the exact moment the run is observable as ``running`` instead of
    sleeping and hoping.
    """
    def _runner(**kwargs):
        started.set()
        gate.wait(timeout=10)
        return AgentRunResult(
            run_id=kwargs["run_id"], status=STATUS_DONE,
            stop_reason=STOP_COMPLETED, summary="finished after the gate",
            steps_used=2,
        )
    return _runner


def _start(service, *, parent_run_id="root", thread_id="thread-1",
           workspace="", config=None):
    """Call ``service.start`` for a child of an existing root run."""
    return service.start(
        SpawnRequest(agent_slug="dev_agent", request=AgentBrief(goal="do it"),
                     thread_id=thread_id, parent_run_id=parent_run_id,
                     background=True, workspace=workspace),
        parent_config=config, base_dir=workspace or None, lang="English")


def _wait_until(predicate, timeout=10.0, interval=0.01):
    """Poll *predicate* until it is true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ─── start returns at once ─────────────────────────────────────────────────


def test_start_returns_a_running_run_immediately(svc):
    """The launch does not wait for the child: it hands back a live run id."""
    gate = threading.Event()
    started = threading.Event()
    svc.set_child_runner(_gated_runner(gate, started))
    svc.ensure_run("root", thread_id="thread-1")

    launched = _start(svc)

    try:
        assert launched["ok"] is True
        assert launched["status"] == STATUS_RUNNING
        assert launched["background"] is True
        assert launched["run_id"]
        assert launched["child_thread_id"].startswith("thread-1__s")
        assert started.wait(timeout=5) is True
    finally:
        gate.set()


def test_start_persists_the_record_before_the_worker_runs(svc):
    """A launch that crashes the process must leave a readable record.

    This is what makes ``runs.heal`` possible: the record exists from the
    moment ``start`` returns, so a dead ``running`` claim can be repaired
    later instead of being invisible.
    """
    gate = threading.Event()
    started = threading.Event()
    svc.set_child_runner(_gated_runner(gate, started))
    svc.ensure_run("root", thread_id="thread-1")

    launched = _start(svc, workspace="/tmp/project")
    try:
        stored = R.load(launched["run_id"], svc.store_root())
        assert stored is not None
        assert stored["status"] == STATUS_RUNNING
        assert stored["background"] is True
        assert stored["workspace"] == "/tmp/project"
        assert stored["root_run_id"] == "root"
    finally:
        gate.set()


def test_start_refuses_synchronously_when_the_pool_is_full(svc):
    """A background launch never accepts work it cannot start right now."""
    config = {"max_active_subagents": 1, "subagent_timeout_seconds": 5}
    svc.ensure_run("root", thread_id="thread-1", config=config)
    held = svc.acquire_pool("root")
    assert bool(held) is True

    try:
        refused = _start(svc, config=config)
    finally:
        svc.release_pool("root", held)

    assert refused["ok"] is False
    assert refused["error_code"] == ERR_LIMIT_EXCEEDED
    assert refused["limit"] == "max_active_subagents"
    assert "run_id" not in refused


def test_start_refuses_without_a_root_identity(svc):
    """No root means no limits; that is refused instead of silently allowed."""
    refused = svc.start(SpawnRequest(agent_slug="dev_agent",
                                     request=AgentBrief(goal="do it"),
                                     background=True))

    assert refused["ok"] is False
    assert refused["error_code"] == ERR_NO_RUN_IDENTITY


# ─── poll, wait and the durable fallback ───────────────────────────────────


def test_poll_reaches_done_after_the_worker_finishes(svc):
    """poll sees the transition running -> done without a second launch."""
    svc.ensure_run("root", thread_id="thread-1")

    launched = _start(svc)
    assert launched["ok"] is True

    done = _wait_until(lambda: svc.poll(launched["run_id"])["run"]["status"] == STATUS_DONE)
    assert done is True
    final = svc.poll(launched["run_id"])["run"]
    assert final["summary"] == "finished after the gate" or final["summary"] == "done by dev_agent"
    assert final["steps_used"] >= 1


def test_poll_unknown_run_reports_run_not_found(svc):
    """An unknown id is a documented refusal, not an exception."""
    out = svc.poll("nope")
    assert out["ok"] is False
    assert out["error_code"] == ERR_RUN_NOT_FOUND


def test_poll_falls_back_to_the_durable_store(svc):
    """A record dropped from memory is still answered from disk.

    This is the case a released tree creates: the in-memory registry is empty
    while the background child is alive and its record is on disk.
    """
    svc.ensure_run("root", thread_id="thread-1")
    gate = threading.Event()
    started = threading.Event()
    svc.set_child_runner(_gated_runner(gate, started))

    launched = _start(svc)
    run_id = launched["run_id"]
    try:
        assert started.wait(timeout=5) is True
        # Simulate the registry losing the record (release / restart).
        with svc._lock:  # noqa: SLF001 - the test deliberately reaches inside
            svc._runs.pop(run_id, None)

        out = svc.poll(run_id)
        assert out["ok"] is True
        assert out["run"]["live"] is False
        assert out["run"]["status"] == STATUS_RUNNING
    finally:
        gate.set()


def test_wait_returns_results_in_the_order_of_the_ids(svc):
    """Positions are preserved, so a caller can map results to its own ids."""
    svc.ensure_run("root", thread_id="thread-1")
    first = _start(svc)["run_id"]
    second = _start(svc)["run_id"]

    out = svc.wait([second, first], timeout=10)

    assert [item["run"]["run_id"] for item in out] == [second, first]
    assert all(item["run"]["status"] == STATUS_DONE for item in out)


def test_wait_marks_a_still_running_run_as_timed_out(svc):
    """A timeout is reported, not raised: the caller keeps the live handle."""
    gate = threading.Event()
    started = threading.Event()
    svc.set_child_runner(_gated_runner(gate, started))
    svc.ensure_run("root", thread_id="thread-1")

    launched = _start(svc)
    try:
        assert started.wait(timeout=5) is True
        out = svc.wait([launched["run_id"]], timeout=0.05)
        assert out[0]["run"]["status"] == STATUS_RUNNING
        assert out[0]["run"]["timed_out"] is True
    finally:
        gate.set()


# ─── mailbox and exactly-once notices ─────────────────────────────────────


def test_the_worker_posts_exactly_one_notice(svc):
    """A finished background run announces itself once, into its own tree."""
    svc.ensure_run("root", thread_id="thread-1")

    launched = _start(svc)
    assert _wait_until(lambda: svc.has_notifications("root")) is True

    notices = svc.take_notifications("root")
    assert len(notices) == 1
    assert notices[0]["type"] == "run_finished"
    assert notices[0]["run_id"] == launched["run_id"]
    assert notices[0]["status"] == STATUS_DONE
    # Drained: a second read is empty, which is what makes delivery once-only.
    assert svc.take_notifications("root") == []


def test_a_claimed_notice_is_not_published_twice(svc):
    """The O_EXCL claim is what guarantees one notice, not the message text."""
    svc.ensure_run("root", thread_id="thread-1")
    launched = _start(svc)
    assert _wait_until(lambda: svc.has_notifications("root")) is True
    svc.take_notifications("root")

    # A second writer (the UI, or a retried worker) must be refused.
    assert R.claim_notice(launched["run_id"], STATUS_DONE, svc.store_root()) is False


def test_notices_do_not_leak_between_trees(svc):
    """A notice belongs to the dialog that owns the child."""
    svc.ensure_run("root", thread_id="thread-1")
    svc.ensure_run("other", thread_id="thread-2")

    _start(svc)
    assert _wait_until(lambda: svc.has_notifications("root")) is True

    assert svc.take_notifications("other") == []


# ─── deferred release (the child outlives the step) ───────────────────────


def test_release_is_deferred_and_performed_by_the_worker(svc):
    """The dialog finishes first; the tree is dropped by the LAST finish."""
    gate = threading.Event()
    started = threading.Event()
    svc.set_child_runner(_gated_runner(gate, started))
    svc.ensure_run("root", thread_id="thread-1")

    launched = _start(svc)
    run_id = launched["run_id"]
    assert started.wait(timeout=5) is True

    deferred = svc.release_tree("root")
    assert deferred == {"released": False, "deferred": True}
    assert svc.get_record(run_id) is not None

    gate.set()
    assert _wait_until(lambda: svc.get_record(run_id) is None) is True


def test_a_released_tree_is_still_cancellable_while_running(svc):
    """A deferred release must not cost the dialog its cancel button."""
    gate = threading.Event()
    started = threading.Event()
    svc.set_child_runner(_gated_runner(gate, started))
    svc.ensure_run("root", thread_id="thread-1")

    launched = _start(svc)
    run_id = launched["run_id"]
    try:
        assert started.wait(timeout=5) is True
        svc.release_tree("root")

        report = svc.cancel(run_id)
        assert report.cancelled is True
        assert svc.is_cancelled(run_id) is True
    finally:
        gate.set()


# ─── compare-and-set cancellation ─────────────────────────────────────────


def test_cancel_during_finish_is_not_overwritten_by_done(svc):
    """The child's own ``done`` must not erase a cancellation that beat it.

    The loop already passed its ``is_cancelled`` check when the child returns,
    so without the compare-and-set in ``finish_run`` the unconditional state
    assignment would publish ``done`` for a run the dialog already cancelled.
    """
    gate = threading.Event()
    started = threading.Event()
    svc.set_child_runner(_gated_runner(gate, started))
    svc.ensure_run("root", thread_id="thread-1")

    launched = _start(svc)
    run_id = launched["run_id"]
    assert started.wait(timeout=5) is True

    svc.cancel(run_id)
    gate.set()

    # Wait for the WORKER to finish: right after cancel the record is already
    # cancelled, so a plain status check would pass before ``finish_run`` ever
    # ran and would never exercise the compare-and-set. The notice is posted
    # strictly after ``finish_run``, so it is the exact signal needed here.
    assert _wait_until(lambda: svc.has_notifications("root")) is True
    assert _wait_until(
        lambda: (svc.poll(run_id).get("run") or {}).get("status") == STATUS_CANCELLED
    ) is True
    final = svc.poll(run_id)["run"]
    assert final["stop_reason"] == STOP_ABORTED
    assert final["error_code"] == "cancelled_by_parent"


def test_finish_run_returns_the_effective_result(svc):
    """The caller publishes what was STORED, not what the child claimed."""
    svc.ensure_run("root")
    svc.ensure_run("child", parent_run_id="root", depth=1)
    svc.cancel("child")

    effective = svc.finish_run("child", AgentRunResult(
        run_id="child", status=STATUS_DONE, stop_reason=STOP_COMPLETED,
        summary="I finished anyway"))

    assert effective.status == STATUS_CANCELLED
    assert svc.get_record("child").status == STATUS_CANCELLED


# ─── listing ──────────────────────────────────────────────────────────────


def test_list_runs_reports_live_and_durable_runs_of_one_tree(svc):
    """The list is the dialog's view: its own children, live or stored."""
    svc.ensure_run("root", thread_id="thread-1")
    first = _start(svc)["run_id"]
    assert svc.wait([first], timeout=10)[0]["run"]["status"] == STATUS_DONE

    runs = svc.list_runs("root")
    ids = [r["run_id"] for r in runs]

    assert first in ids
    assert all(r.get("root_run_id") == "root" for r in runs)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
