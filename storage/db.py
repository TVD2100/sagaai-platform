"""
storage.db - SQLAlchemy engine and Session factories.

Two separate databases:
  - Main DB (sagaai.db): assistants, chat threads, config, instructions.
  - DevAgent DB (devagent.db): DevAgent threads and messages only.

Each has its own engine and session factory, isolated from the other.
DB file locations are read from core.paths (overridable via SAGAAI_DATA_DIR).
"""
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session as _Session
from sqlalchemy.orm import sessionmaker

from storage.models import Base  # noqa: F401 - re-exported for the baseline revision


@contextmanager
def _begin(bind):
    """Yield a connection that runs DDL in a write transaction.

    The migration helpers below are called from two places: an engine (a
    transport starting the platform directly) and an open connection (an
    Alembic revision, which owns the transaction and must not open a second
    one on the same file - SQLite would deadlock against itself). One helper
    covers both: for an engine it opens and commits a transaction, for a
    connection it yields that connection and leaves the commit to the caller.

    Parameters:
        bind: an ``Engine`` or a ``Connection``.

    Yields:
        A ``Connection`` the caller may execute on.
    """
    if isinstance(bind, Connection):
        yield bind
        return
    with bind.begin() as connection:
        yield connection


@contextmanager
def _connect(bind):
    """Yield a read connection, reusing the caller's one when given a connection."""
    if isinstance(bind, Connection):
        yield bind
        return
    with bind.connect() as connection:
        yield connection


# SQLite journal mode: WAL lets readers run concurrently with a single
# writer, which removes the read/write contention that froze the history
# page while another browser tab kept writing messages.
_WAL_JOURNAL_MODE = "wal"
_BUSY_TIMEOUT_MS = 5000
_MESSAGE_INDEX_NAME = "ix_messages_thread_id_id"


def _is_file_db(db_path: str) -> bool:
    """True when *db_path* points at an on-disk SQLite file (not memory)."""
    return bool(str(db_path).strip()) and str(db_path) != ":memory:"


def _configure_sqlite_connection(dbapi_connection, connection_record, *, enforce_foreign_keys: bool = True) -> None:
    """Apply per-connection PRAGMAs on every checked-out SQLite connection.

    ``busy_timeout`` replaces the default immediate "database is locked"
    failure with a short wait, so a write from another tab no longer aborts
    a concurrent read. ``foreign_keys`` is enforced at the SQLite level only
    for the MAIN database (the ORM does not enable it automatically); the
    DevAgent database reuses ``assistant_id`` to store orchestrator slugs,
    which have no matching ``assistants`` row, so FK checks stay off there.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        if enforce_foreign_keys:
            cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _enable_wal(engine, db_path: str) -> str:
    """Switch a file-backed database to WAL journal mode (persistent).

    Must run before the first write transaction on the database. Uses a raw
    DBAPI connection because ``PRAGMA journal_mode`` cannot be changed inside
    an active transaction. The mode is recorded in the database file itself
    so later processes/connections inherit it.
    """
    if not _is_file_db(db_path):
        return ""
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute(f"PRAGMA journal_mode={_WAL_JOURNAL_MODE}")
        row = cursor.fetchone()
        mode = str(row[0]).lower() if row else ""
    finally:
        raw.close()
    return mode


def _ensure_message_index(engine) -> None:
    """Create the messages(thread_id, id) index on legacy databases.

    Fresh databases get the index from the ORM model's ``__table_args__``;
    this migration covers databases created before the index existed so
    per-thread message loads no longer scan the whole table.

    Args:
        engine: an ``Engine`` (startup path) or a ``Connection`` (an Alembic
            revision, which owns the transaction).
    """
    with _connect(engine) as conn:
        has_table = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).scalar()
        if not has_table:
            return  # a legacy database without the table: nothing to index
        has_index = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (_MESSAGE_INDEX_NAME,),
        ).scalar()
    if has_index:
        return
    with _begin(engine) as conn:
        conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {_MESSAGE_INDEX_NAME} "
            "ON messages (thread_id, id)"
        )

def _ensure_marketplace_tables(engine) -> None:
    """Create the marketplace registry tables on any database.

    Fresh databases get no marketplace tables from the ORM models - the
    feature stores its state through raw SQL, exactly like the message
    index migration above. The create is idempotent and wrapped in
    ``try/except``: a failure here must never block ``get_engine()`` and
    stop the whole application from starting.

    ``runtime_db_id`` is TEXT, not INTEGER: the primary keys of
    ``assistants`` and ``orchestrators`` are ``String(8)``, so an integer
    column could never hold them.
    """
    statements = (
        """
        CREATE TABLE IF NOT EXISTS marketplace_installed (
            type                      TEXT NOT NULL,
            id                        TEXT NOT NULL,
            runtime_slug              TEXT NOT NULL,
            runtime_db_id             TEXT,
            runtime_skill_id          TEXT,
            fork_of                   TEXT,
            version                   TEXT NOT NULL,
            channel                   TEXT NOT NULL,
            source_url                TEXT NOT NULL,
            package_sha256            TEXT,
            tree_sha256               TEXT,
            index_sha256              TEXT,
            files_json                TEXT NOT NULL DEFAULT '{}',
            skipped_code_files_json   TEXT NOT NULL DEFAULT '[]',
            installed_at              TEXT NOT NULL,
            updated_at                TEXT NOT NULL,
            installed_by              TEXT NOT NULL,
            user_modified             INTEGER NOT NULL DEFAULT 0,
            user_modified_files_json  TEXT NOT NULL DEFAULT '[]',
            install_id                TEXT,
            bundle_id                 TEXT,
            pinned                    INTEGER NOT NULL DEFAULT 0,
            license_accepted_at       TEXT,
            agent_kind                TEXT,
            PRIMARY KEY (type, id),
            UNIQUE (type, runtime_slug)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS marketplace_removed (
            type         TEXT NOT NULL,
            id           TEXT NOT NULL,
            runtime_slug TEXT NOT NULL,
            removed_at   TEXT NOT NULL,
            via          TEXT NOT NULL,
            PRIMARY KEY (type, id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_marketplace_installed_slug "
        "ON marketplace_installed (type, runtime_slug)",
    )
    try:
        with _begin(engine) as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)
            # An installation created before the column existed carries no
            # kind; it is an orchestrator, which is what the platform had.
            columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(marketplace_installed)"
                ).fetchall()
            }
            if "agent_kind" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE marketplace_installed ADD COLUMN agent_kind TEXT"
                )
    except Exception as exc:  # pragma: no cover - defensive path
        import logging

        logging.getLogger(__name__).warning(
            "marketplace tables migration failed: %s", exc
        )


