# -*- coding: utf-8 -*-
"""tests/test_chat_db_archive.py - the switch of assistant dialogues.

Every test builds a real SQLite file, because the whole point of the module is
how it treats a database ON DISK: the layout stamp, the write-ahead log, the
archive contents, and the exact moment a message row may leave the table.

The central guarantee under test: a message reaches its transcript file before
its row is removed, and a failure anywhere leaves the table readable.
"""
import os
import sqlite3
import zipfile

import pytest

from core import chat_db_archive as A


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_db(path: str, threads: int = 2, messages: int = 3,
             version: int = 0, with_path: bool = False) -> str:
    """Create a database shaped like a real main database."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE threads (thread_id TEXT PRIMARY KEY, title TEXT, "
            "type TEXT, transcript_path TEXT, assistant_name TEXT)"
        )
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "thread_id TEXT, role TEXT, content TEXT, ts TEXT, "
            "file_name TEXT, file_chars INTEGER)"
        )
        con.execute("CREATE TABLE assistants (id TEXT PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO assistants VALUES ('a1', 'Assistant')")
        for i in range(threads):
            con.execute(
                "INSERT INTO threads (thread_id, title, type, transcript_path, "
                "assistant_name) VALUES (?, ?, 'chat', ?, 'Assistant')",
                ("t%d" % i, "title %d" % i,
                 ("t%d/transcript.jsonl" % i) if with_path else None),
            )
        for i in range(messages):
            con.execute(
                "INSERT INTO messages (thread_id, role, content, ts, file_name, "
                "file_chars) VALUES ('t0', 'user', ?, ?, '', 0)",
                ("message %d" % i, "2026-11-02T10:00:%02d" % i),
            )
        con.execute("PRAGMA user_version=%d" % version)
        con.commit()
    finally:
        con.close()
    return path


def _version(path: str) -> int:
    """Return the layout stamp of a database file."""
    con = sqlite3.connect(path)
    try:
        return int(con.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        con.close()


def _count(path: str, table: str, where: str = "") -> int:
    """Return the row count of one table, optionally filtered."""
    con = sqlite3.connect(path)
    try:
        sql = "SELECT count(*) FROM %s" % table
        if where:
            sql += " WHERE " + where
        return int(con.execute(sql).fetchone()[0])
    finally:
        con.close()


def _row(path: str, sql: str, params=()):
    """Return the first row of one query as a tuple."""
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Point the platform at a fresh data dir with its own main database.

    The app modules are dropped and re-imported inside the block and restored
    afterwards: a plain reload would leave the module objects pointing at this
    temporary folder for every later test in the run.
    """
    from tests._test_isolation import isolated_app_modules as _isolate

    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    with _isolate():
        import storage.db as db_mod
        db_mod.reset_engine()
        try:
            yield tmp_path
        finally:
            db_mod.reset_engine()


# ── inspect_database ─────────────────────────────────────────────────────────


def test_a_missing_database_is_reported_and_not_created(tmp_path):
    """Looking at a missing file must not create an empty one."""
    path = str(tmp_path / "sagaai.db")

    report = A.inspect_database(path)

    assert report["exists"] is False
    assert report["legacy"] is False
    assert not os.path.exists(path)


def test_threads_with_messages_in_the_table_are_legacy(tmp_path):
    """A dialogue whose messages live in the table is the old layout."""
    path = _make_db(str(tmp_path / "sagaai.db"))

    report = A.inspect_database(path, with_messages=True)

    assert report["version"] == 0
    assert report["threads"] == 2
    assert report["threads_with_messages"] == 1
    assert report["messages"] == 3
    assert report["legacy"] is True


def test_a_database_whose_threads_all_have_paths_is_not_legacy(tmp_path):
    """An already-current file is left alone even though its stamp is old."""
    path = _make_db(str(tmp_path / "sagaai.db"), with_path=True)

    report = A.inspect_database(path)

    assert report["threads_with_messages"] == 0
    assert report["legacy"] is False


