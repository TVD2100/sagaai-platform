# -*- coding: utf-8 -*-
"""Tests for the agent dialog: park a run on a question, answer it, continue.

They exercise the service directly - the same calls the two tools make - so a
regression in the park/unpack machinery fails here before it reaches a model.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestration import contracts  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentQuestion,
    AgentRunResult,
    SpawnRequest,
)
from core.orchestration.service import OrchestrationService, read_limits  # noqa: E402

CONFIG = {
    "max_depth": 1,
    "max_active_subagents": 1,
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
        step = state["script"].pop(0)
        return step(kwargs)

    svc.set_child_runner(runner)
    svc.state = state
    return svc


def _spawn(service, goal="do work"):
    req = SpawnRequest(agent_slug="dev_agent",
                       request=AgentBrief(goal=goal),
                       thread_id="thread-root",
                       parent_run_id="thread-root",
                       workspace="")
    return service.run(req, parent_config=CONFIG)


def _ask(service, kwargs, text="Which format?"):
    """Park the run and return the parked result, as the tool pair does."""
    question = AgentQuestion(text=text, done="read files",
                             blocked="cannot pick a format")
    parked = service.park_run(kwargs["run_id"], question,
                             limits=read_limits(CONFIG))
    assert parked["ok"] is True
    return AgentRunResult(run_id=kwargs["run_id"], status="awaiting_input",
                          await_reason=contracts.AWAIT_QUESTION,
                          question=question)


def test_defaults_are_the_documented_values():
    """Five rounds by default - the value the owner raised from three."""
    limits = read_limits({})
    assert limits["dialog_rounds"] == contracts.DEFAULT_DIALOG_ROUNDS == 5
    assert limits["max_parked_runs"] == contracts.DEFAULT_MAX_PARKED_RUNS


def test_a_broken_dialog_config_cannot_remove_the_limits():
    """Zero rounds or a zero wait must degrade to a limit, not to no limit."""
    limits = read_limits({"dialog_rounds": 0, "answer_wait_seconds": 0,
                          "question_max_chars": 0, "max_parked_runs": 0})
    assert limits["dialog_rounds"] == 1
    assert limits["answer_wait_seconds"] == contracts.DEFAULT_ANSWER_WAIT_SECONDS
    assert limits["question_max_chars"] == contracts.DEFAULT_QUESTION_MAX_CHARS
    assert limits["max_parked_runs"] == contracts.DEFAULT_MAX_PARKED_RUNS


def test_parking_a_run_keeps_it_alive_and_records_the_question(service):
    """A parked run is not finished: status, reason and question are recorded."""
    service.state["script"] = [lambda kw: _ask(service, kw)]
    payload = _spawn(service)
    result = payload["result"]
    assert result["status"] == "awaiting_input"
    assert result["await_reason"] == "question"
    assert result["question"]["text"] == "Which format?"

    rec = service.get_record(result["run_id"])
    assert rec.is_parked is True
    assert rec.dialog_rounds == 1
    assert [r.run_id for r in service.parked_runs(rec.root_run_id)] == [rec.run_id]


def test_answering_continues_the_same_run(service):
    """The answer continues the SAME run: same id, and the runner is told so."""
    service.state["script"] = [lambda kw: _ask(service, kw)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]

    def finish(kwargs):
        assert kwargs.get("prior_run") is True
        assert kwargs.get("answer_text") == "markdown"
        return AgentRunResult(run_id=kwargs["run_id"], status="done",
                              stop_reason="completed", summary="done with markdown")

    service.state["script"] = [finish]
    rec = service.get_record(run_id)
    continued = service.continue_run(run_id, "markdown", parent_config=CONFIG,
                                     caller_root_run_id=rec.root_run_id)
    assert continued["ok"] is True
    assert continued["result"]["run_id"] == run_id
    assert continued["result"]["status"] == "done"


def test_the_dialog_budget_is_spent_by_questions_not_answers(service):
    """A run may ask five times; the sixth question is refused, not parked."""
    service.state["script"] = [lambda kw: _ask(service, kw)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]
    rec = service.get_record(run_id)
    limits = read_limits(CONFIG)
    for _ in range(1, limits["dialog_rounds"]):
        answer = service.answer_run(run_id, "go on", limits=limits,
                                    caller_root_run_id=rec.root_run_id)
        assert answer["ok"] is True
        parked = service.park_run(run_id, AgentQuestion(text="again?"),
                                 limits=limits)
        assert parked["ok"] is True
    rec = service.get_record(run_id)
    assert rec.dialog_rounds == limits["dialog_rounds"]
    refused = service.park_run(run_id, AgentQuestion(text="one more?"),
                              limits=limits)
    assert refused["ok"] is False
    assert refused["error_code"] == contracts.ERR_QUESTION_LIMIT
    assert service.get_record(run_id).is_parked is True


def test_an_empty_or_oversized_answer_is_refused(service):
    """The answer has to be an answer: not empty, not a new briefing."""
    service.state["script"] = [lambda kw: _ask(service, kw)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]
    rec = service.get_record(run_id)
    empty = service.answer_run(run_id, "   ", limits=read_limits(CONFIG),
                               caller_root_run_id=rec.root_run_id)
    assert empty["ok"] is False
    assert empty["error_code"] == contracts.ERR_INVALID_ARGS
    long_text = "x" * (contracts.DEFAULT_ANSWER_MAX_CHARS + 10)
    too_long = service.answer_run(run_id, long_text, limits=read_limits(CONFIG),
                                  caller_root_run_id=rec.root_run_id)
    assert too_long["ok"] is False
    assert too_long["limit"] == "answer_max_chars"
    assert service.get_record(run_id).is_parked is True


def test_a_foreign_tree_and_a_working_run_are_refused(service):
    """Only a parked run of the CALLER's tree can be answered."""
    service.state["script"] = [lambda kw: _ask(service, kw)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]
    foreign = service.answer_run(run_id, "sure", limits=read_limits(CONFIG),
                                 caller_root_run_id="another-tree")
    assert foreign["ok"] is False
    assert foreign["error_code"] == contracts.ERR_NOT_YOUR_RUN

    rec = service.get_record(run_id)
    rec.status = "running"
    rec.await_reason = ""
    working = service.answer_run(run_id, "sure", limits=read_limits(CONFIG),
                                 caller_root_run_id=rec.root_run_id)
    assert working["ok"] is False
    assert working["error_code"] == contracts.ERR_NOTHING_TO_ANSWER


