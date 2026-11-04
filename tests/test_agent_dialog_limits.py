# -*- coding: utf-8 -*-
"""Tests for the dialog limits: defaults, clamps, and the frozen budget.

A limit is only a limit if it holds against the configuration of whichever
process answers. These tests change the answering config in BOTH directions -
looser and stricter - and check that the run keeps the budget it was spawned
with instead of inheriting the settings of its answerer.
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


BASE = {
    "max_depth": 1,
    "max_active_subagents": 1,
    "subagent_max_steps": 50,
    "subagent_timeout_seconds": 60,
    "total_subagent_runs": 20,
    "dialog_rounds": 2,
    "answer_wait_seconds": 60,
    "max_parked_runs": 2,
}

#: Stands for "a dialog with different settings answered this run".
LOOSE = {"dialog_rounds": 9, "question_max_chars": 5000,
         "answer_max_chars": 5000, "answer_wait_seconds": 120,
         "max_parked_runs": 9}


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


def _spawn(service, config=None):
    req = SpawnRequest(agent_slug="dev_agent",
                       request=AgentBrief(goal="do work"),
                       thread_id="thread-root",
                       parent_run_id="thread-root",
                       workspace="")
    return service.run(req, parent_config=config or BASE)


def _park_with(service, config):
    """Return a runner step that parks the run on a question."""
    def step(kwargs):
        question = AgentQuestion(text="Which format?")
        parked = service.park_run(kwargs["run_id"], question,
                                  limits=read_limits(config))
        assert parked["ok"] is True
        return AgentRunResult(run_id=kwargs["run_id"],
                              status=contracts.STATUS_AWAITING_INPUT,
                              await_reason=contracts.AWAIT_QUESTION,
                              question=question)
    return step


def test_defaults_match_the_documented_values():
    """Five rounds by default - the value the owner raised from three."""
    limits = read_limits({})

    assert limits["dialog_rounds"] == contracts.DEFAULT_DIALOG_ROUNDS == 5
    assert limits["question_max_chars"] == contracts.DEFAULT_QUESTION_MAX_CHARS
    assert limits["answer_max_chars"] == contracts.DEFAULT_ANSWER_MAX_CHARS
    assert limits["answer_wait_seconds"] == contracts.DEFAULT_ANSWER_WAIT_SECONDS
    assert limits["max_parked_runs"] == contracts.DEFAULT_MAX_PARKED_RUNS


def test_a_zero_never_removes_a_limit():
    """Zero rounds must degrade to one, not to an unlimited dialog."""
    limits = read_limits({"dialog_rounds": 0, "question_max_chars": 0,
                          "answer_max_chars": 0, "answer_wait_seconds": 0,
                          "max_parked_runs": 0})

    assert limits["dialog_rounds"] == 1
    assert limits["question_max_chars"] == contracts.DEFAULT_QUESTION_MAX_CHARS
    assert limits["answer_max_chars"] == contracts.DEFAULT_ANSWER_MAX_CHARS
    assert limits["answer_wait_seconds"] == contracts.DEFAULT_ANSWER_WAIT_SECONDS
    assert limits["max_parked_runs"] == contracts.DEFAULT_MAX_PARKED_RUNS


def test_the_wait_is_clamped_to_its_ceiling():
    """A hand-written wait cannot outlive the run budget it mirrors."""
    limits = read_limits({"answer_wait_seconds": 999999})

    assert limits["answer_wait_seconds"] == contracts.MAX_ANSWER_WAIT_SECONDS


def test_a_negative_rounds_value_cannot_invert_the_budget():
    """A negative value must not turn the refusal into an always-pass."""
    limits = read_limits({"dialog_rounds": -5})

    assert limits["dialog_rounds"] == 1


def test_the_spawned_run_freezes_its_dialog_budget(service):
    """The record carries the budget, so another process can read it."""
    service.state["script"] = [_park_with(service, BASE)]

    payload = _spawn(service)

    rec = service.get_record(payload["result"]["run_id"])
    assert rec.dialog_limits["dialog_rounds"] == 2
    assert rec.dialog_limits["max_parked_runs"] == 2
    stored = service.record_from_store(rec.run_id)
    assert stored["dialog_limits"]["dialog_rounds"] == 2


def test_a_looser_answering_config_cannot_extend_the_budget(service):
    """The child's own rounds decide, not the settings of its answerer."""
    service.state["script"] = [_park_with(service, BASE)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]

    # The spawn question spent round 1; round 2 is the last one allowed.
    answer = service.answer_run(run_id, "go on", limits=read_limits(LOOSE))
    assert answer["ok"] is True
    parked = service.park_run(run_id, AgentQuestion(text="again?"),
                             limits=read_limits(LOOSE))
    assert parked["ok"] is True

    # Round 3 would be allowed by the answerer's config but not by the run's.
    service.answer_run(run_id, "go on", limits=read_limits(LOOSE))
    refused = service.park_run(run_id, AgentQuestion(text="one more?"),
                              limits=read_limits(LOOSE))
    assert refused["ok"] is False
    assert refused["error_code"] == contracts.ERR_QUESTION_LIMIT
    assert refused["value"] == 2


def test_a_stricter_answering_config_cannot_cut_the_budget(service):
    """The opposite direction: a cramped answerer must not stop round two."""
    loose_spawn = dict(BASE, dialog_rounds=5)
    service.state["script"] = [_park_with(service, loose_spawn)]
    payload = _spawn(service, config=loose_spawn)
    run_id = payload["result"]["run_id"]
    strict = read_limits(dict(BASE, dialog_rounds=1))

    parked = service.park_run(run_id, AgentQuestion(text="and now?"),
                             limits=strict)

    assert parked["ok"] is True
    assert service.get_record(run_id).dialog_rounds == 2


def test_the_continue_reads_the_frozen_budget(service):
    """continue_run passes the frozen limits down to the runner."""
    service.state["script"] = [_park_with(service, BASE)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]

    service.continue_run(run_id, "markdown", parent_config=LOOSE)

    used = service.state["calls"][-1]["limits"]
    assert used["dialog_rounds"] == 2
    assert used["answer_max_chars"] == read_limits(BASE)["answer_max_chars"]


def test_concurrency_stays_live_while_the_dialog_is_frozen(service):
    """Pool size belongs to the tree doing the work, so it is NOT frozen."""
    service.state["script"] = [_park_with(service, BASE)]
    payload = _spawn(service)
    run_id = payload["result"]["run_id"]

    service.continue_run(run_id, "markdown",
                         parent_config=dict(LOOSE, max_active_subagents=4))

    used = service.state["calls"][-1]["limits"]
    assert used["max_active_subagents"] == 4
    assert used["dialog_rounds"] == 2