# DevAgent DB connection hook: busy_timeout without FK checks (assistant_id
# stores orchestrator slugs there, not assistants.id values).
def _configure_devagent_connection(dbapi_connection, connection_record) -> None:
    """Connect hook for the DevAgent DB: busy_timeout only (no FK checks)."""
    _configure_sqlite_connection(
        dbapi_connection, connection_record, enforce_foreign_keys=False
    )


# Module-level engines (created lazily on first call)
_engine = None
_devagent_engine = None
_SessionLocal = None
_DevAgentSessionLocal = None

# Columns added to the threads table in newer builds. They are nullable and
# safe to add via ALTER TABLE without discarding existing rows.
_THREAD_EXTRA_COLUMNS = [
    ("workspace",   "VARCHAR(1024)"),
    ("target_file", "VARCHAR(1024)"),
    ("assistant_id",   "VARCHAR(8)"),
    ("assistant_name", "VARCHAR(256)"),
    # Transcript index. The message text itself lives in a per-thread
    # append-only file; these columns describe it, so a thread list never
    # touches a transcript and a growing transcript never grows the DB.
    # All nullable on purpose: NULL marks a legacy thread whose messages are
    # still in the ``messages`` table, and the reader falls back to it.
    ("parent_thread_id",   "VARCHAR(64)"),
    ("kind",               "VARCHAR(32)"),
    ("message_count",      "INTEGER"),
    ("total_bytes",        "INTEGER"),
    ("last_message_at",    "VARCHAR(32)"),
    ("tokens_in",          "INTEGER"),
    ("tokens_out",         "INTEGER"),
    ("tokens_cache",       "INTEGER"),
    ("transcript_path",    "VARCHAR(1024)"),
    ("transcript_format",  "VARCHAR(16)"),
    ("transcript_bytes",   "INTEGER"),
    ("content_schema",     "INTEGER"),
]

#: Indexes over the transcript-index columns. Counting and listing dialogs is
#: the hottest read of this table; without an index every list of children of
#: one parent scans every thread row in the database.
_THREAD_EXTRA_INDEXES = [
    ("ix_threads_parent_thread_id", "threads(parent_thread_id)"),
    ("ix_threads_kind",             "threads(kind)"),
]

