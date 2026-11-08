"""
core.gateway.health - liveness, readiness and the version the API reports.

Two probes with two different questions, kept apart on purpose:

- ``health()`` - is this PROCESS alive? It answers from memory and never
  touches the database. A supervisor restarts a process that fails this.
- ``ready()`` - can this process SERVE a request? It reads the schema
  read-only. A load balancer withholds traffic while this fails.

Collapsing them into one endpoint forces a bad choice: either a live
process is restarted because the schema is still being migrated, or traffic
is served into a database that cannot answer.

The readiness probe opens the database with ``sqlite3`` in read-only mode
and NOT through ``storage.db``. That module is the executor of the schema
migrations - merely importing it creates the file and runs the migration
steps, which is exactly what the server must not do.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Dict, List, Optional

from core.gateway.dto import HealthReport

#: How long a "not ready" verdict is cached before the probe runs again. A
#: ready verdict is cached for the life of the process: a schema does not
#: regress while the process runs, and re-probing on every request would open
#: the file on a hot path.
NOT_READY_TTL_SECONDS = 5.0

#: Columns ``_ensure_thread_columns`` adds to ``threads``. Listed here rather
#: than imported from ``storage.db`` for the reason in the module docstring:
#: importing that module migrates the database.
THREAD_COLUMNS = (
    "type", "parent_thread_id", "kind", "transcript_path",
    "message_count", "workspace", "target_file", "orchestrator_slug",
    "orchestrator_name", "title", "assistant_id", "assistant_name",
)

#: Columns ``_ensure_assistant_columns`` adds to ``assistants``.
ASSISTANT_COLUMNS = ("slug", "reasoning_effort", "name_en", "name_ru", "name_zh")

#: Columns ``_ensure_orchestrator_columns`` adds to ``orchestrators``.
ORCHESTRATOR_COLUMNS = ("name_en", "name_ru", "name_zh")

#: Tables the schedule and marketplace features create with raw SQL. Their
#: absence means a migration step did not run.
REQUIRED_TABLES = ("threads", "assistants", "messages", "config_kv",
                   "orchestrators")

_PROBE_STATE: Dict[str, object] = {"ready": False, "checked_at": 0.0}


# ─── Process start ──────────────────────────────────────────────────────────


_BOOTED_AT = time.time()


def boot_time() -> float:
    """Return the epoch second this process recorded its start."""
    return float(_BOOTED_AT)


def uptime_seconds() -> float:
    """Return how long this process has been up, in seconds."""
    return max(0.0, time.time() - _BOOTED_AT)


def version() -> str:
    """Return the platform version.

    ``core.version`` holds the version as a module constant (its history
    lives in the comments above it); this wrapper exists so a caller does not
    import that 560-line module itself.
    """
    try:
        from core.version import __version__
        return str(__version__)
    except Exception:  # pragma: no cover - version is informational
        return "0.0.0"


# ─── Liveness ───────────────────────────────────────────────────────────────


def health() -> HealthReport:
    """Return a liveness report that never touches external storage."""
    return HealthReport(
        status="ok",
        pid=os.getpid(),
        uptime_seconds=uptime_seconds(),
    )


# ─── Readiness ──────────────────────────────────────────────────────────────


def _readonly_connect(path: str) -> Optional[sqlite3.Connection]:
    """Open *path* read-only, or return None when it cannot be opened.

    ``mode=ro`` guarantees the probe cannot create a missing file or write a
    journal entry into a database that belongs to another process.
    """
    if not path:
        return None
    try:
        uri = "file:%s?mode=ro" % path.replace("?", "%3f")
        return sqlite3.connect(uri, uri=True, timeout=2.0)
    except Exception:
        return None


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """Return the column names of *table*, or an empty list."""
    try:
        rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    except Exception:
        return []
    return [str(row[1]) for row in rows if len(row) > 1]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except Exception:
        return False
    return bool(row)


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
    except Exception:
        return False
    return bool(row)


def probe_schema(main_db: str = "", devagent_db: str = "") -> List[str]:
    """Return the list of missing schema pieces; an empty list means ready.

    The checks answer one question: did the migration steps of a normal
    start-up run to completion? They deliberately do not count rows, list
    entities or look at user data - a readiness probe is not a diagnostics
    endpoint and its output reaches an unauthenticated caller.
    """
    if not main_db:
        try:
            from core.paths import DB_PATH
            main_db = DB_PATH
        except Exception:
            main_db = ""
    missing: List[str] = []

    conn = _readonly_connect(main_db)
    if conn is None:
        return ["main_db_unreachable"]
    try:
        for table in REQUIRED_TABLES:
            if not _table_exists(conn, table):
                missing.append("table:" + table)
        if _table_exists(conn, "threads"):
            columns = set(_table_columns(conn, "threads"))
            for name in THREAD_COLUMNS:
                if name not in columns:
                    missing.append("threads." + name)
        if _table_exists(conn, "assistants"):
            columns = set(_table_columns(conn, "assistants"))
            for name in ASSISTANT_COLUMNS:
                if name not in columns:
                    missing.append("assistants." + name)
        if _table_exists(conn, "orchestrators"):
            columns = set(_table_columns(conn, "orchestrators"))
            for name in ORCHESTRATOR_COLUMNS:
                if name not in columns:
                    missing.append("orchestrators." + name)
        if not _index_exists(conn, "ix_messages_thread_id_id"):
            missing.append("index:ix_messages_thread_id_id")
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass

    if not devagent_db:
        try:
            from core.paths import DEVAGENT_DB_PATH
            devagent_db = DEVAGENT_DB_PATH
        except Exception:
            devagent_db = ""
    conn = _readonly_connect(devagent_db)
    if conn is None:
        missing.append("devagent_db_unreachable")
        return missing
    try:
        if not _table_exists(conn, "threads"):
            missing.append("devagent.table:threads")
        else:
            columns = set(_table_columns(conn, "threads"))
            for name in ("parent_thread_id", "kind", "transcript_path"):
                if name not in columns:
                    missing.append("devagent.threads." + name)
        if not _table_exists(conn, "messages"):
            missing.append("devagent.table:messages")
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass

    return missing


def ready(force: bool = False) -> HealthReport:
    """Return a readiness report, cached according to the policy above."""
    now = time.time()
    if not force:
        if _PROBE_STATE.get("ready"):
            return HealthReport(status="ok", version=version(),
                                pid=os.getpid(),
                                uptime_seconds=uptime_seconds(),
                                components={"db": "ready"})
        checked = float(_PROBE_STATE.get("checked_at") or 0.0)
        if checked and (now - checked) < NOT_READY_TTL_SECONDS:
            return _not_ready_report(list(_PROBE_STATE.get("missing") or []))

    missing = probe_schema()
    _PROBE_STATE["checked_at"] = now
    _PROBE_STATE["missing"] = list(missing)
    if missing:
        _PROBE_STATE["ready"] = False
        return _not_ready_report(missing)

    _PROBE_STATE["ready"] = True
    return HealthReport(status="ok", version=version(), pid=os.getpid(),
                        uptime_seconds=uptime_seconds(),
                        components={"db": "ready"})


def _not_ready_report(missing: List[str]) -> HealthReport:
    return HealthReport(
        status="not_ready",
        version=version(),
        pid=os.getpid(),
        uptime_seconds=uptime_seconds(),
        components={"db": "not_ready"},
        missing=list(missing),
        hint="Start the platform once so the schema migrations run, then retry.",
    )


def reset_cache() -> None:
    """Forget the cached readiness verdict (tests and a forced re-check)."""
    _PROBE_STATE["ready"] = False
    _PROBE_STATE["checked_at"] = 0.0
    _PROBE_STATE["missing"] = []
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
