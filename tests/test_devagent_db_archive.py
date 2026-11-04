# -*- coding: utf-8 -*-
"""
Tests for core.devagent_db_archive - the one-time switch to the file store.

Every test builds a real SQLite file, because the whole point of the module is
how it treats a database ON DISK: the layout stamp, the write-ahead log, the
archive contents and the exact moment the old file may be removed.
"""
import os
import sqlite3
import zipfile

from core import devagent_db_archive as A

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_legacy_db(path: str, threads: int = 2, messages: int = 3,
                    version: int = 0, transcripts: bool = False) -> str:
    """Create a database shaped like a real devagent.db and return its path."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE threads (thread_id TEXT PRIMARY KEY, title TEXT, "
            "type TEXT, transcript_path TEXT)"
        )
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "thread_id TEXT, role TEXT, content TEXT)"
        )
        for i in range(threads):
            con.execute(
                "INSERT INTO threads VALUES (?, ?, 'devagent', ?)",
                ("t%d" % i, "title %d" % i,
                 ("history/t%d/transcript.jsonl" % i) if transcripts else None),
            )
        for i in range(messages):
            con.execute(
                "INSERT INTO messages (thread_id, role, content) VALUES (?, ?, ?)",
                ("t0", "user", "message %d" % i),
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


def _count(path: str, table: str) -> int:
    """Return the row count of one table."""
    con = sqlite3.connect(path)
    try:
        return int(con.execute("SELECT count(*) FROM %s" % table).fetchone()[0])
    finally:
        con.close()


def _tables(path: str) -> list:
    """Return the table names present in a database file."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return sorted(str(row[0]) for row in rows)
    finally:
        con.close()


# ── inspect_database ─────────────────────────────────────────────────────────


def test_a_missing_database_is_reported_and_not_created(tmp_path):
    """Looking at a missing file must not create an empty one."""
    path = str(tmp_path / "devagent.db")

    report = A.inspect_database(path)

    assert report["exists"] is False
    assert report["legacy"] is False
    assert not os.path.exists(path)


def test_a_database_with_threads_in_the_messages_table_is_legacy(tmp_path):
    """Threads without a transcript path keep their messages in the database."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"))

    report = A.inspect_database(path, with_messages=True)

    assert report["exists"] is True
    assert report["version"] == 0
    assert report["threads"] == 2
    assert report["threads_without_transcript"] == 2
    assert report["messages"] == 3
    assert report["legacy"] is True


def test_threads_that_all_carry_a_transcript_are_not_a_legacy_layout(tmp_path):
    """An already-current file is left alone even though its stamp is old."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), transcripts=True)

    report = A.inspect_database(path)

    assert report["threads_without_transcript"] == 0
    assert report["legacy"] is False


