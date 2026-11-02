# -*- coding: utf-8 -*-
"""tests/test_transcript_chat_mirror.py - the assistant dialogue mirror.

An assistant chat stores its messages in the ``messages`` table of the main
database and mirrors them into the same append-only transcript file an
orchestrator dialog uses. The mirror is only worth having if it agrees with
the table at every moment a reader could look, and if a broken mirror never
hides a message the table still has:

- a new thread is pointed at its transcript from the first message;
- appending one message appends exactly one record and advances the index;
- a full save REPLACES the transcript, because a regenerated dialogue would
  otherwise keep showing work the user deleted;
- a thread written before the file store keeps working from the table and is
  never mirrored;
- a failed write to the file falls back to the table instead of hiding the
  newest message;
- deleting a thread takes its transcript folder with it.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point the platform at a fresh data dir with a fresh main database."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import storage.db as db_mod
    db_mod.reset_engine()

    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)

    yield tmp_path

    db_mod.reset_engine()


def _meta(tid):
    """Read the index row of a thread."""
    from core.threads import load_thread_meta
    return load_thread_meta(tid) or {}


def _records(tid):
    """Read the raw transcript records of a thread."""
    from dev_agent import transcript as T
    return list(T.iter_records(tid))


def _new_thread(title="A dialog"):
    """Create a thread through the public entry point.

    ``assistant_id`` is left None: the column has a foreign key to the
    ``assistants`` table, and a test does not need a real assistant row to
    exercise the transcript.
    """
    from core.threads import create_thread
    return create_thread(None, "Assistant", title=title)


# ─── a new thread is mirrored from the start ────────────────────────────────


def test_a_new_thread_records_its_transcript_path():
    """A thread born without a path would never be mirrored at all."""
    tid = _new_thread()

    meta = _meta(tid)
    assert meta["transcript_path"], "the row must point at its transcript"
    assert meta["kind"] == "dialog"
    assert meta["message_count"] == 0
    assert meta["content_schema"] == 1


def test_the_first_message_reaches_the_transcript():
    """The copy that survives the process has to exist before a crash."""
    from core.threads import append_thread_message
    tid = _new_thread()

    append_thread_message(tid, "user", "the first question")
    append_thread_message(tid, "assistant", "the first answer")

    assert [r["content"] for r in _records(tid)] == [
        "the first question", "the first answer"]


def test_appending_updates_the_index_counters():
    """A thread list must not scan the file to say how long a dialogue is."""
    from core.threads import append_thread_message
    tid = _new_thread()

    append_thread_message(tid, "user", "one")
    append_thread_message(tid, "assistant", "two")

    meta = _meta(tid)
    assert meta["message_count"] == 2
    assert meta["total_bytes"] > 0
    assert meta["transcript_bytes"] > 0
    assert meta["last_message_at"]


def test_token_counts_reach_the_index():
    """Cost data must survive the move to a file-backed dialogue."""
    from core.threads import append_thread_message
    tid = _new_thread()

    append_thread_message(tid, "assistant", "answer",
                          tokens={"in": 7, "out": 3, "cache": 1})

    meta = _meta(tid)
    assert meta["tokens_in"] == 7
    assert meta["tokens_out"] == 3
    assert meta["tokens_cache"] == 1


def test_the_transcript_does_not_keep_the_render_payload():
    """Events are render state; storing them would double the file for nothing."""
    from core.threads import append_thread_message
    tid = _new_thread()

    append_thread_message(tid, "assistant", "answer",
                          tokens={"in": 1, "out": 1, "cache": 0})

    record = _records(tid)[0]
    assert record["content"] == "answer", (
        "the JSON prefix must be unpacked, not stored in the record")
    assert "__DEVAGENT_EVENTS__" not in str(record)


# ─── a full save REPLACES the transcript ────────────────────────────────────


def test_a_full_save_replaces_the_transcript():
    """A regenerated dialogue must not keep showing deleted messages."""
    from core.threads import append_thread_message, save_thread_messages
    tid = _new_thread()
    append_thread_message(tid, "user", "old question")
    append_thread_message(tid, "assistant", "old answer")

    save_thread_messages(tid, [
        {"role": "user", "content": "new question", "ts": "2026-11-02T15:00:00"},
        {"role": "assistant", "content": "new answer", "ts": "2026-11-02T15:00:01"},
    ])

    assert [r["content"] for r in _records(tid)] == ["new question", "new answer"]
    assert _meta(tid)["message_count"] == 2


def test_a_save_with_fewer_messages_shrinks_the_file():
    """Replacement, not merge: the count must go down as well as up."""
    from core.threads import save_thread_messages
    tid = _new_thread()
    save_thread_messages(tid, [
        {"role": "user", "content": f"m{i}"} for i in range(5)])
    assert len(_records(tid)) == 5

    save_thread_messages(tid, [{"role": "user", "content": "only"}])

    assert [r["content"] for r in _records(tid)] == ["only"]
    assert _meta(tid)["message_count"] == 1


# ─── reading back ───────────────────────────────────────────────────────────


def test_reading_answers_with_the_same_messages_either_way():
    """The file and the table must describe one conversation, not two."""
    from core.threads import append_thread_message, get_thread_messages
    from storage.repository import repo_load_thread_messages
    tid = _new_thread()

    append_thread_message(tid, "user", "question",
                          file_name="a.txt", file_chars=3)
    append_thread_message(tid, "assistant", "answer",
                          tokens={"in": 4, "out": 2, "cache": 0})

    from_file = get_thread_messages(tid)
    from_table = repo_load_thread_messages(tid)

    assert [m["content"] for m in from_file] == ["question", "answer"]
    assert [m["role"] for m in from_file] == ["user", "assistant"]
    assert from_file[0]["file_name"] == "a.txt"
    assert from_file[0]["file_chars"] == 3
    assert from_file[1]["_tokens"] == {"in": 4, "out": 2, "cache": 0}
    assert len(from_file) == len(from_table)


def test_reading_a_thread_from_the_file_survives_a_restart():
    """A reopened dialogue must show what the user saw before closing it."""
    from core.threads import append_thread_message, get_thread_messages
    tid = _new_thread()
    append_thread_message(tid, "user", "before restart")
    append_thread_message(tid, "assistant", "and the reply")

    # A fresh engine stands for a restart: nothing stays in memory.
    import storage.db as db_mod
    db_mod.reset_engine()

    assert [m["content"] for m in get_thread_messages(tid)] == [
        "before restart", "and the reply"]


# ─── threads written before the file store ──────────────────────────────────


def test_a_legacy_thread_is_not_mirrored_and_still_works():
    """Every dialogue that predates the file store must keep working."""
    from core.threads import append_thread_message, load_thread_messages
    from storage.repository import repo_create_thread
    repo_create_thread("legacy-1", None, "Assistant")

    append_thread_message("legacy-1", "user", "an old message")

    assert _meta("legacy-1")["transcript_path"] is None
    assert _records("legacy-1") == []
    assert [m["content"] for m in load_thread_messages("legacy-1")] == [
        "an old message"]


# ─── a broken mirror never hides a message ──────────────────────────────────


def test_a_failed_file_write_falls_back_to_the_table(monkeypatch):
    """A message the table has must never be invisible because a write failed.

    The mirror drops the thread's transcript path in that case, so every later
    read comes from the table: a file that silently lacks the newest message
    would hide it from the user.
    """
    from core import thread_mirror as mirror
    from core.threads import append_thread_message, get_thread_messages
    tid = _new_thread()
    append_thread_message(tid, "user", "written before the failure")

    real_append = mirror.append_message
    monkeypatch.setattr(mirror, "append_message",
                        lambda *a, **k: {"ok": False, "path": "", "bytes": 0,
                                         "error": "disk full"})
    append_thread_message(tid, "assistant", "lost by the log")
    monkeypatch.setattr(mirror, "append_message", real_append)

    assert _meta(tid)["transcript_path"] is None
    assert [m["content"] for m in get_thread_messages(tid)] == [
        "written before the failure", "lost by the log"]


def test_a_file_shorter_than_the_index_is_not_trusted(monkeypatch):
    """Retention drops old segments; the table still has the whole dialogue."""
    from core import thread_mirror as mirror
    from core.threads import append_thread_message, get_thread_messages
    tid = _new_thread()
    append_thread_message(tid, "user", "kept")
    append_thread_message(tid, "assistant", "also kept")

    # Pretend the index counted more messages than the file still holds.
    monkeypatch.setattr(mirror, "read_messages", lambda *a, **k: [])

    assert [m["content"] for m in get_thread_messages(tid)] == [
        "kept", "also kept"]


# ─── removal ────────────────────────────────────────────────────────────────


def test_a_deleted_thread_leaves_no_transcript_behind():
    """A removed dialogue must not leave a folder nothing points at."""
    from core.threads import append_thread_message, delete_thread
    from dev_agent import transcript as T
    tid = _new_thread()
    append_thread_message(tid, "user", "will be deleted")
    assert T.exists(tid) is True

    delete_thread(tid)

    assert T.exists(tid) is False
    assert _meta(tid) == {}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
