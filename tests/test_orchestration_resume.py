# -*- coding: utf-8 -*-
"""tests/test_orchestration_resume.py - building a continuation of a run.

The point of these tests is that a resume is built from the CHILD's journal,
not from the parent's state and not from a transcript that no longer exists.
The fakes below are therefore shaped like real journal sections, and the
identity checks are exercised through the service, where the tree confinement
and the chain ceiling live.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import resume as RES  # noqa: E402
from core.orchestration import service as S  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    ERR_NOT_YOUR_RUN,
    ERR_RESUME_CHAIN_LIMIT,
    ERR_RESUME_UNAVAILABLE,
    STATUS_AWAITING_INPUT,
    STATUS_DONE,
    STATUS_FAILED,
    SpawnRequest,
    STOP_COMPLETED,
    STOP_ERROR,
)


JOURNAL_SECTIONS = {
    "task": "Refactor the storage layer and add tests.",
    "plan": (
        "### Step 1 - Extract the store\n"
        "- verification: unit\n"
        "### Step 2 - Add the atomic write\n"
        "- verification: unit\n"
        "### Step 3 - Document the layout\n"
        "- verification: read\n"
    ),
    "progress": (
        "- [x] Step 1 - Extract the store (status: done)\n"
        "- [~] Step 2 - Add the atomic write (status: in_progress)\n"
        "- [ ] Step 3 - Document the layout\n"
    ),
    "handoff": "store.py created; atomic write still missing.",
    "analysis": "",
    "requests": "",
}


def _record(**extra):
    """A finished child record shaped like the durable one."""
    data = {
        "run_id": "child-1",
        "root_run_id": "root-1",
        "parent_run_id": "root-1",
        "agent_slug": "dev_agent",
        "status": STATUS_AWAITING_INPUT,
        "summary": "needed a decision about the file layout",
        "goal": "Refactor the storage layer",
        "workspace": "/tmp/project",
        "child_thread_id": "thread-1__sabc12345",
        "journal_path": "/tmp/journals/TASK_STATE__thread-1__sabc12345.md",
        "resume_depth": 0,
        "depth": 1,
    }
    data.update(extra)
    return data


def _reader(sections=None, ok=True):
    """A journal reader fake: returns the given sections for any path."""
    def _read(path):
        return {"ok": ok, "path": path, "exists": True,
                "sections": dict(sections or JOURNAL_SECTIONS)}
    return _read


# ─── building the continuation ─────────────────────────────────────────────


def test_resume_reports_done_and_pending_steps():
    """The builder separates what was finished from what is left."""
    out = RES.build_resume(_record(), read_state=_reader(), exists=lambda p: True)

    assert out["ok"] is True
    assert out["source_run_id"] == "child-1"
    assert out["pending"] == ["Add the atomic write", "Document the layout"]
    assert out["done"] == ["Extract the store"]
    assert out["workspace"] == "/tmp/project"


def test_resume_hint_forbids_redoing_finished_work():
    """The hint must tell the next run to continue, not to start over."""
    out = RES.build_resume(_record(), read_state=_reader(), exists=lambda p: True)
    hint = out["resume_hint"]

    assert "Do not" not in hint or "do NOT redo" in hint
    assert "Extract the store" in hint
    assert "Already done" in hint
    assert "do not start over" in hint.lower()
    assert "Add the atomic write" in hint


def test_resume_hint_is_bounded():
    """The hint travels back into a prompt and must stay small."""
    long_sections = dict(JOURNAL_SECTIONS)
    long_sections["plan"] = "".join(
        "### Step %d - %s\n" % (i, "very long step title " * 12) for i in range(40)
    )
    out = RES.build_resume(_record(), read_state=_reader(long_sections),
                           exists=lambda p: True)

    assert out["ok"] is True
    assert len(out["resume_hint"]) <= RES.MAX_HINT_CHARS
    assert len(out["pending"]) <= RES.MAX_PENDING_STEPS


def test_resume_reads_the_journal_of_the_record_not_the_parents():
    """The path passed to the reader is the one carried by the record."""
    seen = []

    def _read(path):
        seen.append(path)
        return {"ok": True, "sections": dict(JOURNAL_SECTIONS)}

    RES.build_resume(_record(), read_state=_read, exists=lambda p: True)

    assert seen == ["/tmp/journals/TASK_STATE__thread-1__sabc12345.md"]


def test_resume_uses_the_goal_when_the_journal_is_gone():
    """A missing journal still allows resuming from the recorded goal."""
    out = RES.build_resume(_record(), read_state=_reader(), exists=lambda p: False)

    assert out["ok"] is True
    assert out["journal_found"] is False
    assert out["goal"] == "Refactor the storage layer"
    assert out["pending"] == []
    assert "Refactor the storage layer" in out["resume_hint"]


def test_resume_without_goal_or_journal_is_refused():
    """Nothing to continue is a refusal, not an empty run."""
    out = RES.build_resume(_record(goal=""), read_state=_reader({}),
                           exists=lambda p: False)

    assert out["ok"] is False
    assert out["error_code"] == ERR_RESUME_UNAVAILABLE


def test_resume_refuses_a_run_that_is_still_working():
    """Resuming a running child would duplicate its work."""
    out = RES.build_resume(_record(status="running"), read_state=_reader(),
                           exists=lambda p: True)

    assert out["ok"] is False
    assert out["error_code"] == ERR_RESUME_UNAVAILABLE
    assert out["status"] == "running"


def test_resume_refuses_the_same_run_twice_beyond_the_chain():
    """A resume of a resume of a resume is refused by the ceiling."""
    out = RES.build_resume(_record(resume_depth=2), read_state=_reader(),
                           exists=lambda p: True)

    assert out["ok"] is False
    assert out["error_code"] == ERR_RESUME_CHAIN_LIMIT


def test_resume_falls_back_to_the_handoff_when_the_plan_is_empty():
    """An unfinished journal may carry its state in the handoff only."""
    sections = {"task": "", "plan": "", "progress": "",
                "handoff": "next: wire the CLI"}
    out = RES.build_resume(_record(goal=""), read_state=_reader(sections),
                           exists=lambda p: True)

    assert out["ok"] is True
    assert out["pending"] == ["next: wire the CLI"]
    assert out["goal"] == ""
    assert "next: wire the CLI" in out["resume_hint"]


# ─── confinement and inheritance, through the service ─────────────────────


def test_resume_of_a_foreign_run_is_refused(monkeypatch, tmp_path):
    """A caller may only resume runs of its own tree."""
    svc = S.OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))
    svc.set_child_runner(lambda **kw: AgentRunResult(
        run_id=kw["run_id"], status=STATUS_DONE, stop_reason=STOP_COMPLETED))
    svc.ensure_run("root", thread_id="thread-1")
    svc.ensure_run("other-root", thread_id="thread-2")
    foreign = svc.ensure_run("foreign", parent_run_id="other-root", depth=1)
    svc.finish_run("foreign", AgentRunResult(run_id="foreign", status=STATUS_DONE))

    refused = svc._prepare_child(  # noqa: SLF001 - the confinement lives here
        SpawnRequest(agent_slug="dev_agent", request=AgentBrief(goal="continue"),
                     thread_id="thread-1", parent_run_id="root",
                     resume_run_id=foreign.run_id),
        caller_root_run_id="root")

    assert refused["ok"] is False
    assert refused["error_code"] == ERR_NOT_YOUR_RUN


def test_resume_inherits_the_tree_and_the_depth(tmp_path):
    """A resume continues the same unit of work, not one level deeper."""
    svc = S.OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))
    svc.set_child_runner(lambda **kw: AgentRunResult(
        run_id=kw["run_id"], status=STATUS_DONE, stop_reason=STOP_COMPLETED))
    svc.ensure_run("root", thread_id="thread-1")
    first = svc.ensure_run("child-1", parent_run_id="root", depth=1)
    svc.finish_run("child-1", AgentRunResult(run_id="child-1", status=STATUS_FAILED,
                                              stop_reason=STOP_ERROR))

    prep = svc._prepare_child(  # noqa: SLF001
        SpawnRequest(agent_slug="dev_agent", request=AgentBrief(goal="continue"),
                     thread_id="thread-1", parent_run_id="root",
                     resume_run_id="child-1"),
        caller_root_run_id="root")

    try:
        assert prep["ok"] is True
        assert prep["root_id"] == "root"
        assert prep["depth"] == first.depth
        new_rec = prep["rec"]
        assert new_rec.resumed_from == "child-1"
        assert new_rec.resume_depth == 1
    finally:
        svc.release_pool("root", prep.get("slot")) if prep.get("ok") else None


def test_resume_of_an_unknown_run_is_unavailable(tmp_path):
    """A resume needs a record; an invented id is refused clearly."""
    svc = S.OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))
    svc.ensure_run("root", thread_id="thread-1")

    refused = svc._prepare_child(  # noqa: SLF001
        SpawnRequest(agent_slug="dev_agent", request=AgentBrief(goal="continue"),
                     thread_id="thread-1", parent_run_id="root",
                     resume_run_id="does-not-exist"),
        caller_root_run_id="root")

    assert refused["ok"] is False
    assert refused["error_code"] == ERR_RESUME_UNAVAILABLE
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
