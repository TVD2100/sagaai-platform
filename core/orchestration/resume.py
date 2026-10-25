# -*- coding: utf-8 -*-
"""core.orchestration.resume - turn a finished run into a resumable next run.

What problem this solves: a child that stopped at ``awaiting_input``, ran out
of steps or failed in the middle leaves its work ON DISK (its own journal and
the files it already wrote) but the parent has no way to continue it - the run
is terminal and the child's transcript is gone with its thread. Resuming must
therefore be built from the child's JOURNAL, not from its chat history.

Three facts make this module careful rather than clever:

1. the journal is read by ABSOLUTE PATH. ``read_task_state()`` resolves the
   file from the process-wide active thread id, so used as-is it would read
   the PARENT's journal - the exact opposite of the goal.
2. files already produced are not touched. ``done`` artifacts are reported as
   "already done", never re-generated, which is what keeps a resume cheap and
   idempotent instead of re-doing the whole task.
3. a resume that cannot say what remains is REFUSED (``resume_unavailable``)
   rather than launched blind: guessing a continuation burns steps and tokens
   and usually conflicts with what the child already wrote.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from core.orchestration.contracts import (
    ERR_RESUME_UNAVAILABLE,
    MAX_RESUME_CHAIN,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
)

log = logging.getLogger("core.orchestration.resume")

#: Hard cap of the continuation text handed to the next run. It travels back
#: into a model prompt, so a grown journal must be condensed, not pasted.
MAX_HINT_CHARS = 1200

#: How many pending steps are quoted in the hint.
MAX_PENDING_STEPS = 8

#: Statuses worth resuming. ``done`` is included on purpose: a parent may want
#: the child to continue with an EXTENDED task ("you finished A, now do B"),
#: and the same machinery serves it.
RESUMABLE_STATUSES = (STATUS_AWAITING_INPUT, STATUS_FAILED, STATUS_CANCELLED,
                      STATUS_DONE)


def _as_list(value: Any) -> List[str]:
    """Return *value* as a list of non-empty stripped strings."""
    if not value:
        return []
    if isinstance(value, str):
        items = value.split("\n")
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        return []
    out: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _normalize_step_title(text: str) -> str:
    """Return a step title comparable across the plan and the progress list.

    The same step is written two ways in a journal: the plan says
    ``### Step 2 - Add the atomic write`` while the progress checklist says
    ``- [~] Step 2 - Add the atomic write (status: in_progress)``. Comparing
    the raw strings therefore NEVER matches, and every finished step would be
    reported as pending - the resume would then redo work already on disk.
    Both sides go through this function, so only the title remains.
    """
    value = str(text or "").strip()
    if value.startswith("### "):
        value = value[4:].strip()
    elif value.startswith("- "):
        value = value[2:].strip()
    if value[:1] in ("[",):
        closing = value.find("]")
        if closing > 0:
            value = value[closing + 1:].strip()
    if value.lower().startswith("step "):
        marker = value.find(" - ")
        if marker > 0:
            value = value[marker + 3:].strip()
    for suffix in ("(status: done)", "(status: in_progress)",
                   "(status: blocked)", "(status: pending)"):
        value = value.replace(suffix, "").strip()
    return value.strip()


def _step_titles(plan_text: str) -> List[str]:
    """Return the step titles of a journal plan section.

    Only the ``### Step N - title`` lines matter for a continuation: the goal
    is already known and the verification lines are noise at this size.
    """
    titles: List[str] = []
    for line in str(plan_text or "").split("\n"):
        stripped = line.strip()
        if not stripped.startswith("### Step "):
            continue
        title = _normalize_step_title(stripped)
        if title:
            titles.append(title)
    return titles


def _done_steps(progress_text: str) -> List[str]:
    """Return the titles of steps marked ``[x]`` in the progress section."""
    done: List[str] = []
    for line in str(progress_text or "").split("\n"):
        stripped = line.strip()
        if not stripped.startswith("- [x]"):
            continue
        title = _normalize_step_title(stripped)
        if title:
            done.append(title)
    return done


def _clip(text: str, limit: int) -> str:
    """Return *text* shortened to *limit* characters on a word boundary."""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    cut = value[:max(0, limit - 1)].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut + "…"


def build_resume(record: Dict[str, Any], *, journal_path: str = "",
                 read_state=None, exists=None) -> Dict[str, Any]:
    """Build the continuation payload of one finished run.

    ``record`` is the durable record of the run to resume (from the service or
    the store). ``journal_path`` overrides the path carried by the record; the
    record's own path is used otherwise. ``read_state`` and ``exists`` are
    injection seams: the service passes :func:`dev_agent.task_state.read_task_state_at`
    and :func:`os.path.isfile`, tests pass fakes.

    Returns ``{"ok": True, "goal", "brief", "pending", "done", "resume_hint",
    "workspace", "agent_slug", "child_thread_id", "source_run_id"}`` on
    success, or ``{"ok": False, "error_code": "resume_unavailable", ...}`` when
    the material needed for a meaningful continuation is missing.

    Never raises: a resume that cannot be built is a refusal the caller can
    report, not a crash in the middle of a delegation.
    """
    if not isinstance(record, dict) or not str(record.get("run_id") or "").strip():
        return {"ok": False, "error_code": ERR_RESUME_UNAVAILABLE,
                "error": "No run record to resume."}

    run_id = str(record.get("run_id") or "").strip()
    status = str(record.get("status") or "").strip()
    if status and status not in RESUMABLE_STATUSES:
        return {"ok": False, "error_code": ERR_RESUME_UNAVAILABLE,
                "error": ("Run " + run_id + " is " + status
                          + ": it is still working or was never started."),
                "status": status}

    chain = int(record.get("resume_depth") or 0)
    if chain >= MAX_RESUME_CHAIN:
        return {"ok": False, "error_code": "resume_chain_limit",
                "error": "Resume chain limit reached.", "value": chain}

    path = str(journal_path or record.get("journal_path") or "").strip()
    state: Dict[str, Any] = {}
    journal_found = False
    if path:
        checker = exists or os.path.isfile
        try:
            journal_found = bool(checker(path))
        except Exception:  # pragma: no cover - a checker must not break resume
            journal_found = False
        if journal_found and read_state is not None:
            try:
                state = dict(read_state(path) or {})
            except Exception as exc:  # pragma: no cover - journal is best effort
                log.warning("resume: journal read failed for %s: %s", path, exc)
                state = {}

    sections = dict(state.get("sections") or {})
    pending_from_plan = _step_titles(sections.get("plan") or "")
    done_titles = _done_steps(sections.get("progress") or "")
    done_set = {t.strip() for t in done_titles}
    pending = [t for t in pending_from_plan if t.strip() not in done_set]
    if not pending:
        pending = [t for t in _as_list(sections.get("handoff"))][:MAX_PENDING_STEPS]

    goal = str(record.get("goal") or "").strip()
    if not goal:
        goal = str(sections.get("task") or "").strip()
    if not goal and not pending:
        return {"ok": False, "error_code": ERR_RESUME_UNAVAILABLE,
                "error": ("Run " + run_id + " left neither a goal nor an"
                          " unfinished step: nothing to continue."),
                "journal_found": journal_found}

    summary = _clip(str(record.get("summary") or ""), 240)
    parts: List[str] = [
        "Continue the delegated task that was interrupted.",
        "Original goal: " + _clip(goal, 300),
    ]
    if summary:
        parts.append("Where it stopped: " + summary
                     + (" (" + status + ")" if status else ""))
    if done_titles:
        parts.append("Already done, do NOT redo: "
                     + _clip("; ".join(done_titles[:MAX_PENDING_STEPS]), 300))
    if pending:
        parts.append("Still pending: "
                     + _clip("; ".join(pending[:MAX_PENDING_STEPS]), 400))
    parts.append("Work from the files that already exist; do not start over.")
    hint = _clip(" ".join(parts), MAX_HINT_CHARS)

    return {
        "ok": True,
        "source_run_id": run_id,
        "agent_slug": str(record.get("agent_slug") or ""),
        "workspace": str(record.get("workspace") or ""),
        "child_thread_id": str(record.get("child_thread_id") or ""),
        "goal": goal,
        "brief": hint,
        "pending": pending[:MAX_PENDING_STEPS],
        "done": done_titles[:MAX_PENDING_STEPS],
        "resume_hint": hint,
        "journal_found": journal_found,
        "journal_path": path,
        "resume_depth": chain,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
