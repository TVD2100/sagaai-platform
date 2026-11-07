# -*- coding: utf-8 -*-
"""core.orchestration.tools_notes - the text a tool result carries.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict

from core.orchestration.contracts import (
    AWAIT_QUESTION,
    ERR_CHILD_UNFINISHED,
    ERR_INTERNAL_ERROR,
    ERR_MAX_STEPS,
    ERR_TIMEOUT,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
)


def run_note(run: Dict[str, Any], *, timed_out: bool = False) -> str:
    """Explain what a run status means for the NEXT decision.

    A raw status word is not self-describing: ``failed`` may mean "try again"
    or "never try this again", and ``running`` next to an expired wait looks
    exactly like a hang. The note is attached to tool results instead of
    living in a system prompt, so it reaches every orchestrator without
    anybody editing prompts per agent - and it stays next to the data it
    describes.

    Returns an empty string when the status needs no explanation.
    """
    status = str(run.get("status") or "").strip()
    code = str(run.get("error_code") or "").strip()
    retryable = bool(run.get("retryable", True))
    try:
        steps = int(run.get("steps_used") or 0)
    except (TypeError, ValueError):
        steps = 0

    if timed_out and status == STATUS_RUNNING:
        return ("The wait window expired - the run is STILL RUNNING, not hung. "
                "Call again with wait_seconds, or keep working and check later.")
    if status == STATUS_RUNNING:
        return ("The run is still working. Wait with wait_seconds or check "
                "again later; a background child reports through the mailbox.")
    if status == STATUS_FAILED:
        if code == ERR_INTERNAL_ERROR or not retryable:
            return ("The child loop itself raised after " + str(steps) + " "
                    "step(s): this is a platform defect, not a provider hiccup. "
                    "Re-running the same call fails the same way - do not spawn "
                    "it again; fix the cause or report it as blocked.")
        if code == ERR_MAX_STEPS:
            return ("The child used its whole step budget. Split the task into "
                    "smaller pieces or raise subagent_max_steps: resuming does "
                    "not add steps.")
        if code == ERR_TIMEOUT:
            return ("The child hit its time limit. Narrow the task or raise "
                    "subagent_timeout_seconds.")
        if retryable:
            return ("The child failed and the run is marked retryable: one "
                    "attempt may pass, but change something (narrow the brief) "
                    "instead of repeating the call verbatim.")
        return "The child failed; the run is marked not retryable."
    if status == STATUS_AWAITING_INPUT:
        if str(run.get("await_reason") or "") == AWAIT_QUESTION:
            # The one status that is answered, not resumed: the child waits
            # for a decision and continues the SAME run once it has one.
            return ("The child is waiting for an ANSWER, not for a resume: "
                    "it parked its own run. Read what it needs in the "
                    "``question`` field and reply with "
                    "answer_child(run_id, answer). Resuming would start a "
                    "different attempt and leave the question unanswered.")
        if code == ERR_CHILD_UNFINISHED:
            return ("The child did its work but never signalled completion: "
                    "its files are usually on disk already. Check the "
                    "artifacts before resuming - re-running would duplicate "
                    "what it wrote.")
        return ("The run stopped with work unfinished. Read its journal with "
                "read_subagent_state, then continue with resume_subagent.")
    if status == STATUS_CANCELLED:
        return ("The run was cancelled: its partial result is all there is, "
                "and resuming starts a new attempt.")
    if status == STATUS_DONE:
        return "The run finished: read the summary and artifacts."
    return ""

def _attach_note(payload: Dict[str, Any], note: str) -> Dict[str, Any]:
    """Return *payload* with a ``note`` key added (a no-op for empty notes)."""
    if not note or not isinstance(payload, dict) or payload.get("note"):
        return payload
    out = dict(payload)
    out["note"] = note
    return out

def fence_question_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a parked child's question in data fences for the parent model.

    A question is written by a process that reads files, web pages and
    documents: whatever it quotes must not gain the force of an instruction
    once it reaches the parent. The fenced form is what the model sees; the
    record keeps the plain text, so the UI can show it as written.
    """
    if not isinstance(payload, dict):
        return payload
    result = payload.get("result")
    source = result if isinstance(result, dict) else payload
    question = source.get("question")
    if not isinstance(question, dict) or not question:
        return payload
    try:
        from core.prompt_guard import wrap_data
    except Exception:  # pragma: no cover - guard is best effort
        return payload
    def _once(value: str) -> str:
        # A payload can pass through this function twice (the blocking result
        # first, then the notice built from it). Wrapping an already fenced
        # value would nest the fences and hide the real boundary.
        if not str(value or "").strip():
            return str(value or "")
        try:
            from core.prompt_guard import is_wrapped_data
            if is_wrapped_data(value):
                return value
        except Exception:  # pragma: no cover - guard is best effort
            pass
        return wrap_data(value, "subagent_question")

    fenced = dict(question)
    for field in ("text", "done", "blocked"):
        value = str(fenced.get(field) or "")
        if value.strip():
            fenced[field] = _once(value)
    options = fenced.get("options")
    if isinstance(options, (list, tuple)):
        fenced["options"] = [_once(str(o)) for o in options]
    out = dict(payload)
    if isinstance(result, dict):
        new_result = dict(result)
        new_result["question"] = fenced
        out["result"] = new_result
    else:
        out["question"] = fenced
    return out

def _note_for_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the explanation of a spawn/resume result to its own payload."""
    if not isinstance(payload, dict):
        return payload
    payload = fence_question_payload(payload)
    result = payload.get("result")
    if isinstance(result, dict):
        return _attach_note(payload, run_note(result))
    return payload

def _journal_note(sections: Dict[str, str], status: str) -> str:
    """Explain how to read a child journal that came back thin or empty.

    A journal is the child's own record, not the runtime's: a run that was
    told to work read-only, or one that crashed before its first
    ``task_state_init``, answers with scaffolding only. Without this line the
    parent reads "did nothing" where the truth is "never wrote it down" - and
    the runtime paragraph explaining the actual outcome may not be written
    yet while the run is still working.
    """
    real = {name: text for name, text in (sections or {}).items()
            if text and text.strip() not in ("", "_(not set)_")}
    if not real:
        return ("The journal carries no real content. This does not prove the "
                "run did nothing: a child writes a journal only when it "
                "decides to. Check summary and artifacts with "
                "subagent_status first; once the run is finished, a runtime "
                "outcome paragraph is recorded here automatically.")
    if status == STATUS_RUNNING:
        return ("The run is still working: this is a snapshot, not a final "
                "record. The outcome paragraph appears under Handoff when the "
                "run finishes.")
    if "Runtime outcome" not in (sections.get("handoff") or ""):
        return ("The journal has no runtime outcome paragraph: read it together "
                "with the run summary instead of treating the handoff as the "
                "whole story.")
    return ""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
