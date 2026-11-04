# -*- coding: utf-8 -*-
"""core.orchestration.transcript_reader - read a thread whose content is a file.

The writer (``dev_agent.transcript``) puts message content in an append-only
log; this module reads it back. Both exist because the same question - "what
did this run actually see and say" - must stay answerable for a thread that
has a million messages in it, and must stay answerable for a thread that was
written before the log existed.

Two sources, ONE output shape:

- a thread with a transcript file is read from the file;
- a thread without one (``transcript_path`` is NULL: every dialog written
  before the change) is read from the ``messages`` table exactly as before.

The caller never learns which source answered. That is the whole point of the
index row: introducing the file store must not require a migration before the
first read, and rolling it back must not lose the ability to open old dialogs.

Output contract - identical to ``core.threads_devagent.read_thread_window``:

    {ok, thread_id, title, orchestrator, orchestrator_slug, total, offset,
     limit, count, remaining, has_more, messages: [{index, role, ts, content,
     file_name, file_chars}]}

``index`` is the ABSOLUTE position of a message among the RAW records, so a
hit from a search pairs with the exact offset to read around, even when hidden
messages were filtered out of the window.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

#: Hard ceiling on one page, the same as the dialog reader uses.
MAX_READ_LIMIT = 200

#: Per-message content cap in a preview; kept identical to the dialog reader so
#: a message reads the same whichever source served it.
MAX_MESSAGE_CHARS = 8000

#: Default budget of one read, in characters of message content. A caller that
#: asks for 200 giant messages must get an answer that still fits a context
#: window instead of an error or a truncated reply it cannot see.
DEFAULT_MAX_CHARS = 120_000


def _as_int(value: Any, default: int) -> int:
    """Coerce one argument to int without raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _meta(thread_id: str) -> Dict[str, Any]:
    """Return the stored index row of a thread (empty when unknown)."""
    try:
        from storage.repository_devagent import repo_devagent_load_thread_meta
        return repo_devagent_load_thread_meta(thread_id) or {}
    except Exception:  # pragma: no cover - a broken store must not crash a read
        return {}


def _parent_of(meta: Dict[str, Any]) -> Optional[str]:
    """Return the parent thread id a child transcript is filed under."""
    parent = str(meta.get("parent_thread_id") or "").strip()
    return parent or None


def _is_child_row(meta: Dict[str, Any]) -> bool:
    """True for an index row that describes a delegated child."""
    if not meta:
        return False
    kind = str(meta.get("kind") or "").strip().lower()
    return bool(_parent_of(meta)) or kind == "subagent"


def _pending_child_window(thread_id: str, meta: Dict[str, Any], *,
                          offset: int, limit: int,
                          note_extra: str = "") -> Optional[Dict[str, Any]]:
    """Answer a child whose transcript file does not exist YET, or None.

    A child's index row is created when its thread is bound, before the first
    step; the first message appears only when that step finishes. Until then
    there is no file, and the honest answer is an EMPTY conversation - not the
    legacy ``messages`` table, which never held a child's messages and would
    report the run as missing. That distinction is the whole point: "this run
    has not spoken yet" and "this run does not exist" lead a parent to
    different decisions.

    None means the caller should keep its normal path: a dialog row (legacy
    threads really are backed by the table) or a child that already has a
    file on disk.
    """
    if not _is_child_row(meta):
        return None
    try:
        from dev_agent import transcript as T
        if T.exists(thread_id, _parent_of(meta)):
            return None
    except Exception:  # pragma: no cover - fall through to the empty answer
        pass
    out = _envelope(thread_id, meta, total=0, offset=offset, limit=limit,
                    messages=[], source="transcript")
    out["note"] = (
        "The child has not written anything yet: its transcript file appears "
        "after the first step. This is an empty conversation, not a missing "
        "run." + ((" " + note_extra) if note_extra else ""))
    return out


def _file_records(thread_id: str, meta: Dict[str, Any],
                  max_bytes: int = 0) -> Iterator[Dict[str, Any]]:
    """Yield the records of a thread from its transcript file.

    ``max_bytes`` stops the scan once that many bytes of record payload have
    been produced: a caller asking for the first page of a ten-gigabyte log
    must not read ten gigabytes to answer. Zero means no budget.
    """
    from dev_agent import transcript as T
    parent = _parent_of(meta)
    produced = 0
    try:
        for record in T.iter_records(thread_id, parent):
            # A checkpoint is the platform's progress marker, not something
            # the child said. Counting it as a message would shift every
            # absolute index the model was given and make a hit point at the
            # wrong line.
            if T.is_checkpoint(record):
                continue
            yield record
            if max_bytes:
                produced += len(json.dumps(record, ensure_ascii=False))
                if produced > max_bytes:
                    return
    except Exception:  # pragma: no cover - a broken log yields what it can
        return


