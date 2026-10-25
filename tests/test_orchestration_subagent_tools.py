# -*- coding: utf-8 -*-
"""tests/test_orchestration_subagent_tools.py - background and resume tools.

The three observation tools exist because a background child keeps working
after the parent's step ended: the parent must be able to ask what happened
(subagent_status), read what the child actually recorded (read_subagent_state)
and continue an interrupted child without redoing its work
(resume_subagent). Each tool is asserted against a fake service, so the test
shows WHICH service call the tool makes and with which arguments - a tool that
silently answers from its own state would pass a weaker test and fail here.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    ERR_INVALID_ARGS,
    ERR_RESUME_UNAVAILABLE,
    ERR_RUN_NOT_FOUND,
    STATUS_AWAITING_INPUT,
    STATUS_DONE,
    STATUS_RUNNING,
    SpawnRequest,
)


class FakeRecord:
    """A live run record with the fields the tools read."""

    def __init__(self, **data):
        self._data = data

    def to_record(self):
        return dict(self._data)


class FakeService:
    """A service double that records every call it receives."""

    def __init__(self, *, record=None, stored=None, runs=None, poll=None,
                 wait=None, run_result=None, start_result=None):
        self.calls = []
        self._record = record
        self._stored = stored
        self._runs = list(runs or [])
        self._poll = poll
        self._wait = wait
        self.run_result = run_result or {"ok": True, "result": {"run_id": "new"}}
        self.start_result = start_result or {"ok": True, "run_id": "bg1",
                                             "status": STATUS_RUNNING,
                                             "background": True}

    def get_record(self, run_id):
        self.calls.append(("get_record", run_id))
        return self._record if self._record is not None else None

    def record_from_store(self, run_id):
        self.calls.append(("record_from_store", run_id))
        return dict(self._stored) if self._stored is not None else None

    def list_runs(self, root_run_id=None):
        self.calls.append(("list_runs", root_run_id))
        return list(self._runs)

    def poll(self, run_id, **kwargs):
        self.calls.append(("poll", run_id))
        if self._poll is not None:
            return dict(self._poll)
        return {"ok": True, "run": {"run_id": run_id, "status": STATUS_RUNNING}}

    def wait(self, run_ids, timeout=0.0, **kwargs):
        self.calls.append(("wait", list(run_ids), timeout))
        if self._wait is not None:
            return list(self._wait)
        return [{"ok": True, "run": {"run_id": run_ids[0], "status": STATUS_DONE}}]

    def run(self, req, **kwargs):
        self.calls.append(("run", req, kwargs))
        return self.run_result

    def start(self, req, **kwargs):
        self.calls.append(("start", req, kwargs))
        return self.start_result


@pytest.fixture
def allowlists(monkeypatch):
    """Allow self-delegation and register the orchestrator card."""
    import core.orchestrators as orch_mod

    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": [])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": "dev_agent", "config": {}}
                        if slug == "dev_agent" else None)
    return True


# ─── spawn_agent: background ────────────────────────────────────────────────


def test_background_spawn_goes_to_start_and_returns_a_run_id(allowlists):
    """A background spawn must not block: it uses start, not run."""
    service = FakeService()

    result = tools.spawn_agent(
        agent_slug="dev_agent", request=AgentBrief(goal="survey the repo"),
        background=True, parent_slug="dev_agent", thread_id="t1",
        service=service)

    assert result["ok"] is True
    assert result["run_id"] == "bg1"
    assert result["background"] is True
    kind, req, _kwargs = service.calls[-1]
    assert kind == "start"
    assert isinstance(req, SpawnRequest)
    assert req.background is True


def test_blocking_spawn_still_uses_run(allowlists):
    """The default stays blocking so existing callers do not change behaviour."""
    service = FakeService()

    tools.spawn_agent(agent_slug="dev_agent", request=AgentBrief(goal="fix it"),
                      parent_slug="dev_agent", service=service)

    kind, req, _kwargs = service.calls[-1]
    assert kind == "run"
    assert req.background is False


def test_spawn_forwards_resume_run_id(allowlists):
    """The continuation marker reaches the request, not just the service call."""
    service = FakeService()

    tools.spawn_agent(agent_slug="dev_agent", request=AgentBrief(goal="continue"),
                      resume_run_id="child-7", parent_slug="dev_agent",
                      service=service)

    kind, req, _kwargs = service.calls[-1]
    assert kind == "run"
    assert req.resume_run_id == "child-7"


def test_resume_run_id_is_trimmed_and_blank_becomes_none(allowlists):
    service = FakeService()

    tools.spawn_agent(agent_slug="dev_agent", request=AgentBrief(goal="go"),
                      resume_run_id="  child-9  ", parent_slug="dev_agent",
                      service=service)
    assert service.calls[-1][1].resume_run_id == "child-9"

    tools.spawn_agent(agent_slug="dev_agent", request=AgentBrief(goal="go"),
                      resume_run_id="   ", parent_slug="dev_agent",
                      service=service)
    assert service.calls[-1][1].resume_run_id is None


# ─── subagent_status ───────────────────────────────────────────────────────


def test_status_needs_an_id():
    result = tools.subagent_status(service=FakeService())
    assert result["error_code"] == ERR_INVALID_ARGS
    assert result["missing"] == ["run_id|root_run_id"]


def test_status_reports_one_run_and_whether_it_can_be_resumed():
    service = FakeService(poll={"ok": True, "run": {
        "run_id": "r1", "status": STATUS_AWAITING_INPUT, "background": True}})

    result = tools.subagent_status(run_id="r1", service=service)

    assert result["ok"] is True
    assert result["status"] == STATUS_AWAITING_INPUT
    assert result["background"] is True
    assert result["resumable"] is True
    assert ("poll", "r1") in service.calls


def test_status_of_a_running_child_is_not_resumable():
    service = FakeService(poll={"ok": True, "run": {
        "run_id": "r1", "status": STATUS_RUNNING}})

    result = tools.subagent_status(run_id="r1", service=service)

    assert result["resumable"] is False


def test_status_wait_is_capped():
    """A tool call must not block for minutes and look like a hang."""
    service = FakeService()

    tools.subagent_status(run_id="r1", wait_seconds=9999, service=service)

    waits = [c for c in service.calls if c[0] == "wait"]
    assert waits and waits[0][2] == tools.MAX_STATUS_WAIT_SECONDS


def test_status_unknown_run_is_run_not_found():
    service = FakeService(poll={"ok": False, "run_id": "ghost",
                                "error": "Unknown run: ghost"})

    result = tools.subagent_status(run_id="ghost", service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_RUN_NOT_FOUND


def test_status_of_a_tree_lists_its_runs():
    service = FakeService(runs=[{"run_id": "a", "root_run_id": "root",
                                 "status": STATUS_DONE},
                                {"run_id": "b", "root_run_id": "root",
                                 "status": STATUS_RUNNING}])

    result = tools.subagent_status(root_run_id="root", service=service)

    assert result["ok"] is True
    assert result["root_run_id"] == "root"
    assert result["count"] == 2
    assert ("list_runs", "root") in service.calls


# ─── read_subagent_state ───────────────────────────────────────────────────


def test_state_needs_a_run_id():
    result = tools.read_subagent_state(service=FakeService())
    assert result["error_code"] == ERR_INVALID_ARGS


def test_state_of_an_unknown_run_is_run_not_found():
    result = tools.read_subagent_state(run_id="ghost", service=FakeService())
    assert result["ok"] is False
    assert result["error_code"] == ERR_RUN_NOT_FOUND


def test_state_without_a_journal_file_explains_why():
    """A background child may not have written its journal yet."""
    stored = {"run_id": "r1", "journal_path": "/nonexistent/TASK_STATE__x.md",
              "status": STATUS_RUNNING}
    result = tools.read_subagent_state(run_id="r1",
                                       service=FakeService(stored=stored))

    assert result["ok"] is False
    assert result["error_code"] == ERR_RESUME_UNAVAILABLE
    assert result["journal_path"] == "/nonexistent/TASK_STATE__x.md"


def test_state_reads_the_child_journal_by_absolute_path(tmp_path):
    """The parent's journal must never be returned in place of the child's."""
    journal = tmp_path / "TASK_STATE__child.md"
    journal.write_text(
        "# Task State\n\n## Active Task\n\n### Task\n\nSurvey the repository.\n\n"
        "### Plan\n\n### Step 1 - Count the files\n\n"
        "### Progress\n\n- [x] Step 1 - Count the files (status: done)\n\n"
        "### Handoff\n\n42 files counted.\n",
        encoding="utf-8")
    stored = {"run_id": "r1", "journal_path": str(journal),
              "status": STATUS_DONE}

    result = tools.read_subagent_state(run_id="r1",
                                       service=FakeService(stored=stored))

    assert result["ok"] is True
    assert result["journal_path"] == str(journal)
    assert "Survey the repository" in result["sections"]["task"]
    assert "42 files counted" in result["sections"]["handoff"]
    assert result["step_ids"] == ["step_1"]


def test_state_can_return_one_section(tmp_path):
    journal = tmp_path / "TASK_STATE__child.md"
    journal.write_text("# Task State\n\n## Active Task\n\n### Task\n\nGoal text.\n",
                       encoding="utf-8")
    stored = {"run_id": "r1", "journal_path": str(journal),
              "status": STATUS_DONE}

    result = tools.read_subagent_state(run_id="r1", section="task",
                                       service=FakeService(stored=stored))

    assert result["section"] == "task"
    assert "Goal text" in result["text"]


def test_state_rejects_an_unknown_section(tmp_path):
    journal = tmp_path / "TASK_STATE__child.md"
    journal.write_text("# Task State\n\n## Active Task\n\n### Task\n\nGoal.\n",
                       encoding="utf-8")
    stored = {"run_id": "r1", "journal_path": str(journal)}

    result = tools.read_subagent_state(run_id="r1", section="nonsense",
                                       service=FakeService(stored=stored))

    assert result["ok"] is False
    assert result["error_code"] == ERR_INVALID_ARGS
    assert "task" in result["available"]


# ─── resume_subagent ───────────────────────────────────────────────────────


def test_resume_needs_a_run_id():
    result = tools.resume_subagent(service=FakeService())
    assert result["error_code"] == ERR_INVALID_ARGS


def test_resume_of_an_unknown_run_is_unavailable():
    result = tools.resume_subagent(run_id="ghost", service=FakeService())
    assert result["ok"] is False
    assert result["error_code"] == ERR_RESUME_UNAVAILABLE


def test_resume_continues_the_original_goal_without_a_request(allowlists, tmp_path):
    """The common case: the child stopped, continue it as it was."""
    journal = tmp_path / "TASK_STATE__child.md"
    journal.write_text(
        "# Task State\n\n## Active Task\n\n### Task\n\nSurvey the repository.\n\n"
        "### Plan\n\n### Step 1 - Count the files\n\n"
        "### Progress\n\n- [x] Step 1 - Count the files (status: done)\n\n"
        "### Handoff\n\n42 files counted.\n",
        encoding="utf-8")
    stored = {"run_id": "child-1", "agent_slug": "dev_agent",
              "journal_path": str(journal), "status": STATUS_AWAITING_INPUT,
              "goal": "Survey the repository", "resume_depth": 0}
    service = FakeService(record=FakeRecord(**stored))

    result = tools.resume_subagent(run_id="child-1", parent_slug="dev_agent",
                                  thread_id="t1", service=service)

    assert result["ok"] is True
    kind, req, _kwargs = service.calls[-1]
    assert kind == "run"
    assert req.resume_run_id == "child-1"
    assert req.agent_slug == "dev_agent"
    assert req.request.goal == "Survey the repository"
    assert "do not start over" in req.request.brief.lower()


def test_resume_uses_an_explicit_request_when_given(allowlists, tmp_path):
    """A caller may extend the task: the explicit goal wins over the journal."""
    journal = tmp_path / "TASK_STATE__child.md"
    journal.write_text("# Task State\n\n## Active Task\n\n### Task\n\nOld goal.\n",
                       encoding="utf-8")
    stored = {"run_id": "child-1", "agent_slug": "dev_agent",
              "journal_path": str(journal), "status": STATUS_DONE,
              "goal": "Old goal", "resume_depth": 0}
    service = FakeService(record=FakeRecord(**stored))

    result = tools.resume_subagent(
        run_id="child-1", request=AgentBrief(goal="Now add the report"),
        parent_slug="dev_agent", service=service)

    assert result["ok"] is True
    req = service.calls[-1][1]
    assert req.request.goal == "Now add the report"


def test_resume_targets_the_original_agent_slug(monkeypatch, tmp_path):
    """Resuming must not silently switch the child to the caller's own agent.

    The original slug is addressed through the normal path, so the allowlist
    still applies: a child of an agent the caller may no longer call cannot be
    resumed by resuming it. Here the slug IS allowed, and the assertion is
    that the call goes to THAT agent rather than to the caller.
    """
    import core.orchestrators as orch_mod

    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": ["research_agent"])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": {}}
                        if slug in ("dev_agent", "research_agent") else None)

    journal = tmp_path / "TASK_STATE__child.md"
    journal.write_text("# Task State\n\n## Active Task\n\n### Task\n\nGoal.\n",
                       encoding="utf-8")
    stored = {"run_id": "child-1", "agent_slug": "research_agent",
              "journal_path": str(journal), "status": STATUS_DONE,
              "goal": "Goal", "resume_depth": 0}
    service = FakeService(record=FakeRecord(**stored))

    result = tools.resume_subagent(run_id="child-1", parent_slug="dev_agent",
                                   service=service)

    assert result["ok"] is True
    assert service.calls[-1][1].agent_slug == "research_agent"


def test_resume_still_obeys_the_allowlist(monkeypatch, tmp_path):
    """A resume is a spawn: a slug the caller may not use stays refused."""
    import core.orchestrators as orch_mod

    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": [])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": {}}
                        if slug == "dev_agent" else None)

    journal = tmp_path / "TASK_STATE__child.md"
    journal.write_text("# Task State\n\n## Active Task\n\n### Task\n\nGoal.\n",
                       encoding="utf-8")
    stored = {"run_id": "child-1", "agent_slug": "research_agent",
              "journal_path": str(journal), "status": STATUS_DONE,
              "goal": "Goal", "resume_depth": 0}
    service = FakeService(record=FakeRecord(**stored))

    result = tools.resume_subagent(run_id="child-1", parent_slug="dev_agent",
                                   service=service)

    assert result["ok"] is False
    assert result["error_code"] == "agent_not_allowed"


def test_resume_refuses_a_running_child(allowlists):
    """Resuming a child that still works would duplicate its output."""
    stored = {"run_id": "child-1", "agent_slug": "dev_agent",
              "status": STATUS_RUNNING, "goal": "Goal"}
    service = FakeService(record=FakeRecord(**stored))

    result = tools.resume_subagent(run_id="child-1", parent_slug="dev_agent",
                                  service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_RESUME_UNAVAILABLE


def test_resume_background_goes_to_start(allowlists, tmp_path):
    journal = tmp_path / "TASK_STATE__child.md"
    journal.write_text("# Task State\n\n## Active Task\n\n### Task\n\nGoal.\n",
                       encoding="utf-8")
    stored = {"run_id": "child-1", "agent_slug": "dev_agent",
              "journal_path": str(journal), "status": STATUS_DONE,
              "goal": "Goal", "resume_depth": 0}
    service = FakeService(record=FakeRecord(**stored))

    tools.resume_subagent(run_id="child-1", background=True,
                          parent_slug="dev_agent", service=service)

    kind, req, _kwargs = service.calls[-1]
    assert kind == "start"
    assert req.resume_run_id == "child-1"
    assert req.background is True


# ─── catalog ───────────────────────────────────────────────────────────────


def test_catalog_entries_of_the_new_tools():
    for entry_fn, name in (
        (tools.subagent_status_tool_entry, "subagent_status"),
        (tools.read_subagent_state_tool_entry, "read_subagent_state"),
        (tools.resume_subagent_tool_entry, "resume_subagent"),
    ):
        entry = entry_fn()
        assert entry["name"] == name
        assert entry["desc"].strip()

    spawn_desc = tools.spawn_tool_entry()["desc"]
    assert "background" in spawn_desc
    assert "subagent_status" in spawn_desc
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
