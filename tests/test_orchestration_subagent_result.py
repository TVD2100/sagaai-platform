# -*- coding: utf-8 -*-
"""tests/test_orchestration_subagent_result.py - collecting a finished run.

``subagent_status`` answers "what is happening"; ``subagent_result`` answers
"what did it produce". The two are only useful apart, so these tests pin the
boundary:

- a terminal run returns exactly the object a blocking ``spawn_agent``
  returns - the same keys, built by the same contract, because two shapes of
  one answer make the model guess which one it is reading;
- an unfinished run returns its STATE and no ``result`` key at all: partial
  work presented as a result is the one failure that looks like success;
- a cancelled run is terminal and carries its cancellation code, so a stop
  is never reported as a successful completion.

Each test drives a fake service, so the assertions show which service calls
the tool makes instead of trusting its own state.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentRunResult,
    Artifact,
    ERR_CANCELLED_BY_PARENT,
    ERR_INVALID_ARGS,
    ERR_NOT_YOUR_RUN,
    ERR_RUN_NOT_FOUND,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_RUNNING,
)

#: The twelve keys a result carries to the model. Pinned literally: a new
#: field must be a deliberate decision, not a side effect of a refactor.
RESULT_KEYS = {
    "run_id", "status", "stop_reason", "summary", "artifacts", "error_code",
    "steps_used", "tokens_used", "child_thread_id", "pending", "resume_hint",
    "retryable",
}


class FakeRecord:
    """A live run record with the fields the tools read."""

    def __init__(self, **data):
        self._data = data

    def to_record(self):
        return dict(self._data)


class FakeService:
    """A service double that records every call it receives."""

    def __init__(self, *, live=None, stored=None, wait=None):
        self.calls = []
        self._live = dict(live or {})
        self._stored = dict(stored or {})
        self._wait = wait

    def get_record(self, run_id):
        self.calls.append(("get_record", run_id))
        data = self._live.get(run_id)
        return FakeRecord(**data) if data is not None else None

    def record_from_store(self, run_id):
        self.calls.append(("record_from_store", run_id))
        data = self._stored.get(run_id)
        return dict(data) if data is not None else None

    def wait(self, run_ids, timeout=0.0, **kwargs):
        self.calls.append(("wait", list(run_ids), timeout))
        if self._wait is not None:
            return list(self._wait)
        return [{"ok": True, "run": {"run_id": run_ids[0], "status": STATUS_DONE}}]


def _finished(run_id="r1", **overrides):
    """Return a live record of a successfully finished run."""
    data = {
        "run_id": run_id,
        "root_run_id": run_id,
        "status": STATUS_DONE,
        "stop_reason": "completed",
        "summary": "wrote 3 files",
        "artifacts": [Artifact(path="/tmp/out.txt", kind="file", digest="1-2")],
        "steps_used": 4,
        "tokens_used": {"in": 120, "out": 30, "cache": 10},
        "child_thread_id": "child-thread",
        "retryable": True,
    }
    data.update(overrides)
    return data


# ─── the result of a finished run ─────────────────────────────────────────


def test_a_finished_run_returns_the_same_shape_as_a_blocking_spawn():
    """One answer shape: the keys match ``AgentRunResult.to_dict`` exactly."""
    service = FakeService(live={"r1": _finished()})

    out = tools.subagent_result(run_id="r1", service=service)

    assert out["ok"] is True
    assert out["ready"] is True
    assert set(out["result"]) == RESULT_KEYS
    assert set(out["result"]) == set(AgentRunResult(run_id="x").to_dict())


def test_the_summary_artifacts_and_resources_reach_the_caller():
    """The answer carries what the run produced, not just its verdict."""
    service = FakeService(live={"r1": _finished()})

    result = tools.subagent_result(run_id="r1", service=service)["result"]

    assert result["summary"] == "wrote 3 files"
    assert result["artifacts"] == [
        {"path": "/tmp/out.txt", "kind": "file", "digest": "1-2"}]
    assert result["tokens_used"] == {"in": 120, "out": 30, "cache": 10}
    assert result["steps_used"] == 4


def test_a_long_summary_is_not_truncated_for_a_live_run():
    """The stored-record cap must not shorten the answer of a live run."""
    long_text = "x" * 9000
    service = FakeService(live={"r1": _finished(summary=long_text)})

    result = tools.subagent_result(run_id="r1", service=service)["result"]

    assert len(result["summary"]) == 9000


# ─── an unfinished run never yields a result ──────────────────────────────


def test_a_running_run_answers_not_ready_without_any_result_key():
    """An absent key cannot be mistaken for an empty result; null could."""
    service = FakeService(live={"r1": _finished(
        status=STATUS_RUNNING, stop_reason=None, summary="half done",
        artifacts=[], steps_used=2)})

    out = tools.subagent_result(run_id="r1", service=service)

    assert out["ok"] is True
    assert out["ready"] is False
    assert "result" not in out
    assert out["status"] == STATUS_RUNNING
    assert out["run"]["run_id"] == "r1"


def test_the_partial_text_of_an_unfinished_run_is_not_returned_as_a_result():
    """The worst failure looks like success: partial work as the answer."""
    service = FakeService(live={"r1": _finished(
        status=STATUS_RUNNING, summary="almost there")})

    out = tools.subagent_result(run_id="r1", service=service)

    assert "result" not in out
    assert "almost there" not in str(out.get("result"))
    assert "still working" in out["note"]


def test_an_awaiting_input_run_points_at_the_journal_instead_of_a_result():
    """Unfinished work is resumed, never reported as a produced result."""
    service = FakeService(live={"r1": _finished(
        status="awaiting_input", stop_reason=None)})

    out = tools.subagent_result(run_id="r1", service=service)

    assert out["ready"] is False
    assert "result" not in out
    assert "read_subagent_state" in out["note"]
    assert "resume_subagent" in out["note"]


# ─── cancellation is terminal and is not a success ────────────────────────


def test_a_cancelled_run_is_terminal_and_carries_its_own_code():
    """A stopped run has a result - the stop - and never claims ``done``."""
    service = FakeService(live={"r1": _finished(
        status=STATUS_CANCELLED, stop_reason="aborted",
        error_code=ERR_CANCELLED_BY_PARENT, summary="stopped after step 2",
        artifacts=[])})

    out = tools.subagent_result(run_id="r1", service=service)

    assert out["ready"] is True
    assert out["result"]["status"] == STATUS_CANCELLED
    assert out["result"]["error_code"] == ERR_CANCELLED_BY_PARENT
    assert out["result"]["summary"] == "stopped after step 2"


# ─── waiting ──────────────────────────────────────────────────────────────


def test_a_wait_collects_the_result_once_the_run_finishes():
    """A known-to-be-finishing child is collected in the same step."""
    service = FakeService(live={"r1": _finished(status=STATUS_RUNNING)})

    def _finish_on_wait(run_ids, timeout=0.0, **kwargs):
        service._live["r1"] = _finished()
        return [{"ok": True, "run": {"run_id": run_ids[0], "status": STATUS_DONE}}]

    service.wait = _finish_on_wait
    out = tools.subagent_result(run_id="r1", wait_seconds=5, service=service)

    assert out["ready"] is True
    assert out["result"]["summary"] == "wrote 3 files"


def test_the_wait_is_capped_by_the_status_ceiling():
    """A tool call that blocks for minutes looks like a hung dialog."""
    seen = {}
    service = FakeService(live={"r1": _finished(status=STATUS_RUNNING)})

    def _capture(run_ids, timeout=0.0, **kwargs):
        seen["timeout"] = timeout
        return [{"ok": True, "run": {"run_id": run_ids[0], "status": STATUS_RUNNING}}]

    service.wait = _capture
    out = tools.subagent_result(run_id="r1", wait_seconds=10_000, service=service)

    assert seen["timeout"] == tools.MAX_STATUS_WAIT_SECONDS
    assert out["ready"] is False
    assert "result" not in out


def test_a_finished_run_does_not_wait_at_all():
    """Nothing to wait for: the answer is immediate even with wait_seconds."""
    service = FakeService(live={"r1": _finished()})

    tools.subagent_result(run_id="r1", wait_seconds=20, service=service)

    assert not [c for c in service.calls if c[0] == "wait"]


# ─── refusals ─────────────────────────────────────────────────────────────


def test_a_missing_run_id_is_refused():
    """The tool needs the identifier it collects by; no default exists."""
    out = tools.subagent_result(service=FakeService())

    assert out["ok"] is False
    assert out["error_code"] == ERR_INVALID_ARGS
    assert out["missing"] == ["run_id"]


def test_an_unknown_run_is_refused_instead_of_answering_ready_false():
    """'No such run' is not 'not finished yet' - the model must tell them apart."""
    out = tools.subagent_result(run_id="ghost", service=FakeService())

    assert out["ok"] is False
    assert out["error_code"] == ERR_RUN_NOT_FOUND


def test_a_run_of_another_tree_is_refused():
    """Reading outside the caller's tree would cross the dialog boundary."""
    service = FakeService(live={
        "mine": _finished(root_run_id="mine"),
        "stranger": _finished(run_id="stranger", root_run_id="stranger"),
    })

    out = tools.subagent_result(run_id="stranger", parent_run_id="mine",
                                service=service)

    assert out["ok"] is False
    assert out["error_code"] == ERR_NOT_YOUR_RUN


