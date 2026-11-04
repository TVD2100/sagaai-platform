# -*- coding: utf-8 -*-
"""tests/test_orchestration_verify.py - the independent verifier of a result.

The verifier exists because a fan-out reports results but never checks them.
What is pinned here is therefore not "a verifier can be called" but the
boundaries that make it a CHECK instead of a second opinion from the same
work:

- the refusal order (empty id, no verifier, unknown run, unfinished run, a
  verifier run) and which refusals are errors versus answers;
- the verdict rules, including the one that outranks the rest - ``passed``
  next to objections is ``failed``;
- what the verifier is allowed to see: the RESULT, never the transcript;
- the limits: a verifier is a child like any other and is refused the same way;
- the catalogue and the prompt block, which decide whether the model even
  learns the tool exists.

The service is a double in the boundary tests (so a test shows WHICH calls the
tool makes) and the real ``OrchestrationService`` with a fake child runner in
the run-level tests (so the thread, the journal and the limits are real).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestration import tools as T  # noqa: E402
from core.orchestration import verify as V  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    ERR_INVALID_ARGS,
    ERR_LIMIT_EXCEEDED,
    ERR_RUN_NOT_FINISHED,
    ERR_RUN_NOT_FOUND,
    ERR_VERIFIER_NOT_CONFIGURED,
    STATUS_DONE,
    STATUS_RUNNING,
    STOP_COMPLETED,
    AgentRunResult,
)
from core.orchestration.service import OrchestrationService  # noqa: E402
from storage.db import reset_devagent_engine, reset_engine  # noqa: E402

SLUG = "verify_orch"
VERIFIER = "verify_checker"


def _answer(verdict="passed", reasons=None, checked=None) -> str:
    """Return a verifier answer in the documented shape."""
    payload = {"verdict": verdict,
               "reasons": list(reasons or []),
               "checked": list(checked or ["the artifact"])}
    return "Checked it myself.\n" + json.dumps(payload)


class FakeRecord:
    """A run record with the fields the tools read (plus anything extra)."""

    def __init__(self, **data):
        self._data = dict(data)

    def to_record(self):
        return dict(self._data)


class FakeService:
    """A service double that records every call it receives."""

    def __init__(self, *, record=None, stored=None, run_result=None):
        self.calls = []
        self.spawned = []
        self._record = record
        self._stored = stored
        self.run_result = run_result

    def get_record(self, run_id):
        self.calls.append(("get_record", run_id))
        return self._record

    def record_from_store(self, run_id):
        self.calls.append(("record_from_store", run_id))
        return dict(self._stored) if self._stored is not None else None

    def run(self, req, **kwargs):
        self.calls.append(("run", req, kwargs))
        self.spawned.append(req)
        if self.run_result is not None:
            return self.run_result
        return {"ok": True, "result": {"run_id": "verifier1",
                                       "status": STATUS_DONE,
                                       "summary": _answer()}}


@pytest.fixture
def verifier_appointed(monkeypatch):
    """Point the orchestrator at a verifier without touching the database."""
    import core.orchestrators as orch_mod

    monkeypatch.setattr(orch_mod, "get_verifier_agent",
                        lambda slug="dev_agent": VERIFIER)
    return VERIFIER


@pytest.fixture
def no_verifier(monkeypatch):
    """An orchestrator with nobody appointed to check results."""
    import core.orchestrators as orch_mod

    monkeypatch.setattr(orch_mod, "get_verifier_agent",
                        lambda slug="dev_agent": "")
    return ""


def _finished(status=STATUS_DONE, **extra):
    """A finished target run record."""
    data = {"run_id": "target1", "status": status, "agent_slug": "worker",
            "summary": "wrote report.md", "thread_id": "parent-thread",
            "goal": "write the report", "artifacts": [],
            "steps_used": 3, "tokens_used": {"in": 10, "out": 5}}
    data.update(extra)
    return data


# ─── refusals (tests 1-5) ──────────────────────────────────────────────────


def test_no_verifier_is_refused_instead_of_substituted(no_verifier):
    """Test 1: nobody appointed is a refusal, never a silent default agent."""
    service = FakeService(record=FakeRecord(**_finished()))

    result = T.verify_result("target1", "must exist", service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_VERIFIER_NOT_CONFIGURED
    assert "verifier_agent" in result["error"]
    # Nothing was launched: a substitution would have started an agent here.
    assert service.spawned == []


def test_empty_run_id_is_invalid_args(verifier_appointed):
    """Test 2: the call must name the run it wants checked."""
    service = FakeService(record=FakeRecord(**_finished()))

    result = T.verify_result("", "criteria", service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_INVALID_ARGS
    assert result["missing"] == ["run_id"]
    assert service.spawned == []


def test_unknown_run_is_run_not_found(verifier_appointed):
    """Test 3: an id nobody knows answers the code ``poll`` already uses."""
    service = FakeService(record=None, stored=None)

    result = T.verify_result("ghost", "criteria", service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_RUN_NOT_FOUND
    assert service.spawned == []


def test_unfinished_run_is_refused_with_its_status(verifier_appointed):
    """Test 4: a running child has no result to judge yet."""
    service = FakeService(record=FakeRecord(**_finished(status=STATUS_RUNNING)))

    result = T.verify_result("target1", "criteria", service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_RUN_NOT_FINISHED
    assert result["status"] == STATUS_RUNNING
    assert service.spawned == []


def test_a_verifier_run_cannot_be_verified(verifier_appointed):
    """Test 5: checking a check is forbidden - no chain of verifications."""
    service = FakeService(record=FakeRecord(**_finished(agent_slug=VERIFIER)))

    result = T.verify_result("target1", "criteria", service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_INVALID_ARGS
    assert "verifier" in result["error"]
    assert service.spawned == []


def test_a_verifier_run_is_recognised_by_its_goal_too(verifier_appointed):
    """The marker outlives a re-pointed setting: the GOAL names the check."""
    record = FakeRecord(**_finished(agent_slug="worker",
                                    goal=V.VERIFIER_GOAL_PREFIX + "old against x."))
    service = FakeService(record=record)

    result = T.verify_result("target1", "criteria", service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_INVALID_ARGS
    assert service.spawned == []


# ─── verdicts (tests 6-10) ────────────────────────────────────────────────


def test_passed_without_reasons_is_passed(verifier_appointed):
    """Test 6: the bare success case."""
    service = FakeService(record=FakeRecord(**_finished()),
                          run_result={"ok": True, "result": {
                              "run_id": "verifier1", "status": STATUS_DONE,
                              "summary": _answer("passed", [], ["report.md"])}})

    result = T.verify_result("target1", "report exists", service=service)

    assert result["ok"] is True
    verdict = result["verdict"]
    assert verdict["verdict"] == "passed"
    assert verdict["reasons"] == []
    assert verdict["checked"] == ["report.md"]
    assert verdict["target_run_id"] == "target1"
    assert verdict["verifier_run_id"] == "verifier1"


def test_passed_with_reasons_becomes_failed(verifier_appointed):
    """Test 7: "all good" plus objections is a failure, not a pass.

    The rule exists so a verifier cannot bless a result and list its defects
    in the same answer; the objections are the part that carries information.
    """
    service = FakeService(record=FakeRecord(**_finished()),
                          run_result={"ok": True, "result": {
                              "run_id": "verifier1", "status": STATUS_DONE,
                              "summary": _answer("passed", ["report is empty"])}})

    result = T.verify_result("target1", "report is filled", service=service)

    assert result["ok"] is True
    assert result["verdict"]["verdict"] == "failed"
    assert any("report is empty" in reason for reason in result["verdict"]["reasons"])


def test_an_answer_without_json_is_inconclusive(verifier_appointed):
    """Test 8: free text cannot be acted on and must not read as success."""
    service = FakeService(record=FakeRecord(**_finished()),
                          run_result={"ok": True, "result": {
                              "run_id": "verifier1", "status": STATUS_DONE,
                              "summary": "Looks fine to me, honestly."}})

    result = T.verify_result("target1", "criteria", service=service)

    assert result["ok"] is True
    assert result["verdict"]["verdict"] == "inconclusive"
    assert result["verdict"]["reasons"]
    assert "parsable" in result["verdict"]["reasons"][0]


def test_an_unsupported_verdict_is_inconclusive(verifier_appointed):
    """Test 9: a fourth value is not a verdict, so nothing is concluded."""
    service = FakeService(record=FakeRecord(**_finished()),
                          run_result={"ok": True, "result": {
                              "run_id": "verifier1", "status": STATUS_DONE,
                              "summary": _answer("probably-fine")}})

    result = T.verify_result("target1", "criteria", service=service)

    assert result["ok"] is True
    assert result["verdict"]["verdict"] == "inconclusive"
    assert "probably-fine" in result["verdict"]["reasons"][0]


def test_a_failed_verifier_is_an_inconclusive_answer(verifier_appointed):
    """Test 10: the verifier's own failure is "could not check", not an error.

    The fan-out must survive a broken verifier: the parent needs a verdict it
    can act on, and ``ok: False`` here would look like a broken call.
    """
    service = FakeService(record=FakeRecord(**_finished()),
                          run_result={"ok": True, "result": {
                              "run_id": "verifier1", "status": "failed",
                              "error_code": "timeout", "summary": ""}})

    result = T.verify_result("target1", "criteria", service=service)

    assert result["ok"] is True
    assert result["verdict"]["verdict"] == "inconclusive"
    assert "timeout" in result["verdict"]["reasons"][0]
    assert result["verdict"]["verifier_run_id"] == "verifier1"


# ─── what the verifier sees (test 11) ─────────────────────────────────────


def _verifier_runner(captured, answer="passed"):
    """Fake child runner that records the brief it was given."""
    def _run(**kwargs):
        captured["brief"] = kwargs.get("brief")
        captured["child_thread_id"] = kwargs.get("child_thread_id")
        summary = (_answer(answer, [], ["report.md"])
                   if not captured.get("answer_raw") else captured["answer_raw"])
        return AgentRunResult(
            run_id=kwargs["run_id"], status=STATUS_DONE,
            stop_reason=STOP_COMPLETED, summary=summary, steps_used=1)
    return _run


@pytest.fixture
def run_service(tmp_path, monkeypatch):
    """A REAL service with a fake child runner and an isolated journal dir.

    The run-level tests need the real thread derivation, the real journal path
    and the real limits; only the child execution is faked, so a test cannot
    pass on a service double that never applies any of them.
    """
    import dev_agent.config as dagent_config

    journal_dir = tmp_path / "task_states"
    journal_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dagent_config, "TASK_STATES_DIR", journal_dir,
                        raising=False)
    monkeypatch.setattr(dagent_config, "WORKSPACE_SELECTED", True,
                        raising=False)

    service = OrchestrationService()
    service.set_store_root(str(tmp_path / "runs"))
    return service


def _finish_target(service, run_id="target1", thread="parent-thread",
                   summary="wrote report.md", artifacts=None, **extra):
    """Create a FINISHED target run the way the runtime does."""
    service.ensure_run(run_id, thread_id=thread, agent_slug="worker")
    service.finish_run(run_id, AgentRunResult(
        run_id=run_id, status=STATUS_DONE, stop_reason=STOP_COMPLETED,
        summary=summary, steps_used=3,
        tokens_used={"in": 10, "out": 5},
        artifacts=list(artifacts or [])))
    return service.get_record(run_id)


def test_the_brief_carries_the_result_but_never_the_transcript(
        verifier_appointed, run_service):
    """Test 11: the verifier sees the RESULT - not the journal, not the thread.

    Independence is the whole point of the tool: a verifier that reads how the
    executor reasoned agrees with the reasoning instead of checking the outcome.
    """
    from core.orchestration.contracts import Artifact

    record = _finish_target(
        run_service, artifacts=[Artifact(path="/tmp/report.md", kind="file",
                                         digest="sha256:abc")])
    # Fields that must never reach the verifier, set right after the finish so
    # no journal is written for them.
    record.journal_path = "/secret/child-journal.md"
    record.child_thread_id = "child-thread-xyz"

    captured = {}
    run_service.set_child_runner(_verifier_runner(captured))

    result = T.verify_result("target1", "report.md exists and is not empty",
                             service=run_service)

    assert result["ok"] is True, result
    brief = captured["brief"]
    assert brief is not None
    text = brief.goal + "\n" + brief.brief
    # The result itself is there...
    assert "wrote report.md" in text
    assert "/tmp/report.md" in text
    assert "sha256:abc" in text
    assert "report.md exists and is not empty" in text
    # ...and the process behind it is not.
    assert "/secret/child-journal.md" not in text
    assert "child-thread-xyz" not in text
    assert "messages" not in text.lower()


def test_the_verifier_gets_its_own_thread_and_journal(
        verifier_appointed, run_service):
    """Test 12: a verifier is a child like any other - its own thread, its own
    journal - so its work never writes into the parent's Active Task."""
    from dev_agent.task_state import task_state_path

    _finish_target(run_service)
    captured = {}
    run_service.set_child_runner(_verifier_runner(captured))

    result = T.verify_result("target1", "criteria", service=run_service)

    verifier_run_id = result["verdict"]["verifier_run_id"]
    assert verifier_run_id
    verifier_record = run_service.get_record(verifier_run_id)
    child_thread = verifier_record.child_thread_id
    assert child_thread
    assert child_thread != "parent-thread"
    assert child_thread == captured["child_thread_id"]
    assert task_state_path(child_thread).is_file()


