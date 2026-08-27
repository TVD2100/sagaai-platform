"""
storage.db - SQLAlchemy engine and Session factories.

Two separate databases:
  - Main DB (sagaai.db): assistants, chat threads, config, instructions.
  - DevAgent DB (devagent.db): DevAgent threads and messages only.

Each has its own engine and session factory, isolated from the other.
DB file locations are read from core.paths (overridable via SAGAAI_DATA_DIR).
"""
import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session as _Session
from storage.models import Base

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
    if not missing:
        return

    with engine.begin() as conn:
        for name, decl in missing:
            conn.execute(text(f"ALTER TABLE threads ADD COLUMN {name} {decl}"))


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

    with engine.begin() as conn:
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

    with engine.begin() as conn:
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
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {_LEGACY_ASSISTANT_TABLE_NAME} "
                f"RENAME TO {_NEW_ASSISTANT_TABLE_NAME}"
            ))


def get_engine():
    """Return (and lazily create) the shared SQLAlchemy engine for the main DB.

    If the existing ``threads`` table is missing the ``type`` column
    (e.g. schema from an older version), the table is dropped and
    re-created - safe because threads at this point are empty.
    Newer columns (``workspace``, ``target_file``) are added in place.
    Legacy "skill" naming is migrated to the new "assistant" naming with
    data preserved.
    """
    global _engine
    if _engine is None:
        from core.paths import DB_PATH
        _engine = create_engine(
            _db_url(DB_PATH),
            connect_args={"check_same_thread": False},
            echo=False,
        )
        _migrate_threads_table_if_needed(_engine)
        _migrate_assistant_table(_engine)
        _migrate_thread_skill_columns(_engine)
        Base.metadata.create_all(_engine)
        _ensure_thread_columns(_engine)
        _ensure_assistant_columns(_engine)
    return _engine


def get_devagent_engine():
    """Return (and lazily create) the SQLAlchemy engine for the DevAgent DB.

    This is a completely separate SQLite database file (devagent.db)
    that stores only DevAgent threads and messages.
    """
    global _devagent_engine
    if _devagent_engine is None:
        from core.paths import DEVAGENT_DB_PATH
        _devagent_engine = create_engine(
            _db_url(DEVAGENT_DB_PATH),
            connect_args={"check_same_thread": False},
            echo=False,
        )
        # DevAgent DB has its own threads/messages tables.
        # We only need Thread and Message models from Base.
        _migrate_thread_skill_columns(_devagent_engine)
        Base.metadata.create_all(_devagent_engine)
        _ensure_thread_columns(_devagent_engine)
    return _devagent_engine


def _migrate_threads_table_if_needed(engine) -> None:
    """Drop and recreate the ``threads`` table if it lacks the ``type`` column."""
    insp = inspect(engine)
    if "threads" not in insp.get_table_names():
        return  # table doesn't exist yet - create_all will handle it

    columns = {col["name"] for col in insp.get_columns("threads")}
    if "type" in columns:
        return  # already migrated

    # Table exists but schema is stale - drop it so create_all rebuilds.
    with engine.connect() as conn:
        conn.exec_driver_sql("DROP TABLE threads")
        conn.commit()


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
# SPDX-License-Identifier: MIT
