# -*- coding: utf-8 -*-
"""core.orchestration.tools_journal - reading a child transcript.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.orchestration.contracts import (
    ERR_INTERNAL_ERROR,
    ERR_INVALID_ARGS,
    ERR_PROVIDER_ERROR,
    ERR_RESUME_UNAVAILABLE,
    error_result,
)
from core.orchestration.tools_records import (
    _child_thread_row_missing,
    _confine_run,
)


def read_subagent_thread(run_id: Optional[str] = None, *,
                         offset: int = 0,
                         limit: int = 50,
                         include_tool_results: bool = False,
                         mode: str = "window",
                         index: Optional[int] = None,
                         before: int = 5,
                         after: int = 5,
                         parent_slug: str = "dev_agent",
                         parent_run_id: Optional[str] = None,
                         service=None) -> Dict[str, Any]:
    """Read the CONVERSATION of a subagent run, not just its journal.

    The journal answers "what did it plan and finish"; this tool answers "what
    did it actually see and say" - the model replies, the tool calls and the
    errors of a child whose result looks wrong. A long child is hard to debug
    from a summary alone, so the parent reads its transcript.

    Access is limited to the caller's own tree: a run of another dialog is not
    addressable, and an unknown run answers ``run_not_found`` instead of an
    empty transcript (an empty list would look like a silent child).

    Windows are paginated with ABSOLUTE message indices, so a hit from a
    search pairs with the exact offset to read around.
    """
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()
    rid = str(run_id or "").strip()
    if not rid:
        return error_result("", "read_subagent_thread needs run_id: the run whose "
                            "conversation you want to read.", ERR_INVALID_ARGS,
                            missing=["run_id"])

    # Confinement: only the caller's own tree is readable. Reading somebody
    # else's transcript would leak another dialog's work into this context.
    checked = _confine_run(service, rid, parent_run_id)
    if not checked.get("ok"):
        return checked
    record = dict(checked.get("record") or {})

    thread_id = str(record.get("child_thread_id") or "").strip()
    if not thread_id:
        return error_result(
            rid, "Run " + rid + " has no child thread recorded.",
            ERR_RESUME_UNAVAILABLE,
            note=("A run prepared without a child thread (an older record or a "
                  "test runner) has no transcript. Use read_subagent_state to "
                  "read its journal instead."))

    # Three ways to read the same transcript, because a parent asks three
    # different questions: "show me page N" (window, the default), "where did
    # it stop" (tail, what a resume needs) and "what is around this hit"
    # (around, pairing with a search result's absolute index).
    wanted = str(mode or "window").strip().lower()
    if wanted not in ("window", "tail", "around", "meta"):
        # A typo must not silently answer with nothing: the paged read is the
        # documented default and the only mode every caller understands.
        wanted = "window"
    try:
        from core.orchestration import transcript_reader as reader
        if wanted == "tail":
            window = reader.read_tail(
                thread_id, limit=limit,
                include_tool_results=bool(include_tool_results))
        elif wanted == "around":
            window = reader.read_around(
                thread_id, index=index if index is not None else offset,
                before=before, after=after,
                include_tool_results=bool(include_tool_results))
        elif wanted == "meta":
            described = reader.describe(thread_id)
            window = described if described.get("ok") else {
                "ok": False, "error": described.get("error") or "Unreadable."}
        else:
            window = reader.read_window(
                thread_id, offset=offset, limit=limit,
                include_tool_results=bool(include_tool_results))
    except Exception as exc:  # pragma: no cover - defensive
        return error_result(rid, "Thread read failed: " + str(exc),
                            ERR_INTERNAL_ERROR)
    if not window.get("ok"):
        # Two different failures used to look alike here. A child whose index
        # row is missing cannot be read by ANY reader - the transcript file
        # may be complete on disk and still be unreachable - and the answer
        # for that is the journal, not a retry. A damaged transcript is a
        # different problem: the row exists, the file does not answer.
        #
        # The "not found" wording is part of the test, not decoration: it is
        # what the legacy reader answers when the row does not exist, and a
        # reader that failed for some OTHER reason must keep its own error
        # instead of being relabelled as a missing row.
        if ("not found" in str(window.get("error") or "").lower()
                and _child_thread_row_missing(thread_id)):
            return error_result(
                rid, "Child thread " + thread_id + " has no index row, so its "
                "transcript cannot be read.", ERR_RESUME_UNAVAILABLE,
                child_thread_id=thread_id,
                note=("The run exists and its journal is readable: use "
                      "read_subagent_state for what it planned and finished. "
                      "A transcript without an index row is not addressable by "
                      "any reader, because the row is what names the file."))
        return error_result(rid, str(window.get("error") or "Thread unreadable."),
                            ERR_PROVIDER_ERROR, child_thread_id=thread_id)

    window["run_id"] = rid
    window["child_thread_id"] = thread_id
    window["status"] = str(record.get("status") or "")
    window["mode"] = wanted
    if wanted == "meta":
        # A description carries no messages on purpose: it answers "how long,
        # how large, how expensive" without reading the content.
        return window
    if not window.get("messages") and not window.get("note"):
        # The reader's own note wins: it knows WHY the window is empty (a child
        # that has not written yet vs. an offset past the end), and overwriting
        # it here flattened that distinction into one vague sentence.
        window["note"] = ("The transcript window is empty. Either the offset is "
                          "past the end, or the child has not produced a "
                          "visible message yet.")
    return window

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