def test_a_run_of_the_callers_own_tree_is_allowed():
    """Confinement permits the caller's own descendants."""
    service = FakeService(live={
        "root": _finished(run_id="root", root_run_id="root"),
        "child": _finished(run_id="child", root_run_id="root"),
    })

    out = tools.subagent_result(run_id="child", parent_run_id="root",
                                service=service)

    assert out["ok"] is True
    assert out["ready"] is True


# ─── after the registry forgot the run ────────────────────────────────────


def test_a_terminal_stored_record_still_answers_with_its_frozen_result():
    """The registry is a cache: a released run stays collectable."""
    stored = {
        "run_id": "r9", "status": STATUS_DONE, "stop_reason": "completed",
        "summary": "short",
        "result": {
            "run_id": "r9", "status": STATUS_DONE, "stop_reason": "completed",
            "summary": "y" * 7000,
            "artifacts": [{"path": "/tmp/a.txt", "kind": "file", "digest": "9-9"}],
            "steps_used": 7, "tokens_used": {"in": 5}, "retryable": False,
        },
    }
    service = FakeService(stored={"r9": stored})

    result = tools.subagent_result(run_id="r9", service=service)["result"]

    assert len(result["summary"]) == 7000
    assert result["artifacts"][0]["path"] == "/tmp/a.txt"
    assert result["retryable"] is False


def test_a_stored_record_without_a_frozen_result_says_so_instead_of_guessing():
    """An older record's capped summary must not pass for the whole one."""
    stored = {"run_id": "r8", "status": STATUS_DONE, "summary": "z" * 2000}
    service = FakeService(stored={"r8": stored})

    out = tools.subagent_result(run_id="r8", service=service)

    assert out["ready"] is True
    assert "read_subagent_state" in out["note"]
    assert out["result"]["summary"] == "z" * 2000


# ─── catalog ──────────────────────────────────────────────────────────────


def test_the_tool_is_offered_to_the_model_without_extra_configuration():
    """A tool the model cannot see is a feature nobody can use."""
    from core.orchestrator_tools import _delegation_catalog

    names = [entry["name"] for entry in _delegation_catalog()]

    assert "subagent_result" in names


def test_the_description_separates_status_from_result():
    """The two questions must not read as one tool with two names."""
    entry = tools.subagent_result_tool_entry()

    assert entry["name"] == "subagent_result"
    assert "ready=false" in entry["desc"]
    assert "subagent_status" in entry["desc"]
    assert "partial" in entry["desc"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
