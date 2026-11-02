# -*- coding: utf-8 -*-
"""tests/test_transcript_dialog_mirror.py - the dialog transcript mirror.

A dialog's messages live in the ``messages`` table, and the file is a mirror
of them. The mirror is only worth having if it agrees with the table at every
moment a reader could look:

- a new dialog is mirrored from its first message, not retroactively;
- appending one message appends exactly one record;
- a full save REPLACES the transcript, because a regenerated or edited dialog
  would otherwise keep showing work the user deleted;
- a dialog created before the change (no ``transcript_path``) keeps working
  and is not mirrored at all.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point the platform at a fresh data dir with a fresh devagent DB."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import storage.db as db_mod
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()

    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)

    yield tmp_path

    db_mod.reset_engine()
    db_mod.reset_devagent_engine()


def _meta(tid):
    """Read the index row of a dialog."""
    from storage.repository_devagent import repo_devagent_load_thread_meta
    return repo_devagent_load_thread_meta(tid) or {}


def _records(tid):
    """Read the transcript records of a dialog."""
    from dev_agent import transcript as T
    meta = _meta(tid)
    return list(T.iter_records(tid, meta.get("parent_thread_id") or None))


def _new_dialog(title="A dialog"):
    """Create a dialog through the public entry point."""
    from core.threads_devagent import create_devagent_thread
    return create_devagent_thread(title=title, orchestrator_slug="dev_agent",
                                  workspace="/tmp/project")


# ─── a new dialog is mirrored from the start ───────────────────────────────


def test_a_new_dialog_records_its_transcript_path():
    """A dialog born without a path would never be mirrored at all."""
    tid = _new_dialog()

    meta = _meta(tid)
    assert meta["transcript_path"], "the row must point at its transcript"
    assert meta["kind"] == "dialog"
    assert meta["parent_thread_id"] is None


def test_the_first_message_reaches_the_transcript():
    """The copy that survives the process has to exist before a crash."""
    from core.threads_devagent import append_thread_message
    tid = _new_dialog()

    append_thread_message(tid, "user", "the first question")
    append_thread_message(tid, "assistant", "the first answer")

    assert [r["content"] for r in _records(tid)] == [
        "the first question", "the first answer"]


def test_appending_updates_the_index_counters():
    """A thread list must not scan the file to say how long a dialog is."""
    from core.threads_devagent import append_thread_message
    tid = _new_dialog()

    append_thread_message(tid, "user", "one")
    append_thread_message(tid, "assistant", "two")

    meta = _meta(tid)
    assert meta["message_count"] == 2
    assert meta["total_bytes"] > 0
    assert meta["transcript_bytes"] > 0
    assert meta["last_message_at"]


def test_token_counts_reach_the_index():
    """Cost data must survive the move to a file-backed dialog."""
    from core.threads_devagent import append_thread_message
    tid = _new_dialog()

    append_thread_message(tid, "assistant", "answer",
                          tokens={"in": 7, "out": 3, "cache": 1})

    meta = _meta(tid)
    assert meta["tokens_in"] == 7
    assert meta["tokens_out"] == 3
    assert meta["tokens_cache"] == 1


def test_the_transcript_does_not_keep_the_render_payload():
    """Events are render state; storing them would double the file for nothing."""
    from core.threads_devagent import append_thread_message
    tid = _new_dialog()
    events = [{"kind": "tool_result", "payload": "x" * 2000}]

    append_thread_message(tid, "assistant", "visible text", events=events)

    record = _records(tid)[0]
    assert record["content"] == "visible text"
    assert "payload" not in str(record)


def test_the_hidden_marker_survives_the_mirror():
    """A service entry stays skipped after a reload from the file."""
    from core.threads_devagent import append_thread_message
    tid = _new_dialog()

    append_thread_message(tid, "user", "[tool_result] payload", hidden=True)

    assert _records(tid)[0].get("hidden") is True


# ─── a full save REPLACES the transcript ───────────────────────────────────


def test_a_full_save_replaces_the_transcript():
    """A regenerated dialog must not keep showing deleted messages.

    ``save_thread_messages`` rewrites the whole conversation. Mirroring it as
    an append would leave the old messages in the file, and a reader (or a
    resuming agent) would see work the database no longer has.
    """
    from core.threads_devagent import append_thread_message, save_thread_messages
    tid = _new_dialog()
    append_thread_message(tid, "user", "old question")
    append_thread_message(tid, "assistant", "old answer")

    save_thread_messages(tid, [
        {"role": "user", "content": "new question", "ts": "2026-11-02T15:00:00"},
        {"role": "assistant", "content": "new answer", "ts": "2026-11-02T15:00:01"},
    ])

    assert [r["content"] for r in _records(tid)] == [
        "new question", "new answer"]
    assert _meta(tid)["message_count"] == 2


def test_a_save_with_fewer_messages_shrinks_the_file():
    """Replacement, not merge: the count must go down as well as up."""
    from core.threads_devagent import save_thread_messages
    tid = _new_dialog()
    save_thread_messages(tid, [
        {"role": "user", "content": f"m{i}"} for i in range(5)])
    assert len(_records(tid)) == 5

    save_thread_messages(tid, [{"role": "user", "content": "only"}])

    assert [r["content"] for r in _records(tid)] == ["only"]
    assert _meta(tid)["message_count"] == 1


# ─── dialogs written before the change ─────────────────────────────────────


def test_a_legacy_dialog_is_not_mirrored_and_still_works():
    """Every dialog that predates the file store must keep working.

    Those rows have no ``transcript_path``: the mirror stays out of the way
    and the table remains the single source of truth until a migration runs.
    """
    from core.threads_devagent import append_thread_message, load_thread_messages
    from storage.repository_devagent import repo_devagent_create_thread
    repo_devagent_create_thread("legacy-1", title="Old",
                                orchestrator_slug="dev_agent")

    append_thread_message("legacy-1", "user", "an old message")

    assert _meta("legacy-1")["transcript_path"] is None
    assert _records("legacy-1") == []
    stored = load_thread_messages("legacy-1")
    assert [m["content"] for m in stored] == ["an old message"]


def test_a_deleted_dialog_leaves_no_transcript_behind():
    """A removed dialog must not leave a folder nothing points at."""
    from core.threads_devagent import append_thread_message, delete_thread
    from dev_agent import transcript as T
    tid = _new_dialog()
    append_thread_message(tid, "user", "will be deleted")
    assert T.exists(tid) is True

    delete_thread(tid)

    assert T.exists(tid) is False
