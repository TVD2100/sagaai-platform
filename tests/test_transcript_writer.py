# -*- coding: utf-8 -*-
"""tests/test_transcript_writer.py - the append-only transcript log.

The log only earns its place if it is trustworthy in the ugly cases: a torn
last line after a crash, a payload too large to store, a file that grows past
its bound, an id that would escape its folder. Each of those is pinned here,
because each of them is a way a recovery log silently stops being a recovery
log - it either loses the record that mattered or refuses to be read at all.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.paths as paths  # noqa: E402
from dev_agent import transcript as T  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Point the history root at a private folder for every test."""
    monkeypatch.setattr(paths, "HISTORY_DIR", str(tmp_path / "history"))
    yield


def _records(thread_id, parent=None):
    """Return the records of a thread as a list."""
    return list(T.iter_records(thread_id, parent))


# ─── round trip ────────────────────────────────────────────────────────────


def test_a_message_round_trips_with_its_fields():
    """What is written is what is read back, field for field."""
    record = T.build_record(
        {"role": "assistant", "content": "hello there",
         "ts": "2026-11-02T14:00:00", "_tokens": {"in": 10, "out": 4}},
        seq=1,
    )
    result = T.append_record(record, "thread-a")

    assert result["ok"] is True
    assert result["bytes"] > 0
    stored = _records("thread-a")
    assert len(stored) == 1
    assert stored[0]["role"] == "assistant"
    assert stored[0]["content"] == "hello there"
    assert stored[0]["ts"] == "2026-11-02T14:00:00"
    assert stored[0]["tokens"] == {"in": 10, "out": 4, "cache": 0}
    assert stored[0]["seq"] == 1


def test_records_keep_their_order():
    """Appending three messages reads back three messages, in order."""
    T.append_messages("thread-b", [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ])

    stored = _records("thread-b")
    assert [r["content"] for r in stored] == ["one", "two", "three"]
    assert [r["seq"] for r in stored] == [1, 2, 3]


def test_a_missing_thread_is_not_an_error():
    """Reading an unknown thread yields nothing instead of raising."""
    assert _records("never_written") == []
    assert T.exists("never_written") is False


# ─── render metadata is dropped, cost is kept ──────────────────────────────


def test_the_event_prefix_is_stripped_from_the_stored_text():
    """The chat feed's render payload must not reach the transcript.

    The message tables embed ``_events`` inside the content field, and that
    payload measured at more than half of a stored message. A transcript is
    read by an agent recovering work, not rendered by the chat feed, so the
    payload is dropped while the visible text is stored clean.
    """
    events = [{"kind": "tool_result", "payload": "x" * 5000}]
    content = ('__DEVAGENT_EVENTS__' + json.dumps({"_events": events})
               + '\n' + 'the visible answer')

    record = T.build_record({"role": "assistant", "content": content}, seq=1)

    assert record["content"] == "the visible answer"
    # The events payload is not stored anywhere in the record.
    assert "tool_result" not in json.dumps(record)


def test_token_counts_survive_the_prefix_strip():
    """Cost data rides in the same prefix and must not be lost with it."""
    content = ('__DEVAGENT_EVENTS__'
               + json.dumps({"tokens": {"in": 7, "out": 3, "cache": 2}})
               + '\n' + 'answer')

    record = T.build_record({"role": "assistant", "content": content}, seq=1)

    assert record["content"] == "answer"
    assert record["tokens"] == {"in": 7, "out": 3, "cache": 2}


def test_a_hidden_marker_survives_the_strip():
    """A hidden entry stays recognisable after a reload."""
    content = ('__DEVAGENT_EVENTS__' + json.dumps({"hidden": True})
               + '\n' + 'service notice')

    record = T.build_record({"role": "user", "content": content}, seq=1)

    assert record["hidden"] is True
    assert record["content"] == "service notice"


# ─── the tree: a child lives inside its parent ────────────────────────────