# ─── limits (test 13) ─────────────────────────────────────────────────────


def test_the_verifier_obeys_the_tree_limits(verifier_appointed, run_service):
    """Test 13: a verifier is refused exactly like a normal child.

    ``total_subagent_runs`` is used rather than the pool: an exhausted pool
    makes a BLOCKING spawn wait for a slot, which would stall the test for
    minutes instead of proving the refusal.
    """
    from core.orchestration.contracts import AgentBrief, SpawnRequest

    _finish_target(run_service)
    run_service.set_child_runner(_verifier_runner({}))
    # The root run counts itself, so a cap of 2 admits exactly one child.
    limits = {"total_subagent_runs": 2}

    first = run_service.run(
        SpawnRequest(agent_slug="worker", request=AgentBrief(goal="consume"),
                     thread_id="parent-thread"),
        parent_config=limits)
    assert first["ok"] is True, first

    result = T.verify_result("target1", "criteria",
                             parent_config=limits, service=run_service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_LIMIT_EXCEEDED
    assert result["limit"] == "total_subagent_runs"
    # The appointment is reported too: the caller sees WHO was not launched.
    assert result["verifier"] == VERIFIER


# ─── catalogue and prompt (tests 14-15) ───────────────────────────────────


@pytest.fixture
def isolated_data_dir():
    """Point core.paths (and the DB engines) at a throwaway data folder."""
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="sagaai_test_verify_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    reset_engine()
    reset_devagent_engine()
    yield tmp
    reset_engine()
    reset_devagent_engine()

    if old_data_dir:
        os.environ["SAGAAI_DATA_DIR"] = old_data_dir
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR"):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def two_employees(isolated_data_dir):
    """This employee and a second one that can be appointed as its verifier."""
    from core.orchestrators import create_orchestrator

    assert create_orchestrator(SLUG, "Verify Orch", "checks results") is not None
    assert create_orchestrator(VERIFIER, "Verify Checker", "verifies") is not None
    return SLUG


def test_the_tool_is_hidden_until_a_verifier_is_appointed(two_employees):
    """Test 14: without an appointment the model must not even see the tool.

    An offered tool the runtime refuses is a dead end that costs the model a
    step, so the catalogue and the runtime read the SAME setting.
    """
    from core.orchestrator_tools import build_tool_catalog, is_conditional_tool_enabled
    from core.orchestrators import set_verifier_agent

    assert is_conditional_tool_enabled("verify_result", SLUG) is False
    names = [t["name"] for t in build_tool_catalog(SLUG, include_connections=False)]
    assert "verify_result" not in names

    assert set_verifier_agent(SLUG, VERIFIER) is True

    assert is_conditional_tool_enabled("verify_result", SLUG) is True
    entries = {t["name"]: t["desc"]
               for t in build_tool_catalog(SLUG, include_connections=False)}
    assert "verify_result" in entries
    # The description must state the boundary and the inconclusive rule.
    assert "inconclusive" in entries["verify_result"]
    assert "transcript" in entries["verify_result"]


def test_the_prompt_block_appears_only_with_a_verifier(two_employees):
    """Test 15: the rule reaches the model through the prompt, not a hope."""
    from core.orchestrators import _extend_prompt_with_delegation, set_verifier_agent

    without = _extend_prompt_with_delegation("BASE", SLUG)
    assert "Verifying delegated work" not in without

    assert set_verifier_agent(SLUG, VERIFIER) is True

    with_block = _extend_prompt_with_delegation("BASE", SLUG)
    assert "## Verifying delegated work" in with_block
    assert "verify_result(run_id=..., criteria=...)" in with_block
    assert "inconclusive" in with_block


# ─── acceptance scenario (test 16) ────────────────────────────────────────


class _FanOutDispatcher:
    """Dispatcher surface the fan-out phase reads."""

    child_disabled_tools = set()

    def __init__(self, slug):
        self._slug = slug

    def _current_orchestrator_slug(self):
        return self._slug


def test_a_fan_out_is_verified_without_disturbing_its_order(
        verifier_appointed, run_service, monkeypatch):
    """Test 16: fan out to three workers, verify one, keep the order.

    The fan-out contract is positional, so the acceptance case checks two
    things at once: the results still pair with the calls that produced them,
    and a verification appended after the fan-out answers with a verdict about
    the chosen run instead of adding a fourth "result".
    """
    import core.orchestrators as orch_mod
    from core.orchestration.contracts import AgentBrief
    from dev_agent import agent_loop as L

    # Rights are checked on the loop branch before the runtime sees the call,
    # so the fan-out must be allowed to address the worker - and the registry
    # must know it, because an unknown slug is a caller mistake, not a right.
    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": ["worker"])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "name": slug,
                                      "config": {}})

    def _runner(**kwargs):
        brief = kwargs.get("brief") or AgentBrief(goal="")
        if str(brief.goal).startswith(V.VERIFIER_GOAL_PREFIX):
            return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                                  stop_reason=STOP_COMPLETED,
                                  summary=_answer("passed", [], ["a.txt"]))
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED,
                              summary="done: " + str(brief.goal))

    run_service.set_child_runner(_runner)

    state = L.AgentLoopState(task="fan out", thread_id="thread-e2e",
                             run_id="run-e2e")
    dispatcher = _FanOutDispatcher(SLUG)
    calls = [{"tool": "spawn_agent",
              "args": {"agent_slug": "worker",
                       "request": {"goal": "audit %d" % i}}}
             for i in range(3)]

    results = L.execute_delegation_calls(calls, state=state,
                                         dispatcher=dispatcher,
                                         service=run_service)

    assert len(results) == 3
    assert [r["result"]["summary"] for r in results] == [
        "done: audit 0", "done: audit 1", "done: audit 2"]
    target_run_id = results[1]["result"]["run_id"]

    verdicts = L.execute_delegation_calls(
        [{"tool": "verify_result",
          "args": {"run_id": target_run_id,
                   "criteria": "the audit report lists every finding"}}],
        state=state, dispatcher=dispatcher, service=run_service)

    assert len(verdicts) == 1
    assert verdicts[0]["ok"] is True, verdicts[0]
    assert verdicts[0]["verdict"]["verdict"] == "passed"
    assert verdicts[0]["verdict"]["target_run_id"] == target_run_id
    # The verifier run is a child of the same tree, not a fourth fan-out item.
    assert verdicts[0]["verdict"]["verifier_run_id"] not in [
        r["result"]["run_id"] for r in results]