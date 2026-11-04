# -*- coding: utf-8 -*-
"""Tests for delivering a parked child's question to its parent.

The question travels two different roads - the blocking result of the launch
call and a background notice - and both end up in front of a model. These
tests pin what the parent sees on each road, and that child-authored text
arrives as DATA rather than as an instruction.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestration import runs  # noqa: E402
from core.orchestration.tools import fence_question_payload  # noqa: E402
from dev_agent.agent_loop import format_subagent_notice  # noqa: E402


QUESTION = {
    "text": "Which date format should the report use?",
    "options": ["ISO 8601", "DD.MM.YYYY"],
    "done": "read the existing reports",
    "blocked": "cannot pick a format without the owner's rule",
}


def _parked_payload(**extra):
    payload = {
        "run_id": "run-7",
        "status": "awaiting_input",
        "await_reason": "question",
        "question": dict(QUESTION),
        "steps_used": 4,
    }
    payload.update(extra)
    return payload


def test_a_parked_notice_asks_for_an_answer_not_for_a_resume():
    """The follow-up call in the notice must continue the run, not restart it."""
    notice = {"run_id": "run-7", "agent_slug": "dev_agent",
              "status": "awaiting_input", "payload": _parked_payload()}

    line = format_subagent_notice(notice)

    assert "answer_child" in line
    assert "resume_subagent" not in line
    assert "run-7" in line


def test_the_notice_carries_the_decision_the_parent_has_to_make():
    """The parent must be able to answer without a second call."""
    notice = {"run_id": "run-7", "agent_slug": "dev_agent",
              "status": "awaiting_input", "payload": _parked_payload()}

    line = format_subagent_notice(notice)

    assert "Which date format" in line
    assert "cannot pick a format" in line


def test_an_unfinished_run_still_points_at_the_journal_and_resume():
    """A run that stopped without asking keeps the old guidance."""
    notice = {"run_id": "run-8", "agent_slug": "dev_agent",
              "status": "awaiting_input",
              "payload": {"run_id": "run-8", "status": "awaiting_input",
                          "await_reason": "child_unfinished"}}

    line = format_subagent_notice(notice)

    assert "read_subagent_state" in line
    assert "resume_subagent" in line
    assert "answer_child" not in line


def test_the_question_arrives_fenced_as_data():
    """A child that quotes a web page must not speak with the page's voice."""
    notice = {"run_id": "run-7", "agent_slug": "dev_agent",
              "status": "awaiting_input", "payload": _parked_payload()}

    line = format_subagent_notice(notice)

    assert "[DATA_BEGIN: subagent_question]" in line
    assert "[DATA_END]" in line


def test_a_question_without_text_does_not_break_the_notice():
    """A malformed question still produces a usable notice."""
    notice = {"run_id": "run-9", "agent_slug": "dev_agent",
              "status": "awaiting_input",
              "payload": {"run_id": "run-9", "status": "awaiting_input",
                          "await_reason": "question", "question": {}}}

    line = format_subagent_notice(notice)

    assert "answer_child" in line
    assert "Question:" not in line


def test_the_blocking_payload_is_fenced_but_the_text_is_not_lost():
    """Fencing changes the transport, not the words the parent reads."""
    payload = {"ok": True, "result": _parked_payload()}

    fenced = fence_question_payload(payload)

    question = fenced["result"]["question"]
    assert "[DATA_BEGIN: subagent_question]" in question["text"]
    assert "Which date format should the report use?" in question["text"]
    assert "[DATA_BEGIN: subagent_question]" in question["blocked"]
    assert all("[DATA_BEGIN" in option for option in question["options"])
    # The original payload is untouched: the record keeps the plain text.
    assert payload["result"]["question"]["text"] == QUESTION["text"]


def test_fencing_a_payload_without_a_question_is_a_no_op():
    """Every other result passes through unchanged."""
    payload = {"ok": True, "result": {"run_id": "run-1", "status": "done"}}

    assert fence_question_payload(payload) == payload


def test_a_fenced_question_is_still_a_question_on_the_next_read():
    """Re-fencing an already fenced payload must not nest the fences."""
    once = fence_question_payload({"ok": True, "result": _parked_payload()})

    twice = fence_question_payload(once)

    text = twice["result"]["question"]["text"]
    assert text.count("[DATA_BEGIN") == 1


def test_each_question_of_one_run_gets_its_own_notice(tmp_path):
    """A second question must not be swallowed by the first one's claim file.

    Both notices carry the same status; only the dialog round tells them
    apart, and a run that asks twice has to reach its parent twice.
    """
    store = str(tmp_path)

    first = runs.claim_notice("run-7", "awaiting_input", store,
                              discriminator="question-1")
    again = runs.claim_notice("run-7", "awaiting_input", store,
                              discriminator="question-1")
    second = runs.claim_notice("run-7", "awaiting_input", store,
                               discriminator="question-2")

    assert first is True
    assert again is False
    assert second is True
    assert runs.notice_claimed("run-7", "awaiting_input", store,
                               discriminator="question-2") is True


def test_a_finished_run_keeps_one_notice_per_status(tmp_path):
    """Without a dialog round the old exactly-once rule still holds."""
    store = str(tmp_path)

    assert runs.claim_notice("run-8", "done", store) is True
    assert runs.claim_notice("run-8", "done", store) is False
