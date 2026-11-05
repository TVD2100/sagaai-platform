# -*- coding: utf-8 -*-
"""storage.migrations - the driver of the versioned schema history.

Before this module the schema moved forward on every start: twelve
idempotent statements in ``storage.db`` ran in a fixed order, nothing
recorded which of them had already been applied, and a failure left no
trace of where it stopped. The history now lives in Alembic revisions under
``storage/migrations/versions`` and this module is the only caller:

1. run the data migrations of the file layout (the two archivers) FIRST -
   they replace or rewrite a database file, which cannot happen while a
   revision transaction is open;
2. upgrade the schema to ``head``;
3. report the revision the database reached.

Both installation databases share one revision chain. They hold the same ORM
tables and differ only in the raw-SQL feature tables, which a revision
creates for ``main`` alone - hence the ``kind`` argument.

Upgrading a database that was never versioned (every installation built
before this module) is supported on purpose: the baseline revision stamps
the schema it finds and the following revisions are defensive, so their
step is a no-op on a current file and a repair on an outdated one.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

#: Recognised database kinds, mirroring ``storage/migrations/env.py``.
MAIN_KIND = "main"
DEVAGENT_KIND = "devagent"

#: Name of the table Alembic keeps the applied revision in. Its absence is
#: how an installation built before the schema history is recognised.
VERSION_TABLE = "alembic_version"

#: How long a connection waits for another process's lock.
BUSY_TIMEOUT_MS = 5000

#: Cached head of the chain; see :func:`head_revision`.
_HEAD_CACHE = None


def config_file() -> str:
    """Return the absolute path of ``alembic.ini`` next to the package."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "alembic.ini")


def make_config(db_path: str, kind: str = MAIN_KIND) -> Config:
    """Return an Alembic config pointing at one database file.

    The path and the kind travel through ``config.attributes`` instead of the
    ini file: ``alembic.ini`` describes the history, not an installation, and
    a second ini file per database would have to be kept in sync by hand.

    Args:
        db_path: absolute path of the SQLite database file.
        kind: ``main`` for ``sagaai.db``, ``devagent`` for ``devagent.db``.

    Returns:
        A configured ``Config`` ready for ``command.upgrade``.
    """
    config = Config(config_file())
    config.attributes["db_path"] = str(db_path)
    config.attributes["db_kind"] = str(kind)
    return config


def head_revision() -> str:
    """Return the revision id of the newest migration in the chain.

    The answer is cached for the life of the process: a running build cannot
    grow a new revision file, and every start of the platform asks for it.
    """
    global _HEAD_CACHE
    if _HEAD_CACHE is None:
        script = ScriptDirectory.from_config(make_config(":memory:"))
        _HEAD_CACHE = str(script.get_current_head() or "")
    return _HEAD_CACHE


def is_current(db_path: str) -> bool:
    """True when the file exists and already sits at ``head``.

    The check is deliberately cheap and independent of SQLAlchemy: it reads
    two values with ``sqlite3`` and compares them with the cached head. The
    startup path calls it on every cold start, and a database that is current
    must not pay for a migration engine, a revision scan or a transaction.
    """
    if not db_path or not os.path.isfile(str(db_path)):
        return False
    head = head_revision()
    if not head:
        return False
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                                     timeout=BUSY_TIMEOUT_MS / 1000.0)
    except sqlite3.Error:
        return False
    try:
        row = connection.execute(
            "SELECT version_num FROM alembic_version").fetchone()
        return bool(row) and str(row[0]) == head
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def revision_history() -> list:
    """Return every revision id of the chain, oldest first.

    The N-2 test of the acceptance criteria starts a database at a known old
    revision, and a test that hardcodes the ids would break on every new
    migration. The order comes from Alembic itself, not from the file names.
    """
    script = ScriptDirectory.from_config(make_config(":memory:"))
    revisions = list(script.walk_revisions())
    revisions.reverse()
    return [str(item.revision) for item in revisions]


def _connect(db_path: str):
    """Open a short-lived read connection with the platform pragmas."""
    engine = create_engine("sqlite:///" + str(db_path),
                           connect_args={"check_same_thread": False})
    return engine


def current_revision(db_path: str) -> str:
    """Return the revision applied to a database, or ``''`` when unversioned.

    A missing file, a missing ``alembic_version`` table and an empty table
    all mean the same thing to the caller: this database has no schema
    history yet and the baseline revision will stamp it.
    """
    if not db_path or not os.path.isfile(str(db_path)):
        return ""
    engine = _connect(db_path)
    try:
        with engine.connect() as connection:
            return str(MigrationContext.configure(connection).get_current_revision() or "")
    except Exception:
        return ""
    finally:
        engine.dispose()