def _is_hidden(record: Dict[str, Any]) -> bool:
    """True for a service entry the chat feed would not show.

    Two markers are honoured: the explicit flag the writer stores, and the
    content prefix older rows use. A row written by either path is filtered
    the same way, so a legacy and a new transcript hide the same messages.
    """
    if record.get("hidden"):
        return True
    try:
        from core.threads_devagent import _is_hidden_message
        return bool(_is_hidden_message(str(record.get("content") or "")))
    except Exception:  # pragma: no cover - helper unavailable
        return False


def _preview(record: Dict[str, Any], index: int,
             include_tool_results: bool) -> Optional[Dict[str, Any]]:
    """Build one output message, or None when the entry is hidden."""
    if not include_tool_results and _is_hidden(record):
        return None
    content = str(record.get("content") or "")
    total_chars = len(content)
    if total_chars > MAX_MESSAGE_CHARS:
        content = content[:MAX_MESSAGE_CHARS] + (
            f"\n…[truncated: {total_chars} chars total]"
        )
    return {
        "index": int(index),
        "role": str(record.get("role") or ""),
        "ts": str(record.get("ts") or ""),
        "content": content,
        "file_name": str(record.get("file_name") or ""),
        "file_chars": _as_int(record.get("file_chars"), 0),
    }


def _envelope(thread_id: str, meta: Dict[str, Any], *, total: int,
              offset: int, limit: int, messages: List[Dict[str, Any]],
              source: str, budget_hit: bool = False) -> Dict[str, Any]:
    """Assemble the reply in the shape every thread reader returns."""
    remaining = max(0, int(total) - int(offset) - len(_window_span(
        offset, limit, total)))
    return {
        "ok": True,
        "thread_id": thread_id,
        "title": str(meta.get("title") or ""),
        "orchestrator": str(meta.get("assistant_name") or ""),
        "orchestrator_slug": str(meta.get("assistant_id") or ""),
        "total": int(total),
        "offset": int(offset),
        "limit": int(limit),
        "count": len(messages),
        "remaining": remaining,
        "has_more": remaining > 0,
        "messages": messages,
        "source": source,
        "budget_hit": bool(budget_hit),
    }


def _window_span(offset: int, limit: int, total: int) -> range:
    """Return the raw record indices a window covers."""
    start = max(0, int(offset))
    stop = min(int(total), start + max(0, int(limit)))
    return range(start, stop)


def _count_and_collect(thread_id: str, meta: Dict[str, Any], *,
                       offset: int, limit: int, include_tool_results: bool,
                       max_chars: int, from_tail: bool = False,
                       around: Optional[int] = None,
                       before: int = 0, after: int = 0
                       ) -> Dict[str, Any]:
    """Read the file once, count every record and collect the wanted window.

    One pass, bounded memory: only the records that can end up in the answer
    are kept. Counting in the same pass is what lets ``total`` be exact on a
    file whose index row predates the count column.
    """
    wanted: Dict[int, Dict[str, Any]] = {}
    total = 0
    tail_buffer: List[Dict[str, Any]] = []
    tail_from = 0
    used_chars = 0
    budget_hit = False

    if around is not None:
        centre = max(0, int(around))
        start = max(0, centre - max(0, int(before)))
        stop = centre + max(1, int(after))
    elif from_tail:
        start, stop = 0, 0  # decided after the pass, see below
    else:
        start = max(0, int(offset))
        stop = start + max(0, int(limit))

    for index, record in enumerate(_file_records(thread_id, meta)):
        total = index + 1
        if from_tail:
            tail_buffer.append(record)
            if len(tail_buffer) > max(1, int(limit)):
                tail_buffer.pop(0)
            continue
        if start <= index < stop:
            wanted[index] = record

    if from_tail:
        tail_from = max(0, total - len(tail_buffer))
        for offset_in_tail, record in enumerate(tail_buffer):
            wanted[tail_from + offset_in_tail] = record

    messages: List[Dict[str, Any]] = []
    for index in sorted(wanted):
        preview = _preview(wanted[index], index, include_tool_results)
        if preview is None:
            continue
        used_chars += len(preview.get("content") or "")
        if max_chars and used_chars > max_chars and messages:
            budget_hit = True
            break
        messages.append(preview)

    effective_offset = tail_from if from_tail else (
        max(0, int(around) - max(0, int(before))) if around is not None
        else max(0, int(offset)))
    return {
        "total": total,
        "offset": effective_offset,
        "messages": messages,
        "budget_hit": budget_hit,
    }


