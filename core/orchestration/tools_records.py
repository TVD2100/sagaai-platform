# -*- coding: utf-8 -*-
"""core.orchestration.tools_records - run lookup and visibility.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.orchestration.contracts import (
    ERR_NOT_YOUR_RUN,
    ERR_RUN_NOT_FOUND,
    error_result,
)


def _load_run_record(service, run_id: str) -> Optional[Dict[str, Any]]:
    """Return a run record as a dict: the live copy first, the store second.

    The two sources carry the same fields but different shapes - a live record
    is an object with attributes, a stored one is already a dict - so callers
    get one shape and never branch on where the record came from.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return None
    try:
        live = service.get_record(rid)
    except Exception:  # pragma: no cover - registry is best effort
        live = None
    if live is not None:
        try:
            return live.to_record()
        except Exception:  # pragma: no cover - defensive
            if isinstance(live, dict):
                return dict(live)
    try:
        return service.record_from_store(rid)
    except Exception:  # pragma: no cover - store is best effort
        return None

def _resolve_run_root(service, run_id: str) -> str:
    """Return the tree root of a run, falling back to the run itself.

    A root record carries its own id as ``root_run_id``, so the fallback is
    correct for a dialog-level run whose record still lacks the field.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return ""
    record = _load_run_record(service, rid) or {}
    return str(record.get("root_run_id") or "").strip() or rid

def _confine_run(service, run_id: str,
                 parent_run_id: Optional[str]) -> Dict[str, Any]:
    """Load a run record, refusing a run that belongs to another tree.

    Reading or stopping a child is a right of its OWN tree: another dialog's
    transcript is not addressable, and reaching into its runs would be a way
    around the dialog boundary. Callers pass the parent run id they were given;
    without one the call is treated as dialog-level and allowed.

    Returns ``{"ok": True, "record": {...}}`` or the ready error payload.
    """
    rid = str(run_id or "").strip()
    record = _load_run_record(service, rid)
    if record is None:
        return error_result(rid, "Unknown run: " + rid + ".", ERR_RUN_NOT_FOUND)
    parent = str(parent_run_id or "").strip()
    if not parent:
        return {"ok": True, "record": record}
    caller_root = _resolve_run_root(service, parent)
    target_root = str(record.get("root_run_id") or "").strip() or _resolve_run_root(
        service, rid)
    if caller_root and target_root and caller_root != target_root:
        return error_result(
            rid, "Run " + rid + " belongs to another tree.", ERR_NOT_YOUR_RUN,
            root_run_id=target_root, caller_root_run_id=caller_root)
    return {"ok": True, "record": record}

def _is_live_result(record: Dict[str, Any]) -> bool:
    """True when *record* carries a result frozen at finish time.

    The nested ``result`` object is written when a run ends. A record without
    it is either still running (and then it is never read as a result) or an
    older one whose summary was already capped for the store.
    """
    return isinstance((record or {}).get("result"), dict)

def _child_thread_row_missing(thread_id: str) -> bool:
    """True when a thread has no index row at all.

    ``transcript_reader`` resolves a thread to its transcript THROUGH the
    index row, and a row that does not exist makes every reader answer "thread
    not found" - even for a child whose transcript file is complete on disk.
    Telling that case apart from a damaged file matters to the caller: a
    missing row is fixed by reading the journal, a broken file by retrying the
    reader.

    A store failure answers False: the unknown case must fall back to the
    ordinary error, never be reported as a missing row.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return False
    try:
        from storage.repository_devagent import repo_devagent_load_thread_meta
        return not repo_devagent_load_thread_meta(tid)
    except Exception:  # pragma: no cover - an unreadable store is not an answer
        return False

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