def test_a_child_transcript_lives_inside_its_parent():
    """The folder tree IS the ownership tree, so one delete takes the branch."""
    child = T.thread_dir("child-1", parent_thread_id="parent-1")
    parent = T.thread_dir("parent-1")

    assert child.parent.name == T.SUBAGENTS_DIR
    assert child.parent.parent == parent


def test_deleting_a_parent_removes_its_children():
    """A removed dialog must not leave orphan transcripts behind."""
    T.append_record(T.build_record({"role": "user", "content": "p"}, 1), "parent-2")
    T.append_record(T.build_record({"role": "user", "content": "c"}, 1),
                    "child-2", parent_thread_id="parent-2")
    assert T.exists("child-2", "parent-2") is True

    assert T.delete_transcript("parent-2") is True

    assert T.exists("parent-2") is False
    assert T.exists("child-2", "parent-2") is False


# ─── the ugly cases ────────────────────────────────────────────────────────


def test_a_torn_last_line_is_skipped_not_fatal():
    """A crash mid-write costs one line, never the whole history."""
    T.append_record(T.build_record({"role": "user", "content": "good"}, 1),
                    "thread-torn")
    path = T.transcript_path("thread-torn")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "seq": 2, "content": "cut off mid')

    stored = _records("thread-torn")

    assert len(stored) == 1
    assert stored[0]["content"] == "good"


def test_a_record_larger_than_the_cap_is_truncated_with_a_marker(monkeypatch):
    """One pathological payload must not make the transcript unreadable."""
    monkeypatch.setattr(T, "MAX_MESSAGE_BYTES", 2048)
    huge = "y" * 20_000

    result = T.append_record(
        T.build_record({"role": "user", "content": huge}, 1), "thread-huge")

    assert result["ok"] is True
    stored = _records("thread-huge")
    assert len(stored) == 1
    assert stored[0]["truncated"] is True
    assert "bytes truncated" in stored[0]["content"]
    assert len(stored[0]["content"]) < len(huge)
    # The envelope survives, so the record still says who and when.
    assert stored[0]["role"] == "user"


def test_a_full_file_rotates_to_a_segment(monkeypatch):
    """No single transcript file grows without bound."""
    monkeypatch.setattr(T, "MAX_FILE_BYTES", 500)

    for i in range(20):
        T.append_record(
            T.build_record({"role": "user", "content": "z" * 100}, i + 1),
            "thread-rotate")

    folder = T.thread_dir("thread-rotate")
    segments = [p.name for p in folder.iterdir() if ".jsonl" in p.name]
    assert len(segments) > 1, "the file never rotated"

    # Rotation must not lose or reorder a single record.
    stored = _records("thread-rotate")
    assert len(stored) == 20
    assert [r["seq"] for r in stored] == list(range(1, 21))


def test_an_unsafe_id_is_refused_instead_of_cleaned():
    """A traversal-shaped id must fail loudly, not land somewhere odd."""
    with pytest.raises(ValueError):
        T.thread_dir("../../etc")
    with pytest.raises(ValueError):
        T.thread_dir("")


def test_a_failed_write_reports_itself_instead_of_raising(monkeypatch):
    """A broken log must not turn a working run into a failed one."""
    def _boom(*_args, **_kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(T.os, "open", _boom)

    result = T.append_record(
        T.build_record({"role": "user", "content": "x"}, 1), "thread-boom")

    assert result["ok"] is False
    assert "disk is gone" in result["error"]


# ─── the stored path ───────────────────────────────────────────────────────


def test_the_index_path_is_relative_to_the_history_root():
    """An index row must survive moving the installation."""
    result = T.append_record(
        T.build_record({"role": "user", "content": "x"}, 1), "thread-rel")

    assert not os.path.isabs(result["path"])
    assert result["path"].startswith("thread-rel/")
    resolved = T.resolve_record_path(result["path"])
    assert resolved is not None
    assert resolved.exists()


def test_an_empty_stored_path_means_legacy():
    """No path is the marker of a thread whose messages are still in SQLite."""
    assert T.resolve_record_path("") is None