def read_window(thread_id: str, offset: int = 0, limit: int = 50,
                include_tool_results: bool = False,
                max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, Any]:
    """Return a window of one thread, from its transcript or from SQLite.

    This is the function every reader calls. When the thread has no transcript
    (a legacy dialog) the answer comes from the ``messages`` table through the
    existing reader, so the output is identical by construction.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return {"ok": False, "error": "Missing required argument 'thread_id'."}

    offset = max(0, _as_int(offset, 0))
    limit = max(1, min(_as_int(limit, 50), MAX_READ_LIMIT))
    meta = _meta(tid)

    stored_path = str(meta.get("transcript_path") or "").strip()
    if stored_path:
        # Before the scan: a child whose file does not exist yet has an empty
        # conversation to report, and a scan cannot tell that apart from a
        # window that simply landed past the end.
        pending = _pending_child_window(tid, meta, offset=offset, limit=limit)
        if pending is not None:
            return pending
        try:
            from dev_agent import transcript as T
            parent = _parent_of(meta)
            if T.exists(tid, parent):
                collected = _count_and_collect(
                    tid, meta, offset=offset, limit=limit,
                    include_tool_results=bool(include_tool_results),
                    max_chars=max(0, _as_int(max_chars, DEFAULT_MAX_CHARS)))
                return _envelope(
                    tid, meta, total=collected["total"],
                    offset=collected["offset"], limit=limit,
                    messages=collected["messages"], source="transcript",
                    budget_hit=collected["budget_hit"])
        except Exception:
            # A broken transcript must never make a readable thread unreadable:
            # fall through to the table that still holds the old messages.
            pass

    try:
        from core.threads_devagent import read_thread_window
        legacy = read_thread_window(
            tid, offset=offset, limit=limit,
            include_tool_results=bool(include_tool_results))
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": "Thread read failed: " + str(exc)}
    if isinstance(legacy, dict):
        legacy.setdefault("source", "messages")
        legacy.setdefault("budget_hit", False)
    return legacy


def read_tail(thread_id: str, limit: int = 20,
              include_tool_results: bool = False,
              max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, Any]:
    """Return the LAST messages of a thread - what a resuming agent needs.

    Reading the tail is the common case for recovery: the question is "where
    did it stop", not "what did it start with". A plain offset cannot answer
    it without knowing the total first, which is why this is a separate mode.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return {"ok": False, "error": "Missing required argument 'thread_id'."}
    limit = max(1, min(_as_int(limit, 20), MAX_READ_LIMIT))
    meta = _meta(tid)
    if str(meta.get("transcript_path") or "").strip():
        # Checked BEFORE the scan: a missing file scans as zero records, which
        # would return an ordinary empty window and hide the one thing worth
        # saying - the child has not written anything yet.
        pending = _pending_child_window(tid, meta, offset=0, limit=limit)
        if pending is not None:
            return pending
        try:
            collected = _count_and_collect(
                tid, meta, offset=0, limit=limit,
                include_tool_results=bool(include_tool_results),
                max_chars=max(0, _as_int(max_chars, DEFAULT_MAX_CHARS)),
                from_tail=True)
            return _envelope(
                tid, meta, total=collected["total"],
                offset=collected["offset"], limit=limit,
                messages=collected["messages"], source="transcript",
                budget_hit=collected["budget_hit"])
        except Exception:
            pass
    # Legacy: the table has no cheap tail read, so count first, then window.
    try:
        from core.threads_devagent import read_thread_window
        probe = read_thread_window(tid, offset=0, limit=1,
                                   include_tool_results=True)
        total = _as_int((probe or {}).get("total"), 0)
        start = max(0, total - limit)
        legacy = read_thread_window(
            tid, offset=start, limit=limit,
            include_tool_results=bool(include_tool_results))
        if isinstance(legacy, dict):
            legacy.setdefault("source", "messages")
            legacy.setdefault("budget_hit", False)
        return legacy
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": "Thread read failed: " + str(exc)}


