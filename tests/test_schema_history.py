"""Tests of the versioned schema history.

The history is the only thing that changes a database structure now, so this
file checks the three properties the installation depends on:

* a fresh database reaches ``head`` and holds the schema the ORM declares;
* a database started at an older revision upgrades without losing a row and
  a second run changes nothing;
* the two installation databases differ only in the feature tables that
  belong to the main one.

The tests call ``storage.migrations`` directly instead of the console: the
revision chain is the unit under test, and a subprocess would only add the
argument parser to the failure output.
"""
import sqlite3

import pytest
from alembic import command
from sqlalchemy import create_engine

from storage import migrations


@pytest.fixture()
def db_path(tmp_path):
    """Return a path for a database that does not exist yet."""
    return str(tmp_path / "sagaai.db")


def table_names(path):
    """Return the sorted table names of one database file."""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def column_names(path, table):
    """Return the column names of one table."""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("PRAGMA table_info(%s)" % table).fetchall()
    finally:
        connection.close()
    return {str(row[1]) for row in rows}


def index_names(path):
    """Return the index names of one database file."""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


def seed_rows(path):
    """Write one row into each core table and return what was written."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO assistants (id, name, service, model, temperature, "
            "description, prompt_text, tools, created_at, updated_at) "
            "VALUES ('a1', 'Assistant', 'svc', 'model', 0.5, '', 'prompt', "
            "'[]', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO threads (thread_id, assistant_name, title, type, "
            "created_at, updated_at) VALUES ('t1', 'Assistant', 'Title', "
            "'chat', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO messages (thread_id, role, content, ts, file_name, "
            "file_chars) VALUES ('t1', 'user', 'hello', '2026-01-01', '', 0)"
        )
        connection.execute(
            "INSERT INTO config_kv (key, value) VALUES ('ui_lang', 'English')"
        )
        connection.commit()
    finally:
        connection.close()


def read_rows(path):
    """Return the rows seeded by :func:`seed_rows`, sorted for comparison."""
    connection = sqlite3.connect(path)
    try:
        return {
            "assistants": connection.execute(
                "SELECT id, name FROM assistants ORDER BY id").fetchall(),
            "threads": connection.execute(
                "SELECT thread_id, title FROM threads ORDER BY thread_id").fetchall(),
            "messages": connection.execute(
                "SELECT thread_id, content FROM messages ORDER BY id").fetchall(),
            "config": connection.execute(
                "SELECT key, value FROM config_kv ORDER BY key").fetchall(),
        }
    finally:
        connection.close()


# ─── The chain itself ───────────────────────────────────────────────────────


def test_the_chain_is_linear_and_has_a_head():
    """Every revision has exactly one parent, and the head is the last one."""
    order = migrations.revision_history()
    assert len(order) >= 8
    assert migrations.head_revision() == order[-1]
    assert order[0] == "0001"


def test_a_fresh_database_reaches_head(db_path):
    """Creating a database is an ``upgrade head``, not a ``create_all``."""
    report = migrations.upgrade(db_path, migrations.MAIN_KIND)
    assert report["ok"], report["error"]
    assert report["action"] == "created"
    assert report["after"] == migrations.head_revision()


def test_a_fresh_database_holds_the_whole_schema(db_path):
    """The ORM tables and the raw-SQL feature tables all exist at head.

    ``create_all`` is the baseline revision now, so a table missing here means
    the history does not reproduce the schema the code expects.
    """
    assert migrations.upgrade(db_path, migrations.MAIN_KIND)["ok"]
    tables = table_names(db_path)
    for name in ("threads", "assistants", "messages", "config_kv",
                 "instructions", "orchestrators", "orchestrator_instructions",
                 "marketplace_installed", "marketplace_removed",
                 "schedules", "schedule_runs"):
        assert name in tables, "the baseline is missing %s" % name
    assert migrations.VERSION_TABLE in tables


def test_a_fresh_database_has_the_indexes_the_startup_used_to_create(db_path):
    """The indexes the twelve startup statements added are part of the chain."""
    assert migrations.upgrade(db_path, migrations.MAIN_KIND)["ok"]
    names = index_names(db_path)
    for name in ("ix_messages_thread_id_id", "ix_threads_kind",
                 "ix_threads_parent_thread_id", "ux_schedule_runs_point",
                 "ix_marketplace_installed_slug"):
        assert name in names, "the baseline is missing index %s" % name


def test_a_fresh_database_has_every_transcript_column(db_path):
    """The transcript index columns reach a fresh database through the chain."""
    assert migrations.upgrade(db_path, migrations.MAIN_KIND)["ok"]
    columns = column_names(db_path, "threads")
    for name in ("workspace", "target_file", "parent_thread_id", "kind",
                 "message_count", "total_bytes", "last_message_at",
                 "tokens_in", "tokens_out", "tokens_cache", "transcript_path",
                 "transcript_format", "transcript_bytes", "content_schema"):
        assert name in columns, "missing column %s" % name


def test_the_agent_database_gets_no_feature_tables(db_path):
    """The registry and the scheduler belong to ``sagaai.db`` alone."""
    agent_path = db_path.replace("sagaai.db", "devagent.db")
    report = migrations.upgrade(agent_path, migrations.DEVAGENT_KIND)
    assert report["ok"], report["error"]
    tables = table_names(agent_path)
    assert "threads" in tables and "messages" in tables
    assert "marketplace_installed" not in tables
    assert "schedules" not in tables


# ─── Upgrading a database from an older revision ────────────────────────────


def test_an_upgrade_from_two_revisions_back_keeps_every_row(db_path):
    """N-2 -> head preserves the data; the acceptance criterion of the task."""
    history = migrations.revision_history()
    old = history[-3]
    assert migrations.upgrade(db_path, migrations.MAIN_KIND)["ok"]
    seed_rows(db_path)
    before = read_rows(db_path)

    command.downgrade(migrations.make_config(db_path, migrations.MAIN_KIND), old)
    assert migrations.current_revision(db_path) == old

    report = migrations.upgrade(db_path, migrations.MAIN_KIND)
    assert report["ok"], report["error"]
    assert report["before"] == old
    assert report["after"] == migrations.head_revision()
    assert read_rows(db_path) == before


def test_a_second_upgrade_changes_no_row(db_path):
    """Repeating the migration is a no-op, not a second rewrite."""
    assert migrations.upgrade(db_path, migrations.MAIN_KIND)["ok"]
    seed_rows(db_path)
    before = read_rows(db_path)

    report = migrations.upgrade(db_path, migrations.MAIN_KIND)
    assert report["ok"], report["error"]
    assert report["action"] == "unchanged"
    assert report["before"] == report["after"]
    assert read_rows(db_path) == before


def test_an_unversioned_database_adopts_the_history(db_path, tmp_path,
                                                   monkeypatch):
    """A database created before the history is stamped, not rebuilt.

    This is the shape every existing installation has: the tables are there
    and ``alembic_version`` is not. The baseline must recognise it and leave
    the data alone.

    A seeded message is expected to LEAVE the ``messages`` table: the chat
    archiver moves the dialogue into a per-thread transcript before the chain
    runs, which is the layout this build reads. The test therefore checks the
    row by its destination, not by its old place.
    """
    from core import paths as paths_mod
    from storage import db as storage_db

    history_dir = tmp_path / "history"
    monkeypatch.setattr(paths_mod, "HISTORY_DIR", str(history_dir))

    engine = create_engine("sqlite:///" + db_path)
    try:
        storage_db.Base.metadata.create_all(engine)
        storage_db._ensure_thread_columns(engine)
        storage_db._ensure_assistant_columns(engine)
        storage_db._ensure_message_index(engine)
        storage_db._ensure_marketplace_tables(engine)
        storage_db._ensure_schedules_tables(engine)
    finally:
        engine.dispose()
    seed_rows(db_path)
    before = read_rows(db_path)
    assert before["messages"] == [("t1", "hello")]
    assert migrations.current_revision(db_path) == ""

    report = migrations.upgrade(db_path, migrations.MAIN_KIND)
    assert report["ok"], report["error"]
    assert report["after"] == migrations.head_revision()

    after = read_rows(db_path)
    assert after["assistants"] == before["assistants"]
    assert after["threads"] == before["threads"]
    assert after["config"] == before["config"]
    assert after["messages"] == [], "the chat archiver should own the dialogue now"

    connection = sqlite3.connect(db_path)
    try:
        transcript_path, transcript_format = connection.execute(
            "SELECT transcript_path, transcript_format FROM threads "
            "WHERE thread_id = 't1'").fetchone()
    finally:
        connection.close()
    assert transcript_path and transcript_format == "jsonl", \
        "the thread did not receive a transcript index"
    stored = history_dir / transcript_path
    assert stored.is_file(), "the transcript file is missing"
    assert "hello" in stored.read_text(encoding="utf-8")


def test_a_legacy_database_is_repaired_by_the_first_revision(tmp_path):
    """The old table and column names are moved forward, data preserved."""
    path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE skills (id VARCHAR(8) PRIMARY KEY, name VARCHAR(256), "
            "service VARCHAR(128), model VARCHAR(128), temperature FLOAT, "
            "description TEXT, prompt_text TEXT, tools TEXT, "
            "max_tool_calls INTEGER, max_tokens INTEGER, created_at VARCHAR(32), "
            "updated_at VARCHAR(32))"
        )
        connection.execute(
            "INSERT INTO skills (id, name) VALUES ('s1', 'Legacy')"
        )
        connection.execute(
            "CREATE TABLE threads (thread_id VARCHAR(64) PRIMARY KEY, "
            "skill_id VARCHAR(8), skill_name VARCHAR(256), title VARCHAR(256), "
            "type VARCHAR(32), created_at VARCHAR(32), updated_at VARCHAR(32))"
        )
        connection.execute(
            "INSERT INTO threads (thread_id, skill_name, title) "
            "VALUES ('t1', 'Legacy', 'Old dialog')"
        )
        connection.commit()
    finally:
        connection.close()

    report = migrations.upgrade(path, migrations.MAIN_KIND)
    assert report["ok"], report["error"]

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT id, name FROM assistants").fetchall() == [("s1", "Legacy")]
        row = connection.execute(
            "SELECT thread_id, assistant_name, title FROM threads").fetchone()
    finally:
        connection.close()
    assert row == ("t1", "Legacy", "Old dialog")


# ─── Reporting ──────────────────────────────────────────────────────────────


def test_the_revision_status_counts_the_missing_revisions(db_path):
    """``behind`` is what the diagnostic prints; it must be the real count."""
    history = migrations.revision_history()
    old = history[-2]
    assert migrations.upgrade(db_path, migrations.MAIN_KIND)["ok"]
    command.downgrade(migrations.make_config(db_path, migrations.MAIN_KIND), old)

    status = migrations.revision_status(db_path)
    assert status["current"] == old
    assert status["head"] == migrations.head_revision()
    assert status["behind"] == 1
    assert status["versioned"] is True


def test_is_current_is_false_for_an_absent_database(tmp_path):
    """The startup check must not treat a missing file as current."""
    assert migrations.is_current(str(tmp_path / "missing.db")) is False


def test_is_current_is_true_only_at_head(db_path):
    """``is_current`` is the fast path of the startup, so it must be exact."""
    assert migrations.upgrade(db_path, migrations.MAIN_KIND)["ok"]
    assert migrations.is_current(db_path) is True

    history = migrations.revision_history()
    command.downgrade(migrations.make_config(db_path, migrations.MAIN_KIND),
                      history[-2])
    assert migrations.is_current(db_path) is False


def test_an_unknown_database_kind_is_refused(tmp_path):
    """A typo in the kind must not silently target the main database."""
    path = str(tmp_path / "typo.db")
    with pytest.raises(ValueError):
        command.upgrade(migrations.make_config(path, "mainframe"), "head")