# Columns added to the assistants table in newer builds.
_ASSISTANT_EXTRA_COLUMNS = [
    ("slug", "VARCHAR(64)"),
    ("reasoning_effort", "VARCHAR(32)"),
]

# Legacy thread column names (old "skill" terminology) that are renamed
# in-place to the new "assistant" names during startup migration.
_THREAD_LEGACY_COLUMN_RENAMES = [
    ("skill_id", "assistant_id"),
    ("skill_name", "assistant_name"),
]


# Legacy table name that is renamed to the new one on startup.
_LEGACY_ASSISTANT_TABLE_NAME = "skills"
_NEW_ASSISTANT_TABLE_NAME = "assistants"


def _db_url(db_path: str) -> str:
    """Return the SQLite database URL for a given path."""
    return f"sqlite:///{db_path}"


def _ensure_thread_columns(engine) -> None:
    """Add missing nullable ``threads`` columns without dropping existing data.

    SQLite supports ``ALTER TABLE ADD COLUMN`` for nullable columns, so this is
    much safer than the old drop-and-recreate migration used for the ``type``
    column.
    """
    insp = inspect(engine)
    if "threads" not in insp.get_table_names():
        return  # Table will be created by Base.metadata.create_all().

    existing = {col["name"] for col in insp.get_columns("threads")}
    missing = [(name, decl) for name, decl in _THREAD_EXTRA_COLUMNS if name not in existing]
    if missing:
        with _begin(engine) as conn:
            for name, decl in missing:
                conn.execute(text(
                    f"ALTER TABLE threads ADD COLUMN {name} {decl}"))
    # Outside the ``missing`` guard on purpose: a database migrated before the
    # indexes existed has every column and no index, and must still get them.
    _ensure_thread_indexes(engine)


def _ensure_thread_indexes(engine) -> None:
    """Create the transcript-index indexes if they are absent.

    ``CREATE INDEX IF NOT EXISTS`` is idempotent, so this runs on every start
    without a migration ledger. It is separate from the column pass because a
    database migrated before the indexes existed must still receive them.
    """
    try:
        with _begin(engine) as conn:
            for name, target in _THREAD_EXTRA_INDEXES:
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))
    except Exception:  # pragma: no cover - an index is an optimisation
        pass


def _ensure_assistant_columns(engine) -> None:
    """Add missing nullable ``assistants`` columns without dropping data.

    Used for the ``slug`` column introduced with the folder-based assistant
    storage. SQLite allows adding nullable columns in place.
    """
    insp = inspect(engine)
    table_name = _NEW_ASSISTANT_TABLE_NAME
    if table_name not in set(insp.get_table_names()):
        return  # Table will be created by Base.metadata.create_all().

    existing = {col["name"] for col in insp.get_columns(table_name)}
    missing = [(name, decl) for name, decl in _ASSISTANT_EXTRA_COLUMNS if name not in existing]
    if not missing:
        return

    with _begin(engine) as conn:
        for name, decl in missing:
            conn.execute(text(f"ALTER TABLE assistants ADD COLUMN {name} {decl}"))


def _migrate_thread_skill_columns(engine) -> None:
    """Rename legacy ``skill_id``/``skill_name`` columns to the new names.

    Runs before ``create_all`` so the ORM schema matches the on-disk schema.
    Existing data is preserved. On fresh databases this is a no-op.
    """
    insp = inspect(engine)
    if "threads" not in insp.get_table_names():
        return

    existing = {col["name"] for col in insp.get_columns("threads")}
    renames = [
        (old, new)
        for old, new in _THREAD_LEGACY_COLUMN_RENAMES
        if old in existing and new not in existing
    ]
    if not renames:
        return

    with _begin(engine) as conn:
        for old, new in renames:
            conn.execute(text(f"ALTER TABLE threads RENAME COLUMN {old} TO {new}"))


def _migrate_assistant_table(engine) -> None:
    """Rename the legacy ``skills`` table to ``assistants`` (if needed).

    Runs before ``create_all`` so the ORM reuses the migrated table instead
    of creating a fresh empty one. Existing assistant rows are preserved.
    """
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if _LEGACY_ASSISTANT_TABLE_NAME in tables and _NEW_ASSISTANT_TABLE_NAME not in tables:
        with _begin(engine) as conn:
            conn.execute(text(
                f"ALTER TABLE {_LEGACY_ASSISTANT_TABLE_NAME} "
                f"RENAME TO {_NEW_ASSISTANT_TABLE_NAME}"
            ))