def test_an_empty_database_is_not_legacy(tmp_path):
    """Nothing to preserve means nothing to migrate."""
    path = _make_db(str(tmp_path / "sagaai.db"), threads=0, messages=0)

    report = A.inspect_database(path)

    assert report["threads"] == 0
    assert report["legacy"] is False


def test_an_unreadable_file_is_reported_instead_of_migrated(tmp_path):
    """A damaged database must be preserved, never rewritten."""
    path = str(tmp_path / "sagaai.db")
    with open(path, "wb") as fh:
        fh.write(b"this is not a database" * 100)

    report = A.inspect_database(path)

    assert report["error"]
    assert report["legacy"] is False


def test_a_path_with_spaces_and_non_ascii_is_opened(tmp_path):
    """Real installations live under names like this; a raw URI would break."""
    folder = tmp_path / "Яндекс.Диск"
    folder.mkdir()
    path = _make_db(str(folder / "sagaai.db"))

    report = A.inspect_database(path)

    assert report["error"] == ""
    assert report["threads"] == 2


# ── default_archive_path ─────────────────────────────────────────────────────


def test_the_first_archive_takes_the_plain_name(tmp_path):
    """The archive sits beside the database under the obvious name."""
    db_path = str(tmp_path / "sagaai.db")

    assert A.default_archive_path(db_path) == db_path + ".zip"


def test_a_second_archive_never_overwrites_the_first(tmp_path):
    """A restored backup must not destroy the archive of the first switch."""
    db_path = str(tmp_path / "sagaai.db")
    with open(db_path + ".zip", "wb") as fh:
        fh.write(b"previous archive")

    second = A.default_archive_path(db_path)

    assert second != db_path + ".zip"
    assert second.startswith(db_path)
    assert second.endswith(".zip")


# ── archive_database ─────────────────────────────────────────────────────────


def test_archiving_keeps_every_row_and_removes_nothing(tmp_path):
    """The archive is a faithful copy; removal is a separate, later step."""
    db_path = _make_db(str(tmp_path / "sagaai.db"), threads=2, messages=5)

    report = A.archive_database(db_path)

    assert report["ok"] is True
    assert report["archive_path"] == db_path + ".zip"
    assert report["bytes"] > 0
    assert os.path.exists(db_path)
    assert _count(db_path, "messages") == 5

    restored = str(tmp_path / "restored")
    os.makedirs(restored)
    with zipfile.ZipFile(report["archive_path"]) as zf:
        zf.extract("sagaai.db", restored)
    assert _count(os.path.join(restored, "sagaai.db"), "messages") == 5
    assert _count(os.path.join(restored, "sagaai.db"), "assistants") == 1


def test_archiving_a_missing_database_fails_cleanly(tmp_path):
    """No file means no archive, and no exception either."""
    report = A.archive_database(str(tmp_path / "absent.db"))

    assert report["ok"] is False
    assert report["error"]


def test_messages_from_the_write_ahead_log_reach_the_archive(tmp_path):
    """A message that exists only in the -wal file must not be lost."""
    db_path = _make_db(str(tmp_path / "sagaai.db"), messages=1)
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO messages (thread_id, role, content, ts, file_name, "
            "file_chars) VALUES ('t0', 'user', 'in the log', '', '', 0)"
        )
        writer.commit()
        assert os.path.exists(db_path + "-wal")

        report = A.archive_database(db_path)
    finally:
        writer.close()

    assert report["ok"] is True
    restored = str(tmp_path / "restored")
    os.makedirs(restored)
    with zipfile.ZipFile(report["archive_path"]) as zf:
        zf.extract("sagaai.db", restored)
    assert _count(os.path.join(restored, "sagaai.db"), "messages") == 2


def test_a_truncated_archive_leaves_the_database_in_place(tmp_path, monkeypatch):
    """Verification runs before removal: a bad zip must not cost the data."""
    db_path = _make_db(str(tmp_path / "sagaai.db"), messages=3)
    monkeypatch.setattr(A, "_verify_archive", lambda archive, db: "simulated corruption")

    report = A.archive_database(db_path)

    assert report["ok"] is False
    assert _count(db_path, "messages") == 3
    assert not os.path.exists(db_path + ".zip")