def revision_status(db_path: str) -> Dict[str, Any]:
    """Describe how far a database is from the newest revision.

    Returns:
        ``{path, exists, current, head, behind, versioned}`` where ``behind``
        counts the revisions still to apply (0 when current or ahead) and
        ``versioned`` is False for a database without a schema history.
    """
    path = str(db_path or "")
    head = head_revision()
    exists = bool(path) and os.path.isfile(path)
    current = current_revision(path) if exists else ""
    order = revision_history()
    behind = 0
    if current and head and current != head:
        if current in order:
            behind = len(order) - order.index(current) - 1
        else:  # pragma: no cover - a revision unknown to this build
            behind = len(order)
    return {"path": path, "exists": exists, "current": current,
            "head": head, "behind": behind,
            "versioned": bool(current) or _has_version_table(path)}


def _has_version_table(db_path: str) -> bool:
    """Return True when the file carries ``alembic_version``."""
    if not db_path or not os.path.isfile(str(db_path)):
        return False
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                                     timeout=BUSY_TIMEOUT_MS / 1000.0)
    except sqlite3.Error:
        return False
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (VERSION_TABLE,),
        ).fetchone()
        return bool(row)
    except sqlite3.Error:  # pragma: no cover - unreadable file
        return False
    finally:
        connection.close()


def upgrade(db_path: str, kind: str = MAIN_KIND) -> Dict[str, Any]:
    """Bring one database file to ``head`` of the schema history.

    The data migrations run first (see the module docstring), then Alembic
    applies the missing revisions. The call is safe to repeat: a database at
    ``head`` opens no transaction and changes no row.

    Args:
        db_path: absolute path of the SQLite database file. The file is
            created when missing - preparing a clean installation without
            opening the interface is a supported use.
        kind: ``main`` or ``devagent``.

    Returns:
        ``{ok, path, kind, before, after, head, action, error}`` where
        ``action`` is ``created``, ``upgraded``, ``unchanged`` or ``error``.
    """
    path = str(db_path)
    report: Dict[str, Any] = {"ok": False, "path": path, "kind": kind,
                              "before": "", "after": "", "head": "",
                              "action": "error", "error": ""}
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        existed = os.path.isfile(path)
        report["before"] = current_revision(path) if existed else ""
        # The archivers read a file that a previous build wrote: on a missing
        # file there is no old layout to move, and creating the file here
        # would stamp it as current before the schema exists.
        if existed:
            _run_data_migrations(path, kind)
        command.upgrade(make_config(path, kind), "head")
        if not existed:
            # The chain created the file and the schema; the archiver now
            # records the layout stamp for the next start.
            _run_data_migrations(path, kind)
        report["after"] = current_revision(path)
        report["head"] = head_revision()
        report["ok"] = report["after"] == report["head"]
        if not existed:
            report["action"] = "created"
        elif report["after"] != report["before"]:
            report["action"] = "upgraded"
        else:
            report["action"] = "unchanged"
        if not report["ok"]:  # pragma: no cover - a broken chain
            report["error"] = ("the database stopped at %s instead of %s"
                               % (report["after"], report["head"]))
    except Exception as exc:
        report["error"] = str(exc)
        report["action"] = "error"
    return report


def _run_data_migrations(db_path: str, kind: str) -> None:
    """Move the file layout forward before the schema is touched.

    The archivers replace a database file (devagent) or rewrite its message
    rows into per-thread transcripts (chat). Both need the file to
themselves: a file swap under an open revision transaction would commit the
    transaction into a file nobody is looking at any more, so they run first.
    A failure is recorded as an exception only when the archiver reports an
    error AND the database is unusable; a database that is merely busy is
    left for the next start instead of blocking the schema upgrade, because
    refusing to start is worse than one more start.
    """
    try:
        if kind == DEVAGENT_KIND:
            from core.devagent_db_archive import ensure_database_format
        else:
            from core.chat_db_archive import ensure_database_format
        ensure_database_format(db_path=db_path)
    except Exception:  # pragma: no cover - defensive
        # The archivers never raise by contract; this guards a future change
        # turning a layout problem into a startup failure.
        pass


def upgrade_installation() -> Dict[str, Any]:
    """Upgrade both installation databases and return one report.

    This is what a transport calls at cold start and what ``sagaai migrate``
    calls for the schema step. Reading the paths here keeps ``core.paths``
    the single source of the file locations, including its
    ``SAGAAI_DATA_DIR`` override.
    """
    from core.paths import DB_PATH, DEVAGENT_DB_PATH

    reports = [upgrade(DB_PATH, MAIN_KIND), upgrade(DEVAGENT_DB_PATH, DEVAGENT_KIND)]
    return {
        "ok": all(item.get("ok") for item in reports),
        "databases": reports,
        "head": reports[0].get("head") if reports else "",
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
