# -*- coding: utf-8 -*-
"""tests/scenarios/test_chat_db_archive_scenario.py - upgrading an installation.

Given an installation whose assistant dialogues live in the ``messages`` table
of the main database, when the updated build starts for the first time, then
the previous database is archived intact, every dialogue keeps working from its
transcript file, the other tables are untouched, and a second start does
nothing at all.
"""
import os
import sqlite3
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point the platform at a fresh data dir with a fresh main database.

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


def _legacy_installation(tmp_path):
    """Build the state of a previous build: one assistant, one chat thread.

    The thread is created through the ORM so the schema is exactly the one a
    real installation has, and its messages are inserted straight into the
    table - which is how every dialogue written before the file store looks.

    The layout stamp is then set back to ``0``: a build that predates the file
    store never wrote ``PRAGMA user_version``, and this is what tells the
    archiver that the rows in the table are the live dialogue rather than a
    leftover of a finished switch.
    """
    from storage.repository import repo_append_message, repo_create_thread, repo_save_thread_meta

    tid = "20260101_120000_legacy"
    assert repo_create_thread(tid, None, "Proofreader") is True
    repo_append_message(tid, "user", "check this text")
    repo_append_message(tid, "assistant", "the text is fine")
    repo_append_message(tid, "user", "thanks")
    repo_save_thread_meta(tid, {"title": "An old dialogue"})
    _unstamp(_db_path(tmp_path))
    return tid


def _unstamp(db_path):
    """Mark a database as written before the file store (layout ``0``)."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA user_version=0")
        con.commit()
    finally:
        con.close()


def _db_path(tmp_path):
    """Return the main database path of the isolated installation."""
    from core.paths import DB_PATH
    return DB_PATH


def _table_count(db_path, table):
    """Count the rows of one table in a database file."""
    con = sqlite3.connect(db_path)
    try:
        return int(con.execute("SELECT count(*) FROM %s" % table).fetchone()[0])
    finally:
        con.close()


# ── the upgrade itself ───────────────────────────────────────────────────────


def test_an_existing_installation_moves_its_dialogues_to_files(tmp_path):
    """The whole upgrade, end to end, on a realistic starting state."""
    from core import chat_db_archive as A
    tid = _legacy_installation(tmp_path)
    db_path = _db_path(tmp_path)
    assert _table_count(db_path, "messages") == 3

    report = A.ensure_database_format(db_path, data_dir=str(tmp_path))

    # Then: the database is archived, the messages are files, the row indexes.
    assert report["action"] == "migrated"
    assert report["threads"] == 1
    assert report["messages"] == 3
    assert os.path.isfile(report["archive_path"])
    assert _table_count(db_path, "messages") == 0

    from dev_agent import transcript as T
    assert [r["content"] for r in T.iter_records(tid)] == [
        "check this text", "the text is fine", "thanks"]

    from core.threads import load_thread_meta
    meta = load_thread_meta(tid)
    assert meta["transcript_path"] == "%s/transcript.jsonl" % tid
    assert meta["message_count"] == 3
    assert meta["content_schema"] == 1


def test_the_archived_database_still_opens_as_the_previous_build_knew_it(tmp_path):
    """The archive is the escape hatch: it holds the complete previous state."""
    from core import chat_db_archive as A
    _legacy_installation(tmp_path)
    db_path = _db_path(tmp_path)

    report = A.ensure_database_format(db_path, data_dir=str(tmp_path))

    restored = str(tmp_path / "restored")
    os.makedirs(restored, exist_ok=True)
    with zipfile.ZipFile(report["archive_path"]) as zf:
        zf.extract(os.path.basename(db_path), restored)
    archived_db = os.path.join(restored, os.path.basename(db_path))
    assert _table_count(archived_db, "messages") == 3
    assert _table_count(archived_db, "threads") == 1


def test_the_dialogue_reads_back_after_the_upgrade(tmp_path):
    """Given an upgraded installation, when the user opens the chat, then it
    shows exactly the conversation written by the previous build."""
    from core import chat_db_archive as A
    tid = _legacy_installation(tmp_path)

    A.ensure_database_format(_db_path(tmp_path), data_dir=str(tmp_path))

    from core.threads import load_thread_messages
    messages = load_thread_messages(tid)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [m["content"] for m in messages] == [
        "check this text", "the text is fine", "thanks"]


def test_a_second_start_does_not_touch_a_finished_installation(tmp_path):
    """The upgrade runs once; a restart must be a no-op with no new archive."""
    from core import chat_db_archive as A
    _legacy_installation(tmp_path)
    db_path = _db_path(tmp_path)
    first = A.ensure_database_format(db_path, data_dir=str(tmp_path))
    archive = first["archive_path"]
    with open(archive, "rb") as fh:
        archive_bytes = fh.read()

    second = A.ensure_database_format(db_path, data_dir=str(tmp_path))

    assert second["action"] == "none"
    with open(archive, "rb") as fh:
        assert fh.read() == archive_bytes, "the archive was rewritten"


def test_a_new_dialogue_after_the_upgrade_is_mirrored_from_the_start(tmp_path):
    """A dialogue written after the switch never lives only in the table."""
    from core import chat_db_archive as A
    _legacy_installation(tmp_path)
    A.ensure_database_format(_db_path(tmp_path), data_dir=str(tmp_path))

    from core.threads import append_thread_message, create_thread
    new_tid = create_thread(None, "Proofreader", title="A new dialogue")
    append_thread_message(new_tid, "user", "a fresh question")

    from dev_agent import transcript as T
    assert [r["content"] for r in T.iter_records(new_tid)] == ["a fresh question"]


def test_the_other_tables_are_left_untouched(tmp_path):
    """Given assistants and settings in the same file, when the switch runs,
    then only the message rows move."""
    from core import chat_db_archive as A
    from storage.repository import repo_load_config, repo_save_config
    _legacy_installation(tmp_path)
    db_path = _db_path(tmp_path)
    repo_save_config({"ui_lang": "Русский"})

    A.ensure_database_format(db_path, data_dir=str(tmp_path))

    assert repo_load_config().get("ui_lang") == "Русский"
    assert _table_count(db_path, "threads") == 1


def test_a_damaged_installation_keeps_its_dialogues(tmp_path):
    """Given an archive that cannot be verified, when the switch runs, then no
    row is removed and the messages are still readable from the table."""
    from core import chat_db_archive as A
    from core import thread_mirror as mirror
    tid = _legacy_installation(tmp_path)
    db_path = _db_path(tmp_path)

    real_rewrite = mirror.rewrite_messages

    def _fail(*args, **kwargs):
        return {"ok": False, "path": "", "count": 0, "bytes": 0,
                "error": "no space left on device"}

    mirror.rewrite_messages = _fail
    try:
        report = A.ensure_database_format(db_path, data_dir=str(tmp_path))
    finally:
        mirror.rewrite_messages = real_rewrite

    assert report["action"] == "error"
    assert _table_count(db_path, "messages") == 3
    from core.threads import load_thread_messages
    assert [m["content"] for m in load_thread_messages(tid)] == [
        "check this text", "the text is fine", "thanks"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
