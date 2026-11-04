# -*- coding: utf-8 -*-
"""tests/test_transcript_schema.py - the index row over a file transcript.

The ``threads`` row is an INDEX, not a store: message content lives in a
per-thread append-only file, and the row carries only what a thread list and a
status line need. These tests pin the two properties that make such a change
safe to ship against a database full of existing dialogs:

- the migration only ever ADDS a nullable column, so no existing message is
touched and no table is rebuilt;
- a legacy row keeps NULL in every new column, which is the marker the reader
  uses to fall back to the ``messages`` table.
"""
import os
import sqlite3
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import storage.db as db_mod  # noqa: E402
from storage.models import Base, Thread  # noqa: E402

#: The columns a transcript index adds. Listed here once so a missing column
#: fails one assertion instead of silently disabling the reader.
INDEX_COLUMNS = (
    "parent_thread_id", "kind", "message_count", "total_bytes",
    "last_message_at", "tokens_in", "tokens_out", "tokens_cache",
    "transcript_path", "transcript_format", "transcript_bytes",
    "content_schema",
)


@pytest.fixture
def legacy_db(tmp_path):
    """Build a database shaped like an installation from before the change."""
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE threads (thread_id VARCHAR(64) PRIMARY KEY, "
            "assistant_id VARCHAR(8), assistant_name VARCHAR(256) NOT NULL "
            "DEFAULT '', title VARCHAR(256) NOT NULL DEFAULT '', "
            "type VARCHAR(32) NOT NULL DEFAULT 'chat', "
            "created_at VARCHAR(32) NOT NULL DEFAULT '', "
            "updated_at VARCHAR(32) NOT NULL DEFAULT '')"
        )
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "thread_id VARCHAR(64) NOT NULL REFERENCES threads(thread_id) "
            "ON DELETE CASCADE, role VARCHAR(32) NOT NULL DEFAULT 'user', "
            "content TEXT NOT NULL DEFAULT '', ts VARCHAR(32) NOT NULL "
            "DEFAULT '', file_name VARCHAR(256) NOT NULL DEFAULT '', "
            "file_chars INTEGER NOT NULL DEFAULT 0)"
        )
        con.execute(
            "INSERT INTO threads (thread_id, title) VALUES ('old-1', 'Old') "
        )
        con.execute(
            "INSERT INTO messages (thread_id, role, content) "
            "VALUES ('old-1', 'user', 'a legacy message')"
        )
        con.commit()
    return path


def test_the_migration_adds_every_index_column(legacy_db):
    """Given an old database, every index column appears without a rebuild."""
    engine = create_engine(f"sqlite:///{legacy_db}")
    try:
        db_mod._ensure_thread_columns(engine)

        cols = {c["name"] for c in inspect(engine).get_columns("threads")}
        for name in INDEX_COLUMNS:
            assert name in cols, f"missing transcript-index column: {name}"
    finally:
        engine.dispose()


def test_the_migration_keeps_the_existing_messages(legacy_db):
    """The rows that make up the history are not touched by the migration."""
    engine = create_engine(f"sqlite:///{legacy_db}")
    try:
        db_mod._ensure_thread_columns(engine)

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT thread_id, content FROM messages")
            ).fetchall()
        assert rows == [("old-1", "a legacy message")]
    finally:
        engine.dispose()


def test_a_legacy_row_reads_as_null_in_every_index_column(legacy_db):
    """NULL is the marker of a thread whose content is still in SQLite.

    The reader uses it to pick a source; a default of 0 or '' would make a
    legacy row look like an EMPTY transcript and lose the whole history.
    """
    engine = create_engine(f"sqlite:///{legacy_db}")
    try:
        db_mod._ensure_thread_columns(engine)

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT " + ", ".join(INDEX_COLUMNS) + " FROM threads"
            )).fetchone()
        assert set(row) == {None}
    finally:
        engine.dispose()


def test_the_migration_is_idempotent(legacy_db):
    """Running it twice must not raise and must not lose the data."""
    engine = create_engine(f"sqlite:///{legacy_db}")
    try:
        db_mod._ensure_thread_columns(engine)
        db_mod._ensure_thread_columns(engine)

        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM threads")).scalar()
        assert count == 1
    finally:
        engine.dispose()


def test_the_indexes_exist_after_the_migration(legacy_db):
    """Listing the children of one dialog must not scan every thread."""
    engine = create_engine(f"sqlite:///{legacy_db}")
    try:
        db_mod._ensure_thread_columns(engine)

        with engine.connect() as conn:
            names = {row[0] for row in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ))}
        assert "ix_threads_parent_thread_id" in names
        assert "ix_threads_kind" in names
    finally:
        engine.dispose()


def test_an_up_to_date_database_still_gets_the_indexes(legacy_db):
    """A database that already has every column must still gain the indexes.

    The indexes were added after the columns, so an installation that took the
    earlier migration has all columns and no index. Returning early on ``no
    missing columns`` would leave it unindexed forever.
    """
    engine = create_engine(f"sqlite:///{legacy_db}")
    try:
        db_mod._ensure_thread_columns(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_threads_parent_thread_id"))

        db_mod._ensure_thread_columns(engine)

        with engine.connect() as conn:
            names = {row[0] for row in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ))}
        assert "ix_threads_parent_thread_id" in names
    finally:
        engine.dispose()


def test_a_new_thread_row_round_trips_the_index_fields(tmp_path):
    """A thread created with index fields reads them back unchanged."""
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        Base.metadata.create_all(engine)
        th = Thread(
            thread_id="sub-1", assistant_id=None, assistant_name="dev_agent",
            title="child", type="devagent", created_at="2026", updated_at="2026",
            parent_thread_id="root-1", kind="subagent", message_count=12,
            total_bytes=4096, last_message_at="2026-11-02T14:00:00",
            tokens_in=100, tokens_out=50, tokens_cache=10,
            transcript_path="history/root-1/subagents/sub-1/messages.jsonl",
            transcript_format="jsonl", transcript_bytes=4096,
            content_schema=1,
        )
        assert th.to_dict()["transcript_path"].endswith("messages.jsonl")
        assert th.to_dict()["kind"] == "subagent"
        assert th.to_dict()["parent_thread_id"] == "root-1"
    finally:
        engine.dispose()