def test_an_empty_database_is_not_a_legacy_layout(tmp_path):
    """Nothing to preserve means nothing to archive."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=0, messages=0)

    report = A.inspect_database(path)

    assert report["threads"] == 0
    assert report["legacy"] is False


def test_a_database_without_the_transcript_column_counts_every_row(tmp_path):
    """A file written before the column existed cannot be read any other way."""
    path = str(tmp_path / "devagent.db")
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY, title TEXT)")
        con.execute("INSERT INTO threads VALUES ('a', 'one')")
        con.execute("INSERT INTO threads VALUES ('b', 'two')")
        con.commit()
    finally:
        con.close()

    report = A.inspect_database(path)

    assert report["threads"] == 2
    assert report["threads_without_transcript"] == 2
    assert report["legacy"] is True


def test_a_database_without_tables_is_not_a_legacy_layout(tmp_path):
    """A file the ORM has not initialised yet carries no dialogue."""
    path = str(tmp_path / "devagent.db")
    sqlite3.connect(path).close()

    report = A.inspect_database(path)

    assert report["threads"] == 0
    assert report["legacy"] is False


def test_an_unreadable_file_is_reported_instead_of_archived(tmp_path):
    """A damaged database must be preserved, never replaced."""
    path = str(tmp_path / "devagent.db")
    with open(path, "wb") as fh:
        fh.write(b"this is not a database" * 100)

    report = A.inspect_database(path)

    assert report["error"]
    assert report["legacy"] is False


def test_a_path_with_spaces_and_non_ascii_is_opened(tmp_path):
    """Real installations live under names like this; a raw URI would break."""
    folder = tmp_path / "Яндекс.Диск"
    folder.mkdir()
    path = _make_legacy_db(str(folder / "devagent.db"))

    report = A.inspect_database(path)

    assert report["error"] == ""
    assert report["threads"] == 2


# ── default_archive_path ─────────────────────────────────────────────────────


def test_the_first_archive_takes_the_plain_name(tmp_path):
    """The archive sits beside the database under the obvious name."""
    db_path = str(tmp_path / "devagent.db")

    assert A.default_archive_path(db_path) == db_path + ".zip"


def test_a_second_archive_never_overwrites_the_first(tmp_path):
    """A restored backup must not destroy the archive of the first switch."""
    db_path = str(tmp_path / "devagent.db")
    first = db_path + ".zip"
    with open(first, "wb") as fh:
        fh.write(b"previous archive")

    second = A.default_archive_path(db_path)

    assert second != first
    assert second.startswith(db_path)
    assert second.endswith(".zip")


def test_repeated_calls_give_distinct_names(tmp_path):
    """Two archives of the same file inside one second must still differ."""
    db_path = str(tmp_path / "devagent.db")
    with open(db_path + ".zip", "wb") as fh:
        fh.write(b"first")

    first = A.default_archive_path(db_path)
    with open(first, "wb") as fh:
        fh.write(b"second")
    second = A.default_archive_path(db_path)

    assert first != second


# ── archive_database ─────────────────────────────────────────────────────────


def test_archiving_keeps_every_message_and_leaves_removal_to_the_caller(tmp_path):
    """The archive is a faithful copy; deleting the source is a separate step."""
    db_path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=2, messages=5)

    report = A.archive_database(db_path)

    assert report["ok"] is True
    assert report["error"] == ""
    assert report["archive_path"] == db_path + ".zip"
    assert report["bytes"] > 0
    assert os.path.exists(db_path), "archiving alone must never delete the source"

    restored = str(tmp_path / "restored.db")
    with zipfile.ZipFile(report["archive_path"]) as zf:
        zf.extract("devagent.db", str(tmp_path))
    os.replace(str(tmp_path / "devagent.db"), restored)
    assert _count(restored, "messages") == 5
    assert _count(restored, "threads") == 2


def test_archiving_a_missing_database_fails_cleanly(tmp_path):
    """No file means no archive, and no exception either."""
    report = A.archive_database(str(tmp_path / "absent.db"))

    assert report["ok"] is False
    assert report["error"]
    assert report["archive_path"] == ""


def test_messages_from_the_write_ahead_log_reach_the_archive(tmp_path):
    """A message that exists only in the -wal file must not be lost."""
    db_path = _make_legacy_db(str(tmp_path / "devagent.db"), messages=1)
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO messages (thread_id, role, content) VALUES ('t0', 'user', 'in the log')"
        )
        writer.commit()
        # The row lives in the -wal file until a checkpoint folds it in.
        assert os.path.exists(db_path + "-wal")

        report = A.archive_database(db_path)
    finally:
        writer.close()

    assert report["ok"] is True
    restored = str(tmp_path / "restored.db")
    with zipfile.ZipFile(report["archive_path"]) as zf:
        zf.extract("devagent.db", str(tmp_path))
    os.replace(str(tmp_path / "devagent.db"), restored)
    rows = _count(restored, "messages")
    assert rows == 2, "the row written to the log is missing from the archive"


def test_a_truncated_archive_leaves_the_database_in_place(tmp_path, monkeypatch):
    """Verification runs before removal: a bad zip must not cost the data."""
    db_path = _make_legacy_db(str(tmp_path / "devagent.db"), messages=3)
    monkeypatch.setattr(A, "_verify_archive", lambda archive, db: "simulated corruption")

    report = A.archive_database(db_path)

    assert report["ok"] is False
    assert "simulated corruption" in report["error"]
    assert os.path.exists(db_path), "the database must survive a failed archive"
    assert _count(db_path, "messages") == 3
    assert not os.path.exists(db_path + ".zip")


def test_a_failing_archive_removes_its_temporary_file(tmp_path, monkeypatch):
    """A failed attempt must not litter the data folder with partial zips."""
    db_path = _make_legacy_db(str(tmp_path / "devagent.db"))

    def _boom(archive, db):
        raise RuntimeError("verification crashed")

    monkeypatch.setattr(A, "_verify_archive", _boom)

    report = A.archive_database(db_path)

    assert report["ok"] is False
    leftovers = [n for n in os.listdir(str(tmp_path)) if n.startswith(".archive-")]
    assert leftovers == []
    assert os.path.exists(db_path)


def test_an_explicit_archive_path_is_honoured(tmp_path):
    """The caller may choose where the archive goes."""
    db_path = _make_legacy_db(str(tmp_path / "devagent.db"))
    target = str(tmp_path / "backups" / "old.zip")

    report = A.archive_database(db_path, target)

    assert report["ok"] is True
    assert report["archive_path"] == target
    assert os.path.exists(target)


# ── take_notice ──────────────────────────────────────────────────────────────


def test_the_notice_is_returned_once_and_then_forgotten(tmp_path):
    """A finished switch is announced once, not on every rerun."""
    data_dir = str(tmp_path)
    A._write_notice({"archive_path": "/x/devagent.db.zip", "threads": 3,
                     "messages": 70}, data_dir)

    first = A.take_notice(data_dir)
    second = A.take_notice(data_dir)

    assert first["archive_path"] == "/x/devagent.db.zip"
    assert first["threads"] == 3
    assert second == {}
    assert not os.path.exists(os.path.join(data_dir, A.NOTICE_FILE_NAME))


def test_a_missing_notice_is_an_empty_dict(tmp_path):
    """A start without an archive says nothing."""
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
    path = str(tmp_path / "devagent.db")

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "created"
    assert os.path.exists(path)
    assert _version(path) == A.DEVAGENT_DB_FORMAT_VERSION


def test_a_legacy_database_with_dialogues_is_archived_and_replaced(tmp_path):
    """The real switch: old file to a zip, new empty file in its place."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=3, messages=7)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "archived"
    assert report["threads"] == 3
    assert report["messages"] == 7
    assert report["archive_path"] == path + ".zip"

    # The new database is empty and already stamped, so the switch cannot run
    # a second time on the same installation. Its tables are created by the
    # ORM on the first real connection, not here.
    assert os.path.exists(path)
    assert _version(path) == A.DEVAGENT_DB_FORMAT_VERSION
    assert _tables(path) == [], "the replacement database must start empty"

    with zipfile.ZipFile(report["archive_path"]) as zf:
        assert "devagent.db" in zf.namelist()


