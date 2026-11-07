# -*- coding: utf-8 -*-
"""core.orchestration.tools_status - reporting on a delegated run.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from core.orchestration.contracts import (
    ERR_INVALID_ARGS,
    ERR_PROVIDER_ERROR,
    ERR_RESUME_UNAVAILABLE,
    ERR_RUN_NOT_FOUND,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    TERMINAL_STATUSES,
    error_result,
    result_from_record,
)
from core.orchestration.resume import RESUMABLE_STATUSES
from core.orchestration.tools_constants import (
    MAX_STATE_CHARS,
    MAX_STATUS_WAIT_SECONDS,
)
from core.orchestration.tools_notes import (
    _attach_note,
    _journal_note,
    fence_question_payload,
    run_note,
)
from core.orchestration.tools_records import (
    _confine_run,
    _is_live_result,
    _load_run_record,
)


def subagent_status(run_id: Optional[str] = None, *,
                    root_run_id: Optional[str] = None,
                    wait_seconds: float = 0.0,
                    service=None) -> Dict[str, Any]:
    """Report the state of one run, of a tree, or wait briefly for it.

    One tool covers three questions the model actually asks, because three
    tools with overlapping answers would make it guess:

    * ``run_id`` alone - what is happening with that child (live record first,
      durable record second, so a child that outlived its tree is still
      answered);
    * ``root_run_id`` alone - every run of that tree, newest first;
    * ``run_id`` with ``wait_seconds`` - block up to that many seconds for a
      terminal status. Waiting is capped by ``MAX_STATUS_WAIT_SECONDS`` on
      purpose: a tool call that blocks for minutes looks like a hung dialog
      and burns the parent's step budget while it waits.

    A timeout is not an error: the run comes back with ``timed_out: True`` so
    the model can decide to wait again, do something else, or report progress.
    """
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    rid = str(run_id or "").strip()
    tree = str(root_run_id or "").strip()
    if not rid and not tree:
        return error_result(
            "", "subagent_status needs run_id (one run) or root_run_id (a tree).",
            ERR_INVALID_ARGS, missing=["run_id|root_run_id"])

    if rid and not tree:
        wait_for = max(0.0, min(float(wait_seconds or 0.0), MAX_STATUS_WAIT_SECONDS))
        timed_out = False
        if wait_for > 0:
            items = service.wait([rid], timeout=wait_for)
            item = dict(items[0]) if items else {}
            run = dict(item.get("run") or {})
            timed_out = bool(run.get("timed_out"))
        else:
            item = service.poll(rid)
        run = dict(item.get("run") or {})
        if not item.get("ok"):
            # A refusal from poll is the documented run_not_found payload.
            return item if item.get("error_code") else error_result(
                "", str(item.get("error") or "Unknown run."), ERR_RUN_NOT_FOUND)
        payload = {"ok": True, "run": run, "status": str(run.get("status") or ""),
                   "background": bool(run.get("background")),
                   "resumable": str(run.get("status") or "") in RESUMABLE_STATUSES}
        return _attach_note(payload, run_note(run, timed_out=timed_out))

    runs = service.list_runs(tree or None)
    tree = tree or (str((runs[0] or {}).get("root_run_id") or "") if runs else "")
    if rid:
        runs = [r for r in runs if str(r.get("run_id") or "") == rid]
    payload = {"ok": True, "root_run_id": tree, "count": len(runs), "runs": runs}
    # The root record stands for the dialog itself. While the dialog is open it
    # is legitimately running, so counting it as unfinished would report every
    # healthy tree as incomplete; only CHILDREN can hold a tree up.
    unfinished = [r for r in runs
                  if str(r.get("run_id") or "") != tree
                  and str(r.get("status") or "") not in (STATUS_DONE, STATUS_FAILED,
                                                         STATUS_CANCELLED)]
    if unfinished:
        payload = _attach_note(payload, str(len(unfinished)) + " of " + str(len(runs))
                               + " run(s) of this tree are not terminal yet: the tree "
                               "cannot be reported as finished while they run.")
    return payload

def subagent_result(run_id: Optional[str] = None, *,
                    wait_seconds: float = 0.0,
                    parent_slug: str = "dev_agent",
                    parent_run_id: Optional[str] = None,
                    service=None) -> Dict[str, Any]:
    """Return what a FINISHED run produced: text, artifacts, resources used.

    ``subagent_status`` answers "what is happening"; this tool answers "what
    did it produce", and the two questions are kept in separate tools on
    purpose. The result is the same compact dict the blocking ``spawn_agent``
    returns, built by the same contract (``AgentRunResult.to_dict``): two
    shapes of one answer would make the model guess which one it is reading.

    An unfinished run NEVER yields a result. The answer is ``ready: false``
    plus the current state, because a partial result presented as a result is
    the worst kind of error: it looks like success. ``wait_seconds`` waits up
    to ``MAX_STATUS_WAIT_SECONDS`` for a terminal status, so a caller that
    knows the child is about to finish does not have to poll in a loop.

    Works for a run of any origin: blocking or background, live or already
    released from the registry. Only the caller's own tree is readable.
    """
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    rid = str(run_id or "").strip()
    if not rid:
        return error_result(
            "", "subagent_result needs run_id: the run whose result you want.",
            ERR_INVALID_ARGS, missing=["run_id"])

    checked = _confine_run(service, rid, parent_run_id)
    if not checked.get("ok"):
        return checked

    waited = 0.0
    record = _load_run_record(service, rid)
    if record is None:
        return error_result(rid, "Unknown run: " + rid + ".", ERR_RUN_NOT_FOUND)
    status = str(record.get("status") or "")

    wait_for = max(0.0, min(float(wait_seconds or 0.0), MAX_STATUS_WAIT_SECONDS))
    if wait_for > 0 and status not in TERMINAL_STATUSES:
        try:
            service.wait([rid], timeout=wait_for)
        except Exception:  # pragma: no cover - wait is best effort
            pass
        waited = wait_for
        record = _load_run_record(service, rid)
        if record is None:
            return error_result(rid, "Unknown run: " + rid + ".", ERR_RUN_NOT_FOUND)
        status = str(record.get("status") or "")

    if status not in TERMINAL_STATUSES:
        # No ``result`` key at all: an absent field cannot be mistaken for an
        # empty result, while ``result: null`` or a partial summary could.
        payload = {
            "ok": True,
            "ready": False,
            "run_id": rid,
            "status": status,
            "run": record,
            "waited_seconds": waited,
        }
        note = run_note(record)
        if status == STATUS_AWAITING_INPUT and not note:
            note = ("The run stopped with work unfinished, so it has no result "
                    "yet. Read its journal with read_subagent_state(run_id=...) "
                    "and continue it with resume_subagent; do not report partial "
                    "work as its result.")
        return _attach_note(fence_question_payload(payload), note)

    result = result_from_record(record)
    if result is None:  # pragma: no cover - defensive
        return error_result(rid, "Run " + rid + " has an unreadable record.",
                            ERR_PROVIDER_ERROR)
    payload = {
        "ok": True,
        "ready": True,
        "run_id": rid,
        "status": result.status,
        "result": result.to_dict(),
    }
    if waited:
        payload["waited_seconds"] = waited
    if not _is_live_result(record):
        # A record rebuilt from the store may predate the frozen result: say
        # so instead of letting a shortened summary pass for the whole one.
        payload = _attach_note(payload, (
            "This run is no longer in the live registry and its stored record "
            "carries no full result: the summary here may be truncated and the "
            "artifact list may be missing. Read the child journal with "
            "read_subagent_state(run_id=...) for the record it left."))
    return payload

def read_subagent_state(run_id: Optional[str] = None, *,
                        section: Optional[str] = None,
                        service=None) -> Dict[str, Any]:
    """Read the journal of a finished (or running) child run.

    Without this the parent can only see a summary string: the steps the
    child took, what it decided and what it left open live in the child's own
    journal, whose path the record carries. Reading it by absolute path is
    the whole point - the ordinary journal reader resolves the file from the
    process-wide active thread id and would hand back the PARENT's journal.

    ``section`` narrows the answer to one of task/architecture/plan/progress/
    handoff/analysis/requests; without it the sections are returned whole,
    trimmed to ``MAX_STATE_CHARS`` so a grown journal cannot flood the
    parent's context.
    """
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()
    rid = str(run_id or "").strip()
    if not rid:
        return error_result("", "read_subagent_state needs run_id: the run whose "
                            "journal you want to read.", ERR_INVALID_ARGS,
                            missing=["run_id"])
    record = None
    live = service.get_record(rid)
    if live is not None:
        record = live.to_record()
    if record is None:
        record = service.record_from_store(rid)
    if record is None:
        return error_result(rid, "Unknown run: " + rid + ".", ERR_RUN_NOT_FOUND)

    path = str(record.get("journal_path") or "").strip()
    if not path or not os.path.isfile(path):
        return error_result(
            rid, "Run " + rid + " has no journal file yet.",
            ERR_RESUME_UNAVAILABLE, journal_path=path,
            note=("A child writes its journal only when it decides to. An empty "
                  "result here does not prove it did nothing: check the run "
                  "summary and artifacts with subagent_status first, and read "
                  "the journal once the run has finished - its outcome is "
                  "recorded there at the end."))

    try:
        from dev_agent.task_state import read_task_state_at
        state = read_task_state_at(path)
    except Exception as exc:
        return error_result(rid, "Journal read failed: " + str(exc),
                            ERR_PROVIDER_ERROR, journal_path=path)
    if not state.get("ok"):
        return error_result(rid, str(state.get("error") or "Journal unreadable."),
                            ERR_PROVIDER_ERROR, journal_path=path)

    sections = dict(state.get("sections") or {})
    wanted = str(section or "").strip().lower()
    if wanted:
        if wanted not in sections:
            return error_result(
                "", "Unknown journal section: " + wanted + ".",
                ERR_INVALID_ARGS, section=wanted,
                available=sorted(sections.keys()))
        return {"ok": True, "run_id": rid, "journal_path": path,
                "section": wanted, "text": sections.get(wanted, "")}

    trimmed: Dict[str, str] = {}
    budget = MAX_STATE_CHARS
    for name in ("task", "architecture", "plan", "progress", "handoff",
                 "analysis", "requests"):
        text = str(sections.get(name) or "")
        if not text:
            continue
        take = min(len(text), max(0, budget))
        trimmed[name] = text[:take]
        budget -= take
        if budget <= 0:
            break
    return {"ok": True, "run_id": rid, "journal_path": path,
            "sections": trimmed, "step_ids": list(state.get("step_ids") or []),
            "status": str(record.get("status") or ""),
            "note": _journal_note(trimmed, str(record.get("status") or ""))}

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
