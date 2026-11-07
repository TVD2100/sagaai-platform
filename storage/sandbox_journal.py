# -*- coding: utf-8 -*-
"""Journal of sandbox runs.

Every run - including a refused one - leaves a row here. The journal answers
the two questions a user asks about generated code after the fact: what was
planned (profile, isolation, limits) and what happened (exit status,
duration, log).

A refused run is recorded on purpose. A denial is a decision of the platform
and its absence from the journal would make the guarantee unauditable: "no
record" would mean both "never attempted" and "attempted and blocked".

The table lives in ``sagaai.db`` alone: the DevAgent database holds dialogs,
not execution history.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

#: How many characters of the stdout/stderr tail a journal row keeps.
TAIL_CHARS = 4000


def _now() -> float:
    """Return the current epoch time as float (an INTEGER column stores it)."""
    return time.time()


def _tail(text: str, limit: int = TAIL_CHARS) -> str:
    """Return the last *limit* characters of *text*."""
    text = str(text or "")
    return text if len(text) <= limit else text[-limit:]


def new_run_id() -> str:
    """Return a fresh run id (short, sortable enough for a journal listing)."""
    return "sbx_" + uuid.uuid4().hex[:16]


def record_run(engine, *, run_id: str, tool: str, profile: str,
               isolation: str, provider: str, request_fingerprint: str,
               thread_id: str = "", workspace: str = "", limits: Optional[Dict[str, Any]] = None,
               denied: bool = False, ok: bool = False, returncode: Optional[int] = None,
               duration_ms: int = 0, timed_out: bool = False, cancelled: bool = False,
               error: str = "", log_path: str = "", stdout: str = "",
               stderr: str = "") -> Dict[str, Any]:
    """Insert one journal row and return it as a mapping.

    The caller passes the planned facts AND the outcome: a denied run has no
    outcome, so ``ok`` stays False and ``denied`` marks why.
    """
    row = {
        "id": str(run_id),
        "tool": str(tool or ""),
        "profile": str(profile or ""),
        "isolation": str(isolation or "none"),
        "provider": str(provider or ""),
        "request_fingerprint": str(request_fingerprint or ""),
        "thread_id": str(thread_id or ""),
        "workspace": str(workspace or ""),
        "limits_json": json.dumps(limits or {}, ensure_ascii=False, sort_keys=True),
        "denied": 1 if denied else 0,
        "ok": 1 if ok else 0,
        "returncode": returncode,
        "duration_ms": int(duration_ms or 0),
        "timed_out": 1 if timed_out else 0,
        "cancelled": 1 if cancelled else 0,
        "error": _tail(error, 1000),
        "log_path": str(log_path or ""),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
        "started_at": _now(),
    }
    insert_run(engine, row)
    return row


def insert_run(engine, row: Dict[str, Any]) -> None:
    """Write one prepared row; a journal failure never breaks the run."""
    from sqlalchemy import text

    from storage.db import _begin

    try:
        with _begin(engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO sandbox_runs (id, tool, profile, isolation, "
                    "provider, request_fingerprint, thread_id, workspace, "
                    "limits_json, denied, ok, returncode, duration_ms, "
                    "timed_out, cancelled, error, log_path, stdout_tail, "
                    "stderr_tail, started_at) VALUES (:id, :tool, :profile, "
                    ":isolation, :provider, :request_fingerprint, :thread_id, "
                    ":workspace, :limits_json, :denied, :ok, :returncode, "
                    ":duration_ms, :timed_out, :cancelled, :error, :log_path, "
                    ":stdout_tail, :stderr_tail, :started_at)"
                ),
                row,
            )
    except Exception:  # pragma: no cover - the journal is a record, not a gate
        pass


def recent_runs(engine, limit: int = 50, thread_id: str = "") -> List[Dict[str, Any]]:
    """Return the newest journal rows, optionally limited to one dialog."""
    from sqlalchemy import text

    from storage.db import _begin

    query = ("SELECT * FROM sandbox_runs"
             + (" WHERE thread_id = :tid" if thread_id else "")
             + " ORDER BY started_at DESC LIMIT :limit")
    params: Dict[str, Any] = {"limit": int(limit)}
    if thread_id:
        params["tid"] = str(thread_id)
    try:
        with _begin(engine) as conn:
            rows = conn.execute(text(query), params).mappings().all()
    except Exception:  # pragma: no cover - an unreadable journal is an empty one
        return []
    return [dict(row) for row in rows]


def purge_old(engine, keep_days: int = 30, keep_rows: int = 5000) -> int:
    """Delete journal rows older than *keep_days*, keeping the newest rows.

    The journal is a diagnostic trail, not an archive: an unbounded table
    grows with every run, including the runs a user repeats while debugging.
    The newest rows are never deleted, so a burst of runs cannot erase the
    only record that explains the current failure.

    Args:
        engine: main database engine.
        keep_days: age after which a row may be deleted.
        keep_rows: minimum number of newest rows to keep regardless of age.

    Returns:
        How many rows were deleted.
    """
    from sqlalchemy import text

    from storage.db import _begin

    cutoff = _now() - max(1, int(keep_days)) * 86400
    try:
        with _begin(engine) as conn:
            rows = conn.execute(
                text("SELECT id FROM sandbox_runs ORDER BY started_at DESC LIMIT :keep"),
                {"keep": max(0, int(keep_rows))},
            ).fetchall()
            keep_ids = {row[0] for row in rows}
            old = conn.execute(
                text("SELECT id FROM sandbox_runs WHERE started_at < :cutoff"),
                {"cutoff": cutoff},
            ).fetchall()
            doomed = [row[0] for row in old if row[0] not in keep_ids]
            for chunk_start in range(0, len(doomed), 500):
                chunk = doomed[chunk_start:chunk_start + 500]
                conn.execute(
                    text("DELETE FROM sandbox_runs WHERE id IN :ids").bindparams(
                        __import__("sqlalchemy").bindparam("ids", expanding=True)
                    ),
                    {"ids": chunk},
                )
    except Exception:  # pragma: no cover - retention is housekeeping
        return 0
    return len(doomed)


def summarise(row: Dict[str, Any]) -> str:
    """Return one human-readable line describing a journal row.

    The line is what the settings page shows in its run list; it states the
    decision (refused or executed) before the outcome, because a refusal is
    the more important fact.
    """
    tool = row.get("tool") or "run"
    profile = row.get("profile") or "-"
    if row.get("denied"):
        return f"{tool} [{profile}] refused: {row.get('error') or 'no provider'}"
    status = "ok" if row.get("ok") else f"exit {row.get('returncode')}"
    if row.get("timed_out"):
        status = "timed out"
    if row.get("cancelled"):
        status = "cancelled"
    return f"{tool} [{profile}] {status} in {int(row.get('duration_ms') or 0)} ms"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
