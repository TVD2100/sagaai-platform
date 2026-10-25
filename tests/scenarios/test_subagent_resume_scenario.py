# -*- coding: utf-8 -*-
"""tests/scenarios/test_subagent_resume_scenario.py - continuing a stopped child.

Given a child that finished part of the work and stopped at a question
When the dialog reads its journal and resumes it with the missing decision
Then the finished files are left untouched, the rest is built, and the new run
inherits the same tree - so the budget is spent, not reset.

The child runner here writes real files into a temporary project, because the
point of the scenario is exactly that a resume does NOT rewrite what the first
attempt already produced.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    ERR_NOT_YOUR_RUN,
    ERR_RESUME_CHAIN_LIMIT,
    STATUS_AWAITING_INPUT,
    STATUS_DONE,
    STOP_COMPLETED,
)
from core.orchestration.service import OrchestrationService  # noqa: E402

CONFIG = {"max_depth": 1, "max_active_subagents": 3,
          "total_subagent_runs": 8, "subagent_timeout_seconds": 30}

JOURNAL = """# Task State

## Active Task

### Task

Write both modules of the report package.

### Plan

### Step 1 - Write module_a

- verification: read the file

### Step 2 - Write module_b

- verification: read the file

### Progress

- [x] Step 1 - Write module_a (status: done)
- [~] Step 2 - Write module_b (status: in_progress)

### Handoff

module_a.py is written; module_b.py is still missing. The layout was approved.
"""


@pytest.fixture
def project(tmp_path):
    """A temporary project the child runner writes into."""
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A private service whose store and journals live in the test folder."""
    from dev_agent import config as dev_config
    from dev_agent import workspace_binding

    journals = tmp_path / "journals"
    journals.mkdir()
    monkeypatch.setattr(dev_config, "TASK_STATES_DIR", journals, raising=False)
    monkeypatch.setattr(workspace_binding, "get_thread_workspace",
                        lambda tid: None, raising=False)
    import core.orchestrators as orch_mod
    monkeypatch.setattr(orch_mod, "get_allowed_agents", lambda slug="dev_agent": [])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": CONFIG}
                        if slug == "dev_agent" else None)

    svc = OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))
    import core.orchestration.service as service_mod
    monkeypatch.setattr(service_mod, "get_service", lambda: svc)
    return svc


def _digest(path):
    """Return the sha256 of a file, or an empty string when it is missing."""
    if not Path(path).is_file():
        return ""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _partial_runner(project, calls=None):
    """First attempt writes module_a and stops; the resume writes module_b.

    The resume is recognised by the continuation hint in the brief, and it
    deliberately refuses to touch module_a: if a resume ever rewrote the
    finished file, this runner would raise instead of quietly succeeding.
    """
    calls = calls if calls is not None else []
    module_a = Path(project) / "module_a.py"
    module_b = Path(project) / "module_b.py"

    def _runner(**kwargs):
        brief = kwargs["brief"]
        text = str(getattr(brief, "brief", "") or "")
        resumed = "Already done" in text
        calls.append({"run_id": kwargs["run_id"], "resumed": resumed,
                      "brief": text})
        if resumed:
            if not module_a.is_file():
                raise AssertionError("the resume lost the work of the first attempt")
            module_b.write_text("B = 2\n", encoding="utf-8")
            return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                                  stop_reason=STOP_COMPLETED,
                                  summary="both modules are ready",
                                  steps_used=1)
        module_a.write_text("A = 1\n", encoding="utf-8")
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_AWAITING_INPUT,
                              stop_reason=STOP_COMPLETED,
                              summary="the layout needs a decision",
                              pending=["Write module_b"], steps_used=1,
                              resume_hint="module_a.py is written")
    return _runner


def _start(service, project, goal="Write both modules"):
    """Run the first (blocking) attempt of the child."""
    return tools.spawn_agent(
        agent_slug="dev_agent", request=AgentBrief(goal=goal),
        parent_slug="dev_agent", parent_run_id="dialog-1", thread_id="dialog-1",
        parent_config=CONFIG, base_dir=str(project), service=service,
    )


