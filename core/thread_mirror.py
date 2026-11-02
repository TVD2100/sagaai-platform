# -*- coding: utf-8 -*-
"""core.thread_mirror - the file side of the dialogue transcript mirror.

Every dialogue thread - an assistant chat kept in the main database and an
orchestrator dialog kept in ``devagent.db`` - mirrors its messages into the
same append-only JSONL store (``dev_agent/transcript.py``). The FILE half of
that mirror is identical for both kinds: the same record shape, the same
append/rewrite/read operations, the same failure policy. Only the INDEX half
differs, because the two stores live in different databases with their own
repositories.

So this module owns the file half and nothing else:

- it never opens a database connection and never decides what an index row
  should contain;
- it returns plain summaries (``{ok, bytes, count, error}``) and never raises -
  a mirror that fails must not fail the database write it mirrors;
- it returns messages in the SAME shape the table reader produces
  (``role``/``content``/``ts``/``file_name``/``file_chars``/``_tokens``/
  ``hidden``), so a caller can switch between the two sources without
  translating anything.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def _transcript():
    """Import the writer lazily.

    ``dev_agent`` imports ``core`` in several places, so a module-level import
    here would tie the two packages into a cycle. The writer is also the one
    piece a caller may want to replace in a test.
    """
    from dev_agent import transcript as T
    return T


def parent_of(meta: Dict[str, Any]) -> Optional[str]:
    """Return the parent thread id a child transcript is filed under."""
    parent = str((meta or {}).get("parent_thread_id") or "").strip()
    return parent or None


def is_mirrored(meta: Dict[str, Any]) -> bool:
    """True when the index row points at a transcript file.

    A row without a path is a thread written before the file store existed:
    it keeps working from the ``messages`` table and is never mirrored.
    """
    return bool(str((meta or {}).get("transcript_path") or "").strip())


def transcript_rel_path(thread_id: str,
                        parent_thread_id: Optional[str] = None) -> str:
    """Return the transcript path RELATIVE to the history root.

    The index stores a relative path so the same database stays valid when the
    installation is moved or restored on another machine.
    """
    try:
        T = _transcript()
        return T._relative_to_history(
            T.transcript_path(thread_id, parent_thread_id))
    except Exception:  # pragma: no cover - a path is best effort
        return ""


def next_seq(meta: Dict[str, Any]) -> int:
    """Return the sequence number the next appended record should carry."""
    try:
        return int((meta or {}).get("message_count") or 0) + 1
    except (TypeError, ValueError):
        return 1


def append_message(thread_id: str, message: Dict[str, Any], seq: int,
                   parent_thread_id: Optional[str] = None) -> Dict[str, Any]:
    """Append ONE chat message to a thread transcript.

    Returns ``{ok, path, bytes, error}``. The message becomes a record inside
    the writer: render events (``_events``) are dropped there, because they
    exist only for the live chat feed and would double the file for nothing.
    """
    if not isinstance(message, dict):
        return {"ok": False, "path": "", "bytes": 0, "error": "not a message"}
    try:
        T = _transcript()
        record = T.build_record(message, int(seq))
        return T.append_record(record, thread_id, parent_thread_id)
    except Exception as exc:  # a log must never break the write it logs
        return {"ok": False, "path": "", "bytes": 0, "error": str(exc)}


def rewrite_messages(thread_id: str, messages: Iterable[Dict[str, Any]],
                     parent_thread_id: Optional[str] = None) -> Dict[str, Any]:
    """Replace a whole transcript with *messages*, atomically.

    A full save replaces the conversation, so the file has to be replaced too:
    an append here would keep messages the database just dropped, and a reader
    would keep showing deleted work.

    Returns ``{ok, path, count, bytes, error}``.
    """
    try:
        T = _transcript()
        records = [T.build_record(m, i + 1) for i, m in enumerate(messages)
                   if isinstance(m, dict)]
        result = T.rewrite_records(thread_id, records, parent_thread_id)
        result.setdefault("count", len(records))
        return result
    except Exception as exc:  # pragma: no cover - a log must not break a save
        return {"ok": False, "path": "", "count": 0, "bytes": 0,
                "error": str(exc)}


def read_messages(thread_id: str,
                  parent_thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read a transcript back as chat messages.

    The output shape matches the table reader exactly, so a caller can use
    either source without knowing which one answered. Checkpoints are skipped
    (they are progress markers, not messages), and a torn line is ignored by
    the reader inside the writer instead of corrupting the conversation.
    """
    out: List[Dict[str, Any]] = []
    try:
        T = _transcript()
        for record in T.iter_records(thread_id, parent_thread_id):
            if T.is_checkpoint(record):
                continue
            message: Dict[str, Any] = {
                "role": str(record.get("role") or "user"),
                "content": str(record.get("content") or ""),
                "ts": str(record.get("ts") or ""),
                "file_name": str(record.get("file_name") or ""),
                "file_chars": int(record.get("file_chars") or 0),
            }
            tokens = record.get("tokens")
            if isinstance(tokens, dict) and tokens:
                message["_tokens"] = {
                    "in": int(tokens.get("in", 0) or 0),
                    "out": int(tokens.get("out", 0) or 0),
                    "cache": int(tokens.get("cache", 0) or 0),
                }
            if record.get("hidden"):
                message["hidden"] = True
            out.append(message)
    except Exception:  # a broken log answers with what it could read
        return out
    return out


def is_complete(meta: Dict[str, Any], messages: List[Dict[str, Any]]) -> bool:
    """True when the file still holds every message the index counted.

    The retention policy drops the OLDEST rotated segments once a thread hits
    its quota, so a long conversation keeps a file shorter than its index
    says. Reading such a file would silently hide the beginning of the
    conversation; the caller falls back to the table instead, which still has
    every message.
    """
    if not is_mirrored(meta):
        return False
    try:
        expected = int((meta or {}).get("message_count") or 0)
    except (TypeError, ValueError):
        expected = 0
    return expected > 0 and len(messages) >= expected


def remove(thread_id: str,
           parent_thread_id: Optional[str] = None) -> bool:
    """Delete a thread's transcript folder; True when something was removed."""
    try:
        T = _transcript()
        return bool(T.delete_transcript(thread_id, parent_thread_id))
    except Exception:
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