def test_an_existing_identical_archive_is_reused(tmp_path):
    """A hand-made zip of the same file is the backup; a second copy is waste."""
    db_path = _make_db(str(tmp_path / "sagaai.db"), messages=2)
    manual = db_path + ".zip"
    with zipfile.ZipFile(manual, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_path, arcname=os.path.basename(db_path))
    before = os.path.getsize(manual)

    assert A.existing_identical_archive(db_path) == manual

    report = A.ensure_database_format(db_path, data_dir=str(tmp_path))

    assert report["action"] == "migrated"
    assert report["archive_path"] == manual
    assert os.path.getsize(manual) == before


def test_a_stale_archive_is_never_reused(tmp_path):
    """An archive of DIFFERENT data must not pass as a copy of this file."""
    db_path = _make_db(str(tmp_path / "sagaai.db"), messages=1)
    manual = db_path + ".zip"
    with zipfile.ZipFile(manual, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(os.path.basename(db_path), b"an unrelated older database")

    assert A.existing_identical_archive(db_path) == ""


# ── take_notice ──────────────────────────────────────────────────────────────


def test_the_notice_is_returned_once_and_then_forgotten(tmp_path):
    """A finished migration is announced once, not on every rerun."""
    data_dir = str(tmp_path)
    A._write_notice({"archive_path": "/x/sagaai.db.zip", "threads": 3,
                     "messages": 70}, data_dir)

    first = A.take_notice(data_dir)
    second = A.take_notice(data_dir)

    assert first["archive_path"] == "/x/sagaai.db.zip"
    assert first["threads"] == 3
    assert second == {}


def test_a_missing_notice_is_an_empty_dict(tmp_path):
    """A start without a migration says nothing."""
    assert A.take_notice(str(tmp_path)) == {}


def test_a_corrupt_notice_file_is_ignored(tmp_path):
    """Damaged JSON must not break the start of the application."""
    path = os.path.join(str(tmp_path), A.NOTICE_FILE_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")

    assert A.take_notice(str(tmp_path)) == {}


# ── ensure_database_format ───────────────────────────────────────────────────


def test_a_missing_database_is_created_and_stamped(tmp_path):
    """A first start gets a file already marked as current layout."""
    path = str(tmp_path / "sagaai.db")

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "created"
    assert os.path.exists(path)
    assert _version(path) == A.CHAT_DB_FORMAT_VERSION


def test_a_legacy_database_is_migrated_to_files(tmp_path):
    """The real switch: rows to transcript files, index filled, rows dropped."""
    path = _make_db(str(tmp_path / "sagaai.db"), threads=2, messages=3)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "migrated"
    assert report["threads"] == 1, "only the thread that had messages is migrated"
    assert report["messages"] == 3
    assert report["archive_path"] == path + ".zip"
    assert _version(path) == A.CHAT_DB_FORMAT_VERSION

    # The messages left the table, the index points at the file.
    assert _count(path, "messages") == 0
    row = _row(path, "SELECT transcript_path, message_count, content_schema "
                     "FROM threads WHERE thread_id = 't0'")
    assert row[0] == "t0/transcript.jsonl"
    assert row[1] == 3
    assert row[2] == A.CHAT_DB_FORMAT_VERSION


# ── the switch keeps what must not be lost ───────────────────────────────────


def test_other_tables_survive_the_switch(tmp_path):
    """Assistants and settings live in this database and must not be touched."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=2)

    A.ensure_database_format(path, data_dir=str(tmp_path))

    assert _count(path, "assistants") == 1
    assert _count(path, "threads") == 2, "a thread row is never deleted"


def test_the_archive_holds_the_messages_the_switch_moved(tmp_path):
    """The zip is the only copy of the table rows after the switch."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=4)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    restored = str(tmp_path / "restored")
    os.makedirs(restored)
    with zipfile.ZipFile(report["archive_path"]) as zf:
        zf.extract("sagaai.db", restored)
    assert _count(os.path.join(restored, "sagaai.db"), "messages") == 4
    assert _count(path, "messages") == 0


def test_the_transcript_file_holds_every_message(tmp_path):
    """A reader must get the whole dialogue from the file after the switch."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=3)

    A.ensure_database_format(path, data_dir=str(tmp_path))

    from dev_agent import transcript as T
    texts = [r["content"] for r in T.iter_records("t0")]
    assert texts == ["message 0", "message 1", "message 2"]


def test_the_notice_names_the_archive_and_the_counts(tmp_path):
    """The user-facing message reports what was saved, not just that it ran."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=5)

    A.ensure_database_format(path, data_dir=str(tmp_path))
    notice = A.take_notice(str(tmp_path))

    assert notice["archive_path"] == path + ".zip"
    assert notice["threads"] == 1
    assert notice["messages"] == 5
    assert notice["created_at"]


def test_a_second_start_finds_nothing_to_do(tmp_path):
    """The switch is idempotent: the stamped file has no table-backed rows."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=2)
    first = A.ensure_database_format(path, data_dir=str(tmp_path))
    assert first["action"] == "migrated"
    assert A.take_notice(str(tmp_path))["messages"] == 2

    again = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert again["action"] == "none"
    assert A.take_notice(str(tmp_path)) == {}


def test_a_legacy_database_without_messages_is_stamped_not_archived(tmp_path):
    """An empty file needs no archive: stamping it keeps the same file."""
    path = _make_db(str(tmp_path / "sagaai.db"), threads=0, messages=0)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "stamped"
    assert _version(path) == A.CHAT_DB_FORMAT_VERSION
    assert not os.path.exists(path + ".zip")


def test_a_current_database_is_never_touched(tmp_path):
    """An installation that already runs on the file store keeps its file."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=2,
                    version=A.CHAT_DB_FORMAT_VERSION, with_path=True)
    before = os.path.getsize(path)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "none"
    assert os.path.getsize(path) == before
    assert _count(path, "messages") == 2


# ── failure modes ────────────────────────────────────────────────────────────


def test_a_failed_archive_removes_no_row(tmp_path, monkeypatch):
    """No archive means no removal - the previous dialogues stay usable."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=4)
    monkeypatch.setattr(A, "_verify_archive",
                        lambda archive, db: "simulated corruption")

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "error"
    assert _count(path, "messages") == 4, "the old rows must still be there"
    assert A.take_notice(str(tmp_path)) == {}


def test_a_failed_transcript_write_removes_no_row(tmp_path, monkeypatch):
    """A row may only leave the table once its file exists."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=3)

    def _boom(*args, **kwargs):
        return {"ok": False, "path": "", "count": 0, "bytes": 0,
                "error": "disk full"}

    from core import thread_mirror as mirror
    monkeypatch.setattr(mirror, "rewrite_messages", _boom)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "error"
    assert _count(path, "messages") == 3, "a failed write must not cost a row"
    assert A.take_notice(str(tmp_path)) == {}


def test_an_unreadable_database_is_left_alone(tmp_path):
    """A damaged file is reported, not rewritten: it may be recoverable."""
    path = str(tmp_path / "sagaai.db")
    with open(path, "wb") as fh:
        fh.write(b"not a database" * 50)
    before = os.path.getsize(path)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "error"
    assert os.path.getsize(path) == before


def test_a_lock_held_by_another_connection_aborts_the_switch(tmp_path):
    """A second running instance must never lose messages to a live archive."""
    path = _make_db(str(tmp_path / "sagaai.db"), messages=4)
    blocker = sqlite3.connect(path, timeout=5)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        report = A.ensure_database_format(path, data_dir=str(tmp_path))
    finally:
        blocker.rollback()
        blocker.close()

    assert report["action"] == "error"
    assert "in use" in report["error"]
    assert _count(path, "messages") == 4
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