def read_around(thread_id: str, index: int, before: int = 5, after: int = 5,
                include_tool_results: bool = True,
                max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, Any]:
    """Return the messages around one absolute index.

    A search hit gives an absolute index; this turns it into readable context
    without guessing an offset. Tool results are included by default because a
    caller looking around a hit is debugging exactly those.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return {"ok": False, "error": "Missing required argument 'thread_id'."}
    centre = max(0, _as_int(index, 0))
    before = max(0, min(_as_int(before, 5), MAX_READ_LIMIT))
    after = max(1, min(_as_int(after, 5), MAX_READ_LIMIT))
    meta = _meta(tid)
    if str(meta.get("transcript_path") or "").strip():
        pending = _pending_child_window(
            tid, meta, offset=max(0, centre - before), limit=before + after)
        if pending is not None:
            return pending
        try:
            collected = _count_and_collect(
                tid, meta, offset=0, limit=0,
                include_tool_results=bool(include_tool_results),
                max_chars=max(0, _as_int(max_chars, DEFAULT_MAX_CHARS)),
                around=centre, before=before, after=after)
            return _envelope(
                tid, meta, total=collected["total"],
                offset=collected["offset"], limit=before + after,
                messages=collected["messages"], source="transcript",
                budget_hit=collected["budget_hit"])
        except Exception:
            pass
    start = max(0, centre - before)
    try:
        from core.threads_devagent import read_thread_window
        legacy = read_thread_window(
            tid, offset=start, limit=before + after,
            include_tool_results=bool(include_tool_results))
        if isinstance(legacy, dict):
            legacy.setdefault("source", "messages")
            legacy.setdefault("budget_hit", False)
        return legacy
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": "Thread read failed: " + str(exc)}


def describe(thread_id: str) -> Dict[str, Any]:
    """Return what the index knows about a thread without reading its content.

    This is the cheap question a status line and a thread list ask: how many
    messages, how large, how much did it cost, is anything on disk at all.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return {"ok": False, "error": "Missing required argument 'thread_id'."}
    meta = _meta(tid)
    stored_path = str(meta.get("transcript_path") or "").strip()
    on_disk = False
    file_bytes = 0
    segments = 0
    try:
        from dev_agent import transcript as T
        parent = _parent_of(meta)
        if stored_path and T.exists(tid, parent):
            on_disk = True
            folder = T.thread_dir(tid, parent)
            for entry in folder.iterdir():
                # A segment is counted by its NUMBER, not by its suffix: a
                # compressed segment is the same segment after housekeeping,
                # and a reader that stopped seeing it would report a thread
                # as empty right after its oldest segments were compressed.
                if T.is_segment_name(entry.name):
                    segments += 1
                    try:
                        file_bytes += entry.stat().st_size
                    except OSError:
                        pass
    except Exception:
        pass
    # A child whose row exists but whose file does not is a run that has not
    # spoken yet, not a legacy table-backed thread: reporting it as "messages"
    # would send the caller looking for content that was never stored there.
    pending_child = bool(stored_path) and not on_disk and _is_child_row(meta)
    out = {
        "ok": True,
        "thread_id": tid,
        "title": str(meta.get("title") or ""),
        "orchestrator_slug": str(meta.get("assistant_id") or ""),
        "parent_thread_id": _parent_of(meta),
        "kind": str(meta.get("kind") or ""),
        "message_count": _as_int(meta.get("message_count"), 0),
        "total_bytes": _as_int(meta.get("total_bytes"), 0),
        "tokens_in": _as_int(meta.get("tokens_in"), 0),
        "tokens_out": _as_int(meta.get("tokens_out"), 0),
        "tokens_cache": _as_int(meta.get("tokens_cache"), 0),
        "last_message_at": str(meta.get("last_message_at") or ""),
        "source": "transcript" if (on_disk or pending_child) else "messages",
        "transcript_path": stored_path,
        "transcript_on_disk": on_disk,
        "segments": segments,
        "file_bytes": file_bytes,
    }
    if pending_child:
        out["note"] = ("The child has not written anything yet: its transcript "
                       "file appears after the first step.")
    return out


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0