def test_the_archive_records_the_dialogues_it_preserved(tmp_path):
    """The user-facing notice reports what was saved, not just that it happened."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=4, messages=9)

    A.ensure_database_format(path, data_dir=str(tmp_path))
    notice = A.take_notice(str(tmp_path))

    assert notice["threads"] == 4
    assert notice["messages"] == 9
    assert notice["archive_path"] == path + ".zip"
    assert notice["created_at"]


def test_a_second_start_finds_nothing_to_do(tmp_path):
    """The switch is idempotent: the new file is already current."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=2)
    first = A.ensure_database_format(path, data_dir=str(tmp_path))
    assert first["action"] == "archived"
    # The UI collects the notice on the render that follows the start-up.
    assert A.take_notice(str(tmp_path))["threads"] == 2

    again = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert again["action"] == "none"
    assert A.take_notice(str(tmp_path)) == {}, "a finished switch must stay silent"


def test_a_legacy_database_without_dialogues_is_stamped_not_replaced(tmp_path):
    """An empty file needs no archive: stamping it keeps the same file."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=0, messages=0)
    before = os.path.getsize(path)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "stamped"
    assert _version(path) == A.DEVAGENT_DB_FORMAT_VERSION
    assert not os.path.exists(path + ".zip")
    assert os.path.getsize(path) == before


def test_a_current_database_is_never_touched(tmp_path):
    """An installation that already runs on the file store keeps its file."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=2,
                           version=A.DEVAGENT_DB_FORMAT_VERSION)
    before = os.path.getsize(path)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "none"
    assert os.path.getsize(path) == before
    assert _count(path, "threads") == 2


