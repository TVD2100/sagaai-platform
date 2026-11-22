# -*- coding: utf-8 -*-
"""The dialog step keeps its journal thread bound for the whole step.

``begin_step`` publishes the served dialog into the process-wide
``ACTIVE_THREAD_ID``, and two dialogs whose steps interleave in one process
republish it. Every journal call of a step (the scaffold, the section writes,
the archive) must nevertheless address the thread the loop STATE belongs to,
otherwise a step archives the other dialog's task and the two journals
silently swap their contents.

Covered:

- a step of dialog A that resumes after dialog B published its id still
  writes into A's journal;
- the previous binding is restored when the step ends, so a later global-only
  call is unaffected;
- two interleaved steps on separate Python threads keep their own journals.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import config
from dev_agent import task_state as ts
from dev_agent.loop.state import AgentLoopState
from dev_agent.loop.steps.runner import run_step
from dev_agent.loop.steps.runtime import LoopRuntime

THREAD_A = "scope_thread_a"
THREAD_B = "scope_thread_b"


@pytest.fixture
def sandbox(tmp_path):
    """Repoint DevAgent at a temp workspace and clear the live thread id."""
    old_root = config.PROJECT_ROOT
    old_thread = config.ACTIVE_THREAD_ID
    old_selected = getattr(config, "WORKSPACE_SELECTED", True)
    try:
        config.set_target_root(tmp_path)
        config.ACTIVE_THREAD_ID = ""
        config.WORKSPACE_SELECTED = True
        yield tmp_path
    finally:
        config.set_target_root(old_root)
        config.ACTIVE_THREAD_ID = old_thread
        config.WORKSPACE_SELECTED = old_selected


class _Dispatcher:
    """Dispatcher stub: the step under test resolves its phase without tools."""

    def dispatch(self, name, args, **kwargs):
        return {"ok": True, "tool": name}


def _runtime():
    return LoopRuntime(dispatcher=_Dispatcher(), lang="en")


def _done_state(thread_id):
    """A state whose phase ends the step immediately after the opening."""
    state = AgentLoopState()
    state.thread_id = thread_id
    state.phase = "done"
    return state


def test_step_keeps_its_own_thread_when_another_dialog_publishes(sandbox, monkeypatch):
    """A step must write into the journal of ITS dialog, not the live global."""
    ts.archive_and_start_task(task="Task A", plan="### Step 1 - a\n", thread_id=THREAD_A)

    seen = []
    real = ts.ensure_task_state_file

    def spy(*args, **kwargs):
        # Dialog B's step publishes its id while A's step is mid-flight.
        config.ACTIVE_THREAD_ID = THREAD_B
        res = real(*args, **kwargs)
        seen.append(res)
        return res

    monkeypatch.setattr(ts, "ensure_task_state_file", spy)
    run_step(_done_state(THREAD_A), _runtime())

    assert seen, "the step must keep its journal alive"
    assert seen[0]["thread_id"] == THREAD_A
    assert seen[0]["path"].endswith(f"TASK_STATE__{THREAD_A}.md")
    # Journal B was never created by dialog A's step.
    assert not ts.task_state_path(THREAD_B).exists()


def test_scope_is_released_when_the_step_ends(sandbox):
    """No stale binding survives the step.

    The step itself publishes its dialog into the global (that is what
    ``begin_step`` is for), so the global is deliberately the step's thread
    afterwards. What must NOT survive is the private binding: a leftover pin
    would make every later global-only call of this Python thread resolve to
    a finished step's dialog.
    """
    run_step(_done_state(THREAD_A), _runtime())

    assert getattr(ts._SCOPE, "thread_id", "") == ""
    assert ts.current_thread_id(THREAD_B) == THREAD_B


def test_two_threads_do_not_share_a_journal(sandbox):
    """Interleaved steps on separate Python threads keep separate journals.

    Both workers sit inside their own step at the same time (a barrier in the
    scaffold call), so each publishes its own id into the shared global while
    the other is still running. The scope must keep the journals apart anyway.
    """
    ts.archive_and_start_task(task="Task A", plan="### Step 1 - a\n", thread_id=THREAD_A)
    ts.archive_and_start_task(task="Task B", plan="### Step 1 - b\n", thread_id=THREAD_B)

    barrier = threading.Barrier(2)
    errors = []
    real = ts.ensure_task_state_file

    def spy(*args, **kwargs):
        barrier.wait(timeout=5)
        return real(*args, **kwargs)

    def worker(thread_id, handoff):
        try:
            run_step(_done_state(thread_id), _runtime())
            with ts.thread_scope(thread_id):
                ts.update_task_state_section("handoff", handoff)
        except Exception as exc:  # pragma: no cover - failure reporting only
            errors.append((thread_id, repr(exc)))

    ts.ensure_task_state_file, original = spy, real
    try:
        threads = [
            threading.Thread(target=worker, args=(THREAD_A, "facts A")),
            threading.Thread(target=worker, args=(THREAD_B, "facts B")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        ts.ensure_task_state_file = original

    assert not errors, errors
    journal_a = ts.read_task_state_at(str(ts.task_state_path(THREAD_A)))
    journal_b = ts.read_task_state_at(str(ts.task_state_path(THREAD_B)))
    assert journal_a["sections"]["task"] == "Task A"
    assert journal_b["sections"]["task"] == "Task B"
    assert journal_a["sections"]["handoff"] == "facts A"
    assert journal_b["sections"]["handoff"] == "facts B"
