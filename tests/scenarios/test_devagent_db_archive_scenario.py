# -*- coding: utf-8 -*-
"""
Scenario test: what an existing installation sees when it upgrades.

Walks the real upgrade path the way a user experiences it, in given→when→then
form and against real files on disk:

1. a database written by an older build (dialogue text inside SQLite);
2. the first start after the upgrade;
3. the second start, and the notice the user is shown.

Only the public entry points are used - ``ensure_database_format`` as the
start-up calls it and ``take_notice`` as the UI calls it - so the test fails if
the two halves stop agreeing with each other, which is the actual risk here.
"""
import os
import sqlite3
import zipfile

from core import devagent_db_archive as A


def _old_installation(db_path: str, threads: int = 3) -> None:
    """Create a devagent.db exactly as an older build left it on disk."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE threads (thread_id TEXT PRIMARY KEY, "
            "assistant_id TEXT, title TEXT, type TEXT)"
        )
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "thread_id TEXT, role TEXT, content TEXT)"
        )
        for i in range(threads):
            con.execute(
                "INSERT INTO threads VALUES (?, 'dev_agent', ?, 'devagent')",
                ("t%d" % i, "диалог %d" % i),
            )
            for j in range(2):
                con.execute(
                    "INSERT INTO messages (thread_id, role, content) VALUES (?, ?, ?)",
                    ("t%d" % i, "user", "сообщение %d-%d" % (i, j)),
                )
        con.commit()
    finally:
        con.close()


def test_an_old_installation_upgrades_once_and_says_where_its_dialogues_went(tmp_path):
    """Given an old database, when the app starts, the user learns what happened."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    db_path = os.path.join(data_dir, "devagent.db")

    # ── Given: an installation from before the file store, with real dialogues
    _old_installation(db_path)
    assert os.path.getsize(db_path) > 0

    # ── When: the first start after the upgrade
    first_start = A.ensure_database_format(db_path, data_dir=data_dir)

    # ── Then: the switch happened, and the old data is reachable in a zip
    assert first_start["action"] == "archived"
    assert first_start["threads"] == 3
    assert first_start["messages"] == 6
    archive = first_start["archive_path"]
    assert archive == db_path + ".zip"
    assert os.path.isfile(archive)

    with zipfile.ZipFile(archive) as zf:
        zf.extract("devagent.db", str(tmp_path / "restored"))
    restored = str(tmp_path / "restored" / "devagent.db")
    con = sqlite3.connect(restored)
    try:
        recovered = con.execute("SELECT content FROM messages ORDER BY id").fetchall()
    finally:
        con.close()
    assert len(recovered) == 6, "the archive does not hold every message"
    assert recovered[0][0] == "сообщение 0-0"

    # ── And: the user is told where the dialogues are, in one sentence
    notice = A.take_notice(data_dir)
    assert notice["archive_path"] == archive
    assert notice["threads"] == 3
    assert notice["messages"] == 6

    # ── And: the replacement database starts empty, so new work is clean
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        con.close()
    assert tables == [], "the new database must not inherit old tables"

    # ── When: every later start happens
    for _ in range(3):
        again = A.ensure_database_format(db_path, data_dir=data_dir)
        assert again["action"] == "none"

    # ── Then: nothing is announced again and nothing is re-archived
    assert A.take_notice(data_dir) == {}
    assert os.path.isfile(archive)


def test_a_user_who_rewrites_the_database_keeps_the_first_archive(tmp_path):
    """Given an archive from an earlier switch, when an old file reappears,
    the previous archive must survive and the new one gets its own name."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    db_path = os.path.join(data_dir, "devagent.db")

    _old_installation(db_path)
    first = A.ensure_database_format(db_path, data_dir=data_dir)
    assert first["action"] == "archived"
    first_archive = first["archive_path"]
    with open(first_archive, "rb") as fh:
        original_bytes = fh.read()

    # The user restores a DIFFERENT pre-upgrade backup: two dialogues, not
    # three. Identical bytes would legitimately reuse the existing archive,
    # so the restored file has to hold different data for this scenario.
    os.remove(db_path)
    _old_installation(db_path, threads=2)

    second = A.ensure_database_format(db_path, data_dir=data_dir)

    assert second["action"] == "archived"
    assert second["threads"] == 2
    assert second["archive_path"] != first_archive
    with open(first_archive, "rb") as fh:
        assert fh.read() == original_bytes, "the first archive was overwritten"


def test_a_restored_byte_identical_backup_reuses_the_existing_archive(tmp_path):
    """Given the same database back on disk, when the switch runs again, the
    archive already holds it and no second copy is written."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    db_path = os.path.join(data_dir, "devagent.db")

    _old_installation(db_path)
    first = A.ensure_database_format(db_path, data_dir=data_dir)
    assert first["action"] == "archived"
    first_archive = first["archive_path"]
    with open(first_archive, "rb") as fh:
        original_bytes = fh.read()
    size_after_first = os.path.getsize(first_archive)

    # The same bytes come back - a restore, or a copy from another machine.
    os.remove(db_path)
    _old_installation(db_path)

    second = A.ensure_database_format(db_path, data_dir=data_dir)

    assert second["action"] == "archived"
    assert second["archive_path"] == first_archive
    assert os.path.getsize(first_archive) == size_after_first
    with open(first_archive, "rb") as fh:
        assert fh.read() == original_bytes
    assert not os.path.exists(first_archive + ".1")
    # Only one archive beside the database: no duplicate was created.
    archives = [n for n in os.listdir(data_dir) if n.startswith("devagent.db.zip")]
    assert archives == ["devagent.db.zip"]


def test_a_dialogue_written_and_never_reopened_is_archived_intact(tmp_path):
    """Given a single long dialogue, when the switch runs, every message of it
    is still readable from the archive."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    db_path = os.path.join(data_dir, "devagent.db")
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY, type TEXT)")
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "thread_id TEXT, role TEXT, content TEXT)"
        )
        con.execute("INSERT INTO threads VALUES ('long', 'devagent')")
        body = "строка с не-ASCII символами и кавычками \"ё\"\n"
        for i in range(500):
            con.execute(
                "INSERT INTO messages (thread_id, role, content) VALUES ('long', ?, ?)",
                ("user" if i % 2 == 0 else "assistant", "%d %s" % (i, body)),
            )
        con.commit()
    finally:
        con.close()

    report = A.ensure_database_format(db_path, data_dir=data_dir)
    assert report["messages"] == 500

    with zipfile.ZipFile(report["archive_path"]) as zf:
        zf.extract("devagent.db", str(tmp_path / "restored"))
    restored = str(tmp_path / "restored" / "devagent.db")
    con = sqlite3.connect(restored)
    try:
        rows = con.execute(
            "SELECT role, content FROM messages ORDER BY id").fetchall()
    finally:
        con.close()
    assert len(rows) == 500
    assert rows[0][0] == "user"
    assert "строка с не-ASCII" in rows[0][1]
    assert rows[-1][0] == "assistant"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