def _ensure_schedules_tables(engine) -> None:
    """Create the scheduled-run tables on any database.

    Same contract as ``_ensure_marketplace_tables``: raw SQL, idempotent,
    and wrapped so a failure here can never stop the application from
    starting. The feature stores its state outside the ORM on purpose -
    the due-point claim needs a single atomic ``UPDATE ... WHERE`` and a
    ``BEGIN IMMEDIATE`` transaction, which the ORM session does not model.

    ``schedule_runs`` carries the idempotency key ``UNIQUE(schedule_id,
    planned_at)``: one scheduled point may produce at most one history row no
    matter how often a tick repeats, how many processes run, or how the
    process died between claiming and starting.
    """
    statements = (
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id                    TEXT PRIMARY KEY,
            title                 TEXT NOT NULL DEFAULT '',
            agent_slug            TEXT NOT NULL DEFAULT '',
            task_text             TEXT NOT NULL DEFAULT '',
            brief_text            TEXT NOT NULL DEFAULT '',
            workspace             TEXT NOT NULL DEFAULT '',
            first_run_at          INTEGER NOT NULL DEFAULT 0,
            ends_at               INTEGER,
            interval_value        INTEGER NOT NULL DEFAULT 1,
            interval_unit         TEXT NOT NULL DEFAULT 'days',
            tz                    TEXT NOT NULL DEFAULT 'Europe/Moscow',
            blocked_weekdays      TEXT NOT NULL DEFAULT '',
            blocked_ranges_json   TEXT NOT NULL DEFAULT '[]',
            quiet_hours           TEXT NOT NULL DEFAULT '',
            exception_policy      TEXT NOT NULL DEFAULT 'skip',
            max_shift_seconds     INTEGER NOT NULL DEFAULT 604800,
            misfire_policy        TEXT NOT NULL DEFAULT 'skip',
            misfire_grace_seconds INTEGER NOT NULL DEFAULT 3600,
            overlap_policy        TEXT NOT NULL DEFAULT 'skip',
            max_runs              INTEGER,
            max_runs_per_day      INTEGER,
            max_steps             INTEGER NOT NULL DEFAULT 60,
            budget_tokens         INTEGER NOT NULL DEFAULT 0,
            timeout_seconds       INTEGER NOT NULL DEFAULT 900,
            max_depth             INTEGER NOT NULL DEFAULT 0,
            next_run_at           INTEGER,
            last_run_at           INTEGER,
            last_status           TEXT NOT NULL DEFAULT '',
            last_error_code       TEXT NOT NULL DEFAULT '',
            status                TEXT NOT NULL DEFAULT 'active',
            run_count             INTEGER NOT NULL DEFAULT 0,
            fail_count            INTEGER NOT NULL DEFAULT 0,
            consecutive_failures  INTEGER NOT NULL DEFAULT 0,
            lock_owner            TEXT,
            lock_expires_at       INTEGER,
            created_at            INTEGER NOT NULL DEFAULT 0,
            updated_at            INTEGER NOT NULL DEFAULT 0,
            created_by            TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS schedule_runs (
            id              TEXT PRIMARY KEY,
            schedule_id     TEXT NOT NULL,
            planned_at      INTEGER NOT NULL DEFAULT 0,
            trigger         TEXT NOT NULL DEFAULT 'schedule',
            status          TEXT NOT NULL DEFAULT 'started',
            started_at      INTEGER NOT NULL DEFAULT 0,
            finished_at     INTEGER,
            run_id          TEXT,
            thread_id       TEXT,
            stop_reason     TEXT NOT NULL DEFAULT '',
            error_code      TEXT NOT NULL DEFAULT '',
            reason          TEXT NOT NULL DEFAULT '',
            steps_used      INTEGER NOT NULL DEFAULT 0,
            tokens_in       INTEGER NOT NULL DEFAULT 0,
            tokens_out      INTEGER NOT NULL DEFAULT 0,
            tokens_cache    INTEGER NOT NULL DEFAULT 0,
            summary         TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_schedules_due ON schedules (status, next_run_at)",
        "CREATE INDEX IF NOT EXISTS ix_schedules_agent ON schedules (agent_slug, status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_schedule_runs_point "
        "ON schedule_runs (schedule_id, planned_at)",
        "CREATE INDEX IF NOT EXISTS ix_schedule_runs_sched "
        "ON schedule_runs (schedule_id, planned_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_schedule_runs_open "
        "ON schedule_runs (schedule_id, status)",
    )
    try:
        with _begin(engine) as conn:
            for statement in statements:
                conn.execute(text(statement))
    except Exception:  # pragma: no cover - scheduling is an optional feature
        pass


def _ensure_sandbox_tables(engine) -> None:
    """Create the sandbox run journal table on any database.

    Same contract as ``_ensure_schedules_tables``: raw SQL, idempotent, and
    wrapped so a failure here can never stop the application from starting.
    The journal stores what was planned for a run and what came out of it;
    a refused run is a row too, because "no record" would otherwise mean
    both "never attempted" and "attempted and blocked".
    """
    statements = (
        """
        CREATE TABLE IF NOT EXISTS sandbox_runs (
            id                  TEXT PRIMARY KEY,
            tool                TEXT NOT NULL DEFAULT '',
            profile             TEXT NOT NULL DEFAULT '',
            isolation           TEXT NOT NULL DEFAULT 'none',
            provider            TEXT NOT NULL DEFAULT '',
            request_fingerprint TEXT NOT NULL DEFAULT '',
            thread_id           TEXT NOT NULL DEFAULT '',
            workspace           TEXT NOT NULL DEFAULT '',
            limits_json         TEXT NOT NULL DEFAULT '{}',
            denied              INTEGER NOT NULL DEFAULT 0,
            ok                  INTEGER NOT NULL DEFAULT 0,
            returncode          INTEGER,
            duration_ms         INTEGER NOT NULL DEFAULT 0,
            timed_out           INTEGER NOT NULL DEFAULT 0,
            cancelled           INTEGER NOT NULL DEFAULT 0,
            error               TEXT NOT NULL DEFAULT '',
            log_path            TEXT NOT NULL DEFAULT '',
            stdout_tail         TEXT NOT NULL DEFAULT '',
            stderr_tail         TEXT NOT NULL DEFAULT '',
            started_at          INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_sandbox_runs_started "
        "ON sandbox_runs (started_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_sandbox_runs_thread "
        "ON sandbox_runs (thread_id, started_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_sandbox_runs_tool "
        "ON sandbox_runs (tool, started_at DESC)",
    )
    try:
        with _begin(engine) as conn:
            for statement in statements:
                conn.execute(text(statement))
            # A journal created by an earlier release lacks the columns added
            # later; CREATE TABLE IF NOT EXISTS would leave it without them.
            columns = _sandbox_columns(conn)
            for name, ddl in (("cancelled",
                               "ALTER TABLE sandbox_runs ADD COLUMN "
                               "cancelled INTEGER NOT NULL DEFAULT 0"),):
                if name not in columns:
                    conn.execute(text(ddl))
    except Exception:  # pragma: no cover - the journal is an optional feature
        pass


def _sandbox_table_exists(connection) -> bool:
    """Return True when the journal table exists on *connection*."""
    from sqlalchemy import text

    row = connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='sandbox_runs'")
    ).first()
    return row is not None


def _sandbox_columns(connection) -> set:
    """Return the column names the journal table currently has."""
    from sqlalchemy import text

    try:
        rows = connection.execute(text("PRAGMA table_info(sandbox_runs)")).all()
    except Exception:  # pragma: no cover - a non-SQLite bind has no PRAGMA
        return set()
    return {str(row[1]) for row in rows}


def get_engine():
    """Return (and lazily create) the shared SQLAlchemy engine for the main DB.

    The schema is brought to the current revision by ``storage.migrations``:
    the revision chain owns every structural change, and this function only
    opens the engine and applies the per-connection pragmas. A database that
    already sits at ``head`` skips the migration entirely, so a normal start
    pays one read of ``alembic_version``.
    """
    global _engine
    if _engine is None:
        from core.paths import DB_PATH
        _migrate_main_database(DB_PATH)
        _engine = create_engine(
            _db_url(DB_PATH),
            connect_args={"check_same_thread": False},
            echo=False,
        )
        event.listen(_engine, "connect", _configure_sqlite_connection)
        _enable_wal(_engine, DB_PATH)
        _ensure_sandbox_tables(_engine)
        _prune_sandbox_journal(_engine)
    return _engine


def _prune_sandbox_journal(engine) -> None:
    """Drop journal rows nobody will read again (best effort, once per start).

    The journal records every run, including the repeated ones a debugging
    session produces, so an unbounded table would grow with the work it is
    meant to describe. The newest rows survive regardless of age, and a
    failure here is ignored: retention is housekeeping, not a gate.
    """
    try:
        from storage import sandbox_journal

        sandbox_journal.purge_old(engine)
    except Exception:  # pragma: no cover - housekeeping must never block a start
        pass


def _migrate_main_database(db_path: str) -> None:
    """Bring ``sagaai.db`` to the current schema revision before opening it.

    A failure is not swallowed: a database that cannot reach the revision the
    code expects would fail later in a place that cannot explain itself.
    """
    from storage import migrations

    if migrations.is_current(db_path):
        return
    report = migrations.upgrade(db_path, migrations.MAIN_KIND)
    if not report.get("ok"):
        raise RuntimeError("schema migration failed for %s: %s"
                           % (db_path, report.get("error") or report.get("action")))



def get_devagent_engine():
    """Return (and lazily create) the SQLAlchemy engine for the DevAgent DB.

    This is a completely separate SQLite database file (devagent.db)
    that stores only DevAgent threads and messages. Its schema follows the
    same revision chain as the main database; the revisions that create the
    marketplace and schedule tables skip this file.
    """
    global _devagent_engine
    if _devagent_engine is None:
        from core.paths import DEVAGENT_DB_PATH
        _migrate_devagent_database(DEVAGENT_DB_PATH)
        _devagent_engine = create_engine(
            _db_url(DEVAGENT_DB_PATH),
            connect_args={"check_same_thread": False},
            echo=False,
        )
        event.listen(_devagent_engine, "connect", _configure_devagent_connection)
        _enable_wal(_devagent_engine, DEVAGENT_DB_PATH)
    return _devagent_engine


def _migrate_devagent_database(db_path: str) -> None:
    """Bring ``devagent.db`` to the current schema revision before opening it."""
    from storage import migrations

    if migrations.is_current(db_path):
        return
    report = migrations.upgrade(db_path, migrations.DEVAGENT_KIND)
    if not report.get("ok"):
        raise RuntimeError("schema migration failed for %s: %s"
                           % (db_path, report.get("error") or report.get("action")))



def _migrate_threads_table_if_needed(engine) -> None:
    """Drop and recreate the ``threads`` table if it lacks the ``type`` column.

    A schema from before the ``type`` column cannot be extended in place (the
    column is NOT NULL without a default), so the table is dropped and the
    baseline revision recreates it. This runs inside the schema history, so it
    happens once per database instead of on every start.

    Args:
        engine: an ``Engine`` or a ``Connection`` (a revision).
    """
    insp = inspect(engine)
    if "threads" not in insp.get_table_names():
        return  # table doesn't exist yet - the baseline revision creates it

    columns = {col["name"] for col in insp.get_columns("threads")}
    if "type" in columns:
        return  # already migrated

    # The connect event enforces foreign_keys=ON, but this legacy drop must
    # run with foreign_keys=OFF (the messages.thread_id FK would block the
    # DROP). A raw DBAPI connection is used because the PRAGMA is a no-op
    # inside an already-started transaction; a revision's connection already
    # carries the pragma off (see storage/migrations/env.py).
    if isinstance(engine, Connection):
        engine.exec_driver_sql("DROP TABLE threads")
        return
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("DROP TABLE threads")
        raw.commit()
    finally:
        raw.close()


def get_session() -> _Session:
    """Return a new SQLAlchemy session for the MAIN database."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)
    return _SessionLocal()


def get_devagent_session() -> _Session:
    """Return a new SQLAlchemy session for the DevAgent database."""
    global _DevAgentSessionLocal
    if _DevAgentSessionLocal is None:
        _DevAgentSessionLocal = sessionmaker(bind=get_devagent_engine(), autocommit=False, autoflush=False)
    return _DevAgentSessionLocal()


def reset_engine() -> None:
    """
    Drop and recreate module-level engine / session factory for the MAIN DB.
    Used in tests to point at a fresh in-memory or tmp database.
    """
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None


def reset_devagent_engine() -> None:
    """
    Drop and recreate module-level engine / session factory for the DevAgent DB.
    Used in tests to isolate DevAgent DB from main DB.
    """
    global _devagent_engine, _DevAgentSessionLocal
    _devagent_engine = None
    _DevAgentSessionLocal = None
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
