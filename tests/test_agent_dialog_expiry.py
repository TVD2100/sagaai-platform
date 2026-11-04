# -*- coding: utf-8 -*-
"""Tests for cleaning up waiting runs: expiry, cancellation and orphans.

A parked run never finishes on its own, so every path that reaches one has to
know what "nobody is going to answer this" means. These tests pin the verdict
(``failed`` / ``answer_not_received``), its idempotence, and the fact that a
late reply cannot revive a run that was already reported as finished.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestration import contracts, runs  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentQuestion,
    AgentRunResult,
    SpawnRequest,
)
from core.orchestration.service import OrchestrationService, read_limits  # noqa: E402


CONFIG = {
    "max_depth": 1,
    "max_active_subagents": 2,
    "subagent_max_steps": 50,
    "subagent_timeout_seconds": 60,
    "total_subagent_runs": 20,
    "dialog_rounds": 5,
    "answer_wait_seconds": 60,
    "max_parked_runs": 2,
}


@pytest.fixture()
def service(tmp_path):
    """A service with a private store and a scripted child runner."""
    svc = OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))
    state = {"calls": [], "script": []}

    def runner(**kwargs):
        state["calls"].append(kwargs)
        if not state["script"]:
            return AgentRunResult(run_id=kwargs["run_id"], status="done",
                                  stop_reason="completed", summary="finished")
        return state["script"].pop(0)(kwargs)

    svc.set_child_runner(runner)
    svc.state = state
    return svc


def _spawn(service):
    req = SpawnRequest(agent_slug="dev_agent",
                       request=AgentBrief(goal="do work"),
                       thread_id="thread-root",
                       parent_run_id="thread-root",
                       workspace="")
    return service.run(req, parent_config=CONFIG)


def _park(service, deadline_in=None):
    """Spawn a run that parks itself, with a chosen deadline."""
    def step(kwargs):
        question = AgentQuestion(text="Which format?")
        parked = service.park_run(kwargs["run_id"], question,
                                  limits=read_limits(CONFIG))
        assert parked["ok"] is True
        rec = service.get_record(kwargs["run_id"])
        if deadline_in is not None:
            # The question is a frozen value object: an adjusted deadline is
            # a NEW question, not a mutated field.
            rec.question = replace(rec.question,
                                   deadline_at=time.time() + deadline_in)
            question = rec.question
        return AgentRunResult(run_id=kwargs["run_id"],
                              status=contracts.STATUS_AWAITING_INPUT,
                              await_reason=contracts.AWAIT_QUESTION,
                              question=question)

    service.state["script"] = [step]
    payload = _spawn(service)
    return payload["result"]["run_id"]


def test_an_expired_question_finishes_as_answer_not_received(service):
    """The run must not wait forever for an answer nobody will give."""
    run_id = _park(service, deadline_in=-1)

    outcome = service.expire_parked(run_id)

    assert outcome["expired"] is True
    rec = service.get_record(run_id)
    assert rec.status == contracts.STATUS_FAILED
    assert rec.error_code == contracts.ERR_ANSWER_NOT_RECEIVED
    assert rec.is_parked is False
    assert "Which format?" in rec.summary


def test_a_live_wait_is_not_expired(service):
    """The sweep must not cut a question that still has time on the clock."""
    run_id = _park(service, deadline_in=300)

    outcome = service.expire_parked(run_id)

    assert outcome["expired"] is False
    assert service.get_record(run_id).is_parked is True


def test_expiry_is_idempotent(service):
    """Running the sweep twice must not rewrite a finished run."""
    run_id = _park(service, deadline_in=-1)
    service.expire_parked(run_id)

    again = service.expire_parked(run_id)

    assert again["expired"] is False


def test_an_answer_after_the_deadline_is_refused(service):
    """A late reply must not revive a run the reader already sees as failed."""
    run_id = _park(service, deadline_in=-1)

    reply = service.answer_run(run_id, "markdown", limits=read_limits(CONFIG))

    assert reply["ok"] is False
    assert reply["error_code"] == contracts.ERR_ANSWER_NOT_RECEIVED
    rec = service.get_record(run_id)
    assert rec.status == contracts.STATUS_FAILED
    assert rec.is_parked is False


def test_the_sweep_reaches_every_waiting_run_of_a_tree(service):
    """One abandoned question per child: a tree-wide sweep clears them all."""
    first = _park(service, deadline_in=-1)
    rec = service.get_record(first)
    second = service.ensure_run("run-second", thread_id="t2",
                                parent_run_id="thread-root")
    second.root_run_id = rec.root_run_id
    second.status = contracts.STATUS_AWAITING_INPUT
    second.await_reason = contracts.AWAIT_QUESTION
    second.question = AgentQuestion(text="and this one?",
                                    deadline_at=time.time() - 5)

    expired = service.expire_parked_in(rec.root_run_id)

    assert set(expired) == {first, "run-second"}
    assert service.parked_runs(rec.root_run_id) == []


def test_the_parked_list_never_reports_an_answerable_wait(service):
    """The read path sweeps: a caller cannot see a question past its window."""
    run_id = _park(service, deadline_in=-1)
    root = service.get_record(run_id).root_run_id

    assert service.parked_runs(root) == []


def test_a_stored_expired_question_reads_as_failed():
    """A record from a previous process must not claim a live wait."""
    record = {
        "run_id": "run-1", "status": contracts.STATUS_AWAITING_INPUT,
        "await_reason": contracts.AWAIT_QUESTION,
        "question": {"text": "Which format?", "deadline_at": time.time() - 30},
    }

    expired = runs.expire_unanswered(record)

    assert expired["status"] == contracts.STATUS_FAILED
    assert expired["error_code"] == contracts.ERR_ANSWER_NOT_RECEIVED
    assert expired["await_reason"] == ""
    # The question itself stays: it explains WHY the run failed.
    assert expired["question"]["text"] == "Which format?"


def test_a_stored_record_without_a_deadline_is_left_alone():
    """No deadline means an older record; guessing a verdict would be wrong."""
    record = {
        "run_id": "run-2", "status": contracts.STATUS_AWAITING_INPUT,
        "await_reason": contracts.AWAIT_QUESTION,
        "question": {"text": "Which format?"},
    }

    assert runs.expire_unanswered(record) is record


def test_an_answered_question_is_never_expired():
    """A reply recorded in time keeps its meaning however late it is read."""
    record = {
        "run_id": "run-3", "status": contracts.STATUS_AWAITING_INPUT,
        "await_reason": contracts.AWAIT_QUESTION,
        "question": {"text": "Which format?", "deadline_at": time.time() - 30,
                     "answer": "markdown"},
    }

    assert runs.expire_unanswered(record) is record


def test_expiry_is_reachable_through_the_demotion_path():
    """Every reader goes through one function, so the verdict is not bypassed."""
    record = {
        "run_id": "run-4", "status": contracts.STATUS_AWAITING_INPUT,
        "await_reason": contracts.AWAIT_QUESTION,
        "question": {"text": "Which format?", "deadline_at": time.time() - 1},
    }

    demoted = runs.demote_if_dead(record)

    assert demoted["error_code"] == contracts.ERR_ANSWER_NOT_RECEIVED


def test_cancelling_an_expired_run_still_reports_the_terminal_state(service):
    """Cancelling a finished run is a no-op, not a resurrection."""
    run_id = _park(service, deadline_in=-1)
    service.expire_parked(run_id)

    report = service.cancel(run_id)

    assert report.cancelled is False
    assert report.reason == "already_terminal"


def test_an_expired_run_does_not_hold_its_tree_open(service):
    """An abandoned question must not keep the whole tree alive forever."""
    run_id = _park(service, deadline_in=-1)
    root = service.get_record(run_id).root_run_id
    service.expire_parked(run_id)
    service.close_root_run(root)

    outcome = service.release_tree(root)

    assert outcome["released"] is True