def test_a_failed_archive_leaves_the_legacy_database_untouched(tmp_path, monkeypatch):
    """No archive means no replacement - the previous dialogues stay usable."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=2, messages=4)
    monkeypatch.setattr(A, "_verify_archive",
                        lambda archive, db: "simulated corruption")

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "error"
    assert report["error"] == "simulated corruption"
    assert _count(path, "messages") == 4, "the old database must still be readable"
    assert A.take_notice(str(tmp_path)) == {}


def test_an_unreadable_database_is_left_alone(tmp_path):
    """A damaged file is reported, not replaced: it may still be recoverable."""
    path = str(tmp_path / "devagent.db")
    with open(path, "wb") as fh:
        fh.write(b"not a database" * 50)
    before = os.path.getsize(path)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "error"
    assert os.path.getsize(path) == before
    assert not os.path.exists(path + ".zip")


def test_a_restored_legacy_file_is_archived_under_a_second_name(tmp_path):
    """Bringing back an old backup must not destroy the first archive."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=1)
    A.ensure_database_format(path, data_dir=str(tmp_path))
    first_archive = path + ".zip"
    with open(first_archive, "rb") as fh:
        first_bytes = fh.read()

    _make_legacy_db(path, threads=2)
    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "archived"
    assert report["archive_path"] != first_archive
    with open(first_archive, "rb") as fh:
        assert fh.read() == first_bytes, "the first archive was overwritten"


# ── Locking and reuse of an existing archive ─────────────────────────────────


def test_a_lock_held_by_another_connection_aborts_the_switch(tmp_path):
    """A second running instance must never lose messages to a live archive."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=2, messages=4)
    blocker = sqlite3.connect(path, timeout=5)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        report = A.ensure_database_format(path, data_dir=str(tmp_path))
    finally:
        blocker.rollback()
        blocker.close()

    assert report["action"] == "error"
    assert "in use" in report["error"]
    assert _count(path, "messages") == 4, "a locked database must stay untouched"
    assert not os.path.exists(path + ".zip")
    assert A.take_notice(str(tmp_path)) == {}


def test_an_existing_identical_archive_is_reused_not_duplicated(tmp_path):
    """A hand-made zip of the same file is the backup; a second copy is waste."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=2, messages=3)
    # Build the archive by hand, exactly as a user would with a zip tool.
    manual = path + ".zip"
    with zipfile.ZipFile(manual, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(path, arcname=os.path.basename(path))
    size_before = os.path.getsize(manual)

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "archived"
    assert report["archive_path"] == manual
    assert os.path.getsize(manual) == size_before, "the existing archive was rewritten"
    assert not os.path.exists(path + ".zip.1")
    assert _version(path) == A.DEVAGENT_DB_FORMAT_VERSION


def test_a_stale_archive_is_never_reused(tmp_path):
    """An archive of DIFFERENT data must not pass as a copy of this file."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=1, messages=1)
    manual = path + ".zip"
    with zipfile.ZipFile(manual, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(os.path.basename(path), b"an unrelated older database")

    assert A.existing_identical_archive(path) == ""

    report = A.ensure_database_format(path, data_dir=str(tmp_path))

    assert report["action"] == "archived"
    assert report["archive_path"] != manual, "a stale zip must not be taken for a backup"
    assert os.path.exists(manual), "the unrelated archive must be left in place"


def test_an_identical_archive_needs_the_same_size_and_checksum(tmp_path):
    """Byte-exact means both the length and the CRC-32 have to agree."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), threads=1)
    manual = path + ".zip"
    with zipfile.ZipFile(manual, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(path, arcname=os.path.basename(path))

    assert A.existing_identical_archive(path) == manual

    # One byte appended changes the size, so the archive stops matching.
    with open(path, "ab") as fh:
        fh.write(b"\x00")
    assert A.existing_identical_archive(path) == ""


def test_archiving_a_locked_database_reports_the_lock(tmp_path):
    """The plain archiving entry point refuses a locked file instead of copying."""
    path = _make_legacy_db(str(tmp_path / "devagent.db"), messages=2)
    blocker = sqlite3.connect(path, timeout=5)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        report = A.archive_database(path)
    finally:
        blocker.rollback()
        blocker.close()

    assert report["ok"] is False
    assert report["error"]
    assert not os.path.exists(path + ".zip")
    assert _count(path, "messages") == 2
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