def test_too_many_parked_siblings_are_refused(service):
    """A waiting run is cheap, but a warehouse of them holds the tree open."""
    first = service.ensure_run("run-a", thread_id="a", parent_run_id="thread-root")
    second = service.ensure_run("run-b", thread_id="b", parent_run_id="thread-root")
    third = service.ensure_run("run-c", thread_id="c", parent_run_id="thread-root")
    limits = read_limits(CONFIG)
    for rec in (first, second):
        rec.root_run_id = "thread-root"
        assert service.park_run(rec.run_id, AgentQuestion(text="q"),
                                limits=limits)["ok"] is True
    third.root_run_id = "thread-root"
    refused = service.park_run(third.run_id, AgentQuestion(text="q"), limits=limits)
    assert refused["ok"] is False
    assert refused["error_code"] == contracts.ERR_TOO_MANY_PARKED


def test_cancelling_a_tree_reaches_a_parked_run(service):
    """A parked run is alive for cancellation; a dropped one would wait forever."""
    service.state["script"] = [lambda kw: _ask(service, kw)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]
    report = service.cancel(run_id)
    assert report.cancelled is True
    rec = service.get_record(run_id)
    assert rec.is_parked is False
    assert rec.status == "cancelled"
    assert rec.cancel_requested is True


def test_a_parked_run_keeps_the_tree_from_being_released(service):
    """Releasing the tree would delete the only copy of an answerable question."""
    service.state["script"] = [lambda kw: _ask(service, kw)]
    payload = _spawn(service)
    rec = service.get_record(payload["result"]["run_id"])
    service.close_root_run(rec.root_run_id)
    outcome = service.release_tree(rec.root_run_id)
    assert outcome["released"] is False
    assert outcome["deferred"] is True
    assert service.get_record(rec.run_id) is not None


def test_the_question_survives_in_the_durable_record(service):
    """The record carries the question, so it is readable after a restart."""
    service.state["script"] = [lambda kw: _ask(service, kw)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]
    stored = service.record_from_store(run_id)
    assert stored is not None
    assert stored["await_reason"] == contracts.AWAIT_QUESTION
    assert stored["question"]["text"] == "Which format?"
    assert stored["dialog_rounds"] == 1
    assert stored["status"] == "awaiting_input"
