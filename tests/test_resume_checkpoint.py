# -*- coding: utf-8 -*-
"""tests/test_resume_checkpoint.py - continuing from a transcript checkpoint.

The journal and the checkpoint answer different questions, and a resume needs
both:

- the journal says what the child PLANNED and how it worded its steps;
- the checkpoint says how far it actually GOT and which files it wrote.

A killed child often never wrote a journal at all, so the checkpoint is the
only source left. These tests pin the facts the hint is built from - the step
number and the written files - because a hint without them tells the resumed
child to start over, which is the exact failure this work exists to prevent.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration.resume import build_resume  # noqa: E402


def _record(**kw):
    """A finished run record, as the service stores it."""
    base = {"run_id": "kid", "status": "failed", "goal": "ship the module",
            "agent_slug": "dev_agent", "child_thread_id": "th-kid"}
    base.update(kw)
    return base


def _checkpoint(**kw):
    base = {"step": 3, "max_steps": 10, "written": ["core/a.py", "core/b.py"]}
    base.update(kw)
    return base


# ─── the checkpoint supplies the facts ─────────────────────────────────────

def test_the_written_files_reach_the_hint():
    """Naming them is what stops a resumed child from regenerating them."""
    built = build_resume(_record(), read_checkpoint=lambda tid: _checkpoint())

    assert built["ok"] is True
    assert "core/a.py" in built["resume_hint"]
    assert built["written"] == ["core/a.py", "core/b.py"]


def test_the_step_number_reaches_the_hint():
    """"It completed step 3 of 10" is the shortest useful progress report."""
    built = build_resume(_record(), read_checkpoint=lambda tid: _checkpoint())

    assert built["step"] == 3
    assert "step 3 of 10" in built["resume_hint"]


def test_the_checkpoint_is_read_for_the_child_thread():
    """Reading the parent's transcript would answer the wrong question."""
    seen = {}

    def _read(tid):
        seen["tid"] = tid
        return _checkpoint()

    build_resume(_record(), read_checkpoint=_read)

    assert seen["tid"] == "th-kid"


def test_the_result_reports_whether_a_checkpoint_was_found():
    built = build_resume(_record(), read_checkpoint=lambda tid: {})

    assert built["checkpoint_found"] is False
    assert built["written"] == []


# ─── the journal still wins on wording ─────────────────────────────────────

def test_journal_pending_steps_are_used_when_present():
    """The checkpoint says where; the journal says what remains, in words."""
    journal = {"sections": {
        "plan": "### Step 1 - alpha\n### Step 2 - beta\n### Step 3 - gamma",
        "progress": "- [x] Step 1 - alpha\n- [x] Step 2 - beta",
    }}
    built = build_resume(_record(), read_state=lambda p: journal, exists=lambda p: True,
                         journal_path="/j.md",
                         read_checkpoint=lambda tid: _checkpoint(pending=["from cp"]))

    assert built["pending"] == ["gamma"]


def test_the_checkpoint_supplies_pending_when_the_journal_has_none():
    """A killed child often wrote no journal; it still wrote a checkpoint."""
    built = build_resume(_record(), read_checkpoint=lambda tid: _checkpoint(
        pending=["write the parser", "run the tests"]))

    assert built["pending"] == ["write the parser", "run the tests"]


# ─── the checkpoint is optional and must never break a resume ──────────────

def test_a_missing_checkpoint_leaves_the_journal_path_untouched():
    journal = {"sections": {"plan": "### Step 1 - alpha", "progress": ""}}

    built = build_resume(_record(), read_state=lambda p: journal, exists=lambda p: True,
                         journal_path="/j.md", read_checkpoint=lambda tid: {})

    assert built["ok"] is True
    assert built["pending"] == ["alpha"]


def test_a_broken_checkpoint_reader_does_not_break_the_resume():
    """Recovery data is an aid; a defect in it must not block recovery."""
    def _boom(tid):
        raise RuntimeError("damaged log")

    built = build_resume(_record(), read_checkpoint=_boom)

    assert built["ok"] is True
    assert built["checkpoint_found"] is False


def test_no_reader_at_all_is_still_a_valid_call():
    """The seam is optional: existing callers must keep working unchanged."""
    built = build_resume(_record())

    assert built["ok"] is True
    assert built["checkpoint_found"] is False


def test_a_garbage_checkpoint_value_is_ignored():
    built = build_resume(_record(), read_checkpoint=lambda tid: None)

    assert built["ok"] is True


# ─── the refusal paths are unchanged ───────────────────────────────────────

def test_a_run_that_left_nothing_is_still_refused():
    """A checkpoint with no goal and no pending step is not a task."""
    built = build_resume({"run_id": "kid", "status": "failed"},
                         read_checkpoint=lambda tid: {})

    assert built["ok"] is False


def test_the_chain_limit_is_checked_before_the_checkpoint():
    """A capped chain must not read a log it will never use."""
    calls = []

    built = build_resume(_record(resume_depth=99),
                         read_checkpoint=lambda tid: calls.append(tid) or {})

    assert built["ok"] is False
    assert calls == []