def _write_journal(service, run_id):
    """Write the child journal at the path its own record carries."""
    record = service.get_record(run_id).to_record()
    path = Path(record["journal_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(JOURNAL, encoding="utf-8")
    return path


# ─── given: a child that stopped half-way ──────────────────────────────────


def test_the_first_attempt_stops_at_its_open_step(service, project):
    """The child leaves a readable handoff instead of a dead end."""
    service.set_child_runner(_partial_runner(project))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    started = _start(service, project)

    assert started["ok"] is True
    assert started["result"]["status"] == STATUS_AWAITING_INPUT
    assert started["result"]["pending"] == ["Write module_b"]
    assert (Path(project) / "module_a.py").is_file()
    assert not (Path(project) / "module_b.py").exists()


def test_the_dialog_reads_the_handoff_before_resuming(service, project):
    """``read_subagent_state`` answers from the child journal, not from a guess."""
    service.set_child_runner(_partial_runner(project))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _start(service, project)["result"]["run_id"]
    _write_journal(service, rid)

    state = tools.read_subagent_state(run_id=rid, service=service)

    assert state["ok"] is True
    assert "module_b.py is still missing" in state["sections"]["handoff"]

    one = tools.subagent_status(run_id=rid, service=service)
    assert one["status"] == STATUS_AWAITING_INPUT
    assert one["resumable"] is True


# ─── when: the dialog resumes it ───────────────────────────────────────────


def test_resume_finishes_the_work_without_redoing_it(service, project):
    """The finished file keeps its bytes; only the missing one appears."""
    calls = []
    service.set_child_runner(_partial_runner(project, calls))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _start(service, project)["result"]["run_id"]
    _write_journal(service, rid)
    before = _digest(project / "module_a.py")

    resumed = tools.resume_subagent(run_id=rid, parent_slug="dev_agent",
                                   parent_run_id="dialog-1", thread_id="dialog-1",
                                   parent_config=CONFIG, base_dir=str(project),
                                   service=service)

    assert resumed["ok"] is True
    assert resumed["result"]["status"] == STATUS_DONE
    assert _digest(project / "module_a.py") == before
    assert (project / "module_b.py").read_text(encoding="utf-8") == "B = 2\n"
    # The child was told what was already done, not just "continue".
    resumed_call = [c for c in calls if c["resumed"]]
    assert len(resumed_call) == 1
    assert "Write module_a" in resumed_call[0]["brief"]
    assert "do not start over" in resumed_call[0]["brief"].lower()


def test_resume_inherits_the_tree_and_its_budget(service, project):
    """A resume continues the same unit of work: the counters keep growing."""
    service.set_child_runner(_partial_runner(project))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    first = _start(service, project)["result"]
    _write_journal(service, first["run_id"])
    used_before = service._trees["dialog-1"].total_used

    resumed = tools.resume_subagent(run_id=first["run_id"], parent_slug="dev_agent",
                                    parent_run_id="dialog-1", thread_id="dialog-1",
                                    parent_config=CONFIG, base_dir=str(project),
                                    service=service)
    second_id = resumed["result"]["run_id"]
    second = service.get_record(second_id)

    assert second.root_run_id == "dialog-1"
    assert second.depth == service.get_record(first["run_id"]).depth
    assert second.resumed_from == first["run_id"]
    assert second.resume_depth == 1
    # Same tree, same budget: the resume spent one more run, it did not reset.
    assert service._trees["dialog-1"].total_used == used_before + 1


# ─── then: the boundaries hold ─────────────────────────────────────────────


def test_resume_of_a_child_of_another_dialog_is_refused(service, project):
    """Confinement is checked through the public tool, not only in the service."""
    service.set_child_runner(_partial_runner(project))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    service.ensure_run("dialog-2", thread_id="dialog-2", config=CONFIG)
    rid = _start(service, project)["result"]["run_id"]
    _write_journal(service, rid)

    refused = tools.resume_subagent(run_id=rid, parent_slug="dev_agent",
                                    parent_run_id="dialog-2", thread_id="dialog-2",
                                    parent_config=CONFIG, base_dir=str(project),
                                    service=service)

    assert refused["ok"] is False
    assert refused["error_code"] == ERR_NOT_YOUR_RUN


def test_the_resume_chain_stops_at_its_ceiling(service, project):
    """Repeated resuming is refused instead of looping forever."""
    service.set_child_runner(_partial_runner(project))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    current = _start(service, project)["result"]["run_id"]
    _write_journal(service, current)

    seen = []
    for _ in range(2):
        _write_journal(service, current)
        step = tools.resume_subagent(run_id=current, parent_slug="dev_agent",
                                     parent_run_id="dialog-1", thread_id="dialog-1",
                                     parent_config=CONFIG, base_dir=str(project),
                                     service=service)
        assert step["ok"] is True
        current = step["result"]["run_id"]
        seen.append(service.get_record(current).resume_depth)

    assert seen == [1, 2]

    refused = tools.resume_subagent(run_id=current, parent_slug="dev_agent",
                                    parent_run_id="dialog-1", thread_id="dialog-1",
                                    parent_config=CONFIG, base_dir=str(project),
                                    service=service)

    assert refused["ok"] is False
    assert refused["error_code"] == ERR_RESUME_CHAIN_LIMIT
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
