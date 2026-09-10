# -*- coding: utf-8 -*-
"""tests/test_db_concurrency.py - SQLite hardening regression tests (Level 1).

Covers the session-break / history-freeze fixes:
- the messages(thread_id, id) index is created on a fresh DB and migrated
  into a legacy DB;
- every checked-out MAIN-DB connection receives busy_timeout + foreign_keys,
  while DevAgent-DB connections get busy_timeout only (assistant_id stores
  orchestrator slugs there, so FK checks would reject every thread insert);
- file-backed databases run in WAL journal mode;
- the planner uses the index for per-thread message loads.
"""
import os
import sqlite3
import sys

import pytest

from sqlalchemy import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _reset_engines():
    """Ensure no engine survives between tests."""
    yield
    import storage.db as db
    db.reset_engine()
    db.reset_devagent_engine()


def _fresh_engine(tmp_path, monkeypatch, isolated_app_modules):
    """Point SAGAAI_DATA_DIR at a temp dir and build the main engine."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    import core.paths as paths
    from storage import db
    return db.get_engine(), paths.DB_PATH


def test_fresh_db_creates_message_index(tmp_path, monkeypatch,
                                       isolated_app_modules):
    """Fresh databases get ix_messages_thread_id_id from the ORM model."""
    eng, _db_path = _fresh_engine(tmp_path, monkeypatch, isolated_app_modules)
    try:
        indexes = {ix["name"] for ix in inspect(eng).get_indexes("messages")}
        assert "ix_messages_thread_id_id" in indexes
        # Both columns participate, in order.
        ix = next(i for i in inspect(eng).get_indexes("messages")
                  if i["name"] == "ix_messages_thread_id_id")
        assert list(ix["column_names"]) == ["thread_id", "id"]
    finally:
        eng.dispose()


def test_legacy_db_migrates_index(tmp_path, monkeypatch,
                                  isolated_app_modules):
    """A legacy DB without the index gets it on next engine startup."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    import core.paths as paths

    legacy = paths.DB_PATH
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    con = sqlite3.connect(legacy)
    con.execute(
        "CREATE TABLE threads (thread_id VARCHAR(64) PRIMARY KEY, "
        "assistant_id VARCHAR(8), assistant_name VARCHAR(256) NOT NULL "
        "DEFAULT '', title VARCHAR(256) NOT NULL DEFAULT '', "
        "created_at VARCHAR(32) NOT NULL DEFAULT '', "
        "updated_at VARCHAR(32) NOT NULL DEFAULT '')"
    )
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "thread_id VARCHAR(64) NOT NULL REFERENCES threads(thread_id) "
        "ON DELETE CASCADE, role VARCHAR(32) NOT NULL DEFAULT 'user', "
        "content TEXT NOT NULL DEFAULT '', ts VARCHAR(32) NOT NULL DEFAULT '', "
        "file_name VARCHAR(256) NOT NULL DEFAULT '', "
        "file_chars INTEGER NOT NULL DEFAULT 0)"
    )
    con.execute("INSERT INTO threads (thread_id, assistant_name) "
                "VALUES ('t1', 'Dev')")
    con.execute("INSERT INTO messages (thread_id, role, content) "
                "VALUES ('t1', 'user', 'hello')")
    con.commit()
    con.close()

    eng, _db_path = _fresh_engine(tmp_path, monkeypatch, isolated_app_modules)
    try:
        with sqlite3.connect(legacy) as con:
            names = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )}
        assert "ix_messages_thread_id_id" in names
    finally:
        eng.dispose()


def test_connections_get_pragmas(tmp_path, monkeypatch,
                                 isolated_app_modules):
    """Every MAIN-DB connection grants busy_timeout and foreign_keys pragmas."""
    eng, _db_path = _fresh_engine(tmp_path, monkeypatch, isolated_app_modules)
    try:
        with eng.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        eng.dispose()


def test_devagent_connections_skip_foreign_keys(tmp_path, monkeypatch,
                                                isolated_app_modules):
    """DevAgent-DB connections get busy_timeout but keep foreign_keys OFF.

    The DevAgent database stores ORCHESTRATOR SLUGS in threads.assistant_id;
    those slugs have no matching assistants row, so enabling FK checks would
    reject every thread insert (regression caught by the full suite).
    """
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    import storage.db as db
    eng = db.get_devagent_engine()
    try:
        with eng.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
        # The insert that failed while FK checks leaked into this engine.
        from storage.repository_devagent import repo_devagent_create_thread
        assert repo_devagent_create_thread(
            "tid_slug", "title", "orchestrator_x", "Custom",
            workspace=None, target_file=None,
        ) is True
    finally:
        eng.dispose()


def test_file_db_uses_wal(tmp_path, monkeypatch, isolated_app_modules):
    """File-backed databases persist WAL journal mode."""
    eng, db_path = _fresh_engine(tmp_path, monkeypatch, isolated_app_modules)
    try:
        with sqlite3.connect(db_path) as con:
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        eng.dispose()


def test_query_plan_uses_index(tmp_path, monkeypatch,
                               isolated_app_modules):
    """Per-thread message loads use the index, not a full scan."""
    eng, db_path = _fresh_engine(tmp_path, monkeypatch, isolated_app_modules)
    try:
        from storage.repository import repo_create_thread, repo_append_message
        repo_create_thread("t1", None, "Dev")
        repo_append_message("t1", "user", "hello")
        with sqlite3.connect(db_path) as con:
            plan = con.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM messages "
                "WHERE thread_id='t1' ORDER BY id"
            ).fetchall()
        assert any("USING INDEX ix_messages_thread_id_id" in str(row)
                   for row in plan), plan
    finally:
        eng.dispose()
