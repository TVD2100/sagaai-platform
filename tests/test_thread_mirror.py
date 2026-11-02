# -*- coding: utf-8 -*-
"""tests/test_thread_mirror.py - the file half of the dialogue mirror.

Both dialogue kinds share one transcript writer, so the operations that touch
only the FILE have to behave identically no matter which database the index
row lives in. These tests pin that contract on its own, without a database:

- an append writes exactly one record and reports its size;
- a rewrite REPLACES the file, because a full save of a conversation must not
  leave deleted messages behind for a reader to find;
- reading returns the same message shape the table reader produces, so a
  caller never has to translate between the two sources;
- a broken or incomplete file is reported instead of raising.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Point the platform at a fresh data dir with an empty history tree."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import core.paths as paths_mod
    importlib.reload(paths_mod)

    yield tmp_path


def _mirror():
    """Import the module under test through its normal import path."""
    from core import thread_mirror
    return thread_mirror


def _records(thread_id):
    """Read the raw records of a thread straight from the writer."""
    from dev_agent import transcript as T
    return list(T.iter_records(thread_id))


# ─── the index row decides whether a thread is mirrored ──────────────────────


def test_a_row_without_a_transcript_path_is_not_mirrored():
    """A dialogue written before the file store keeps working from the table."""
    M = _mirror()

    assert M.is_mirrored({}) is False
    assert M.is_mirrored({"transcript_path": None}) is False
    assert M.is_mirrored({"transcript_path": "  "}) is False
    assert M.is_mirrored({"transcript_path": "t1/transcript.jsonl"}) is True


def test_the_relative_path_is_usable_and_stable():
    """The index stores a relative path so an installation can be moved."""
    M = _mirror()

    rel = M.transcript_rel_path("thread-1")

    assert rel == "thread-1/transcript.jsonl"
    assert not os.path.isabs(rel)


def test_a_child_keeps_its_transcript_inside_its_parent():
    """The folder tree is the ownership tree: deleting a parent takes the child."""
    M = _mirror()

    rel = M.transcript_rel_path("child-1", "parent-1")

    assert rel == "parent-1/subagents/child-1/transcript.jsonl"


def test_next_seq_continues_from_the_index_counter():
    """A resumed thread must not number a new record over an existing one."""
    M = _mirror()

    assert M.next_seq({}) == 1
    assert M.next_seq({"message_count": 7}) == 8
    assert M.next_seq({"message_count": "bad"}) == 1


# ─── append ──────────────────────────────────────────────────────────────────


def test_an_append_writes_exactly_one_record():
    """The mirror trails the database write by one message, never by a batch."""
    M = _mirror()

    result = M.append_message("t-append", {"role": "user", "content": "hello"},
                              seq=1)

    assert result["ok"] is True
    assert result["bytes"] > 0
    assert [r["content"] for r in _records("t-append")] == ["hello"]


def test_an_append_keeps_the_attachment_and_the_cost():
    """The file is the reader's source, so it has to carry what the table did."""
    M = _mirror()

    M.append_message("t-full", {"role": "user", "content": "text",
                                 "file_name": "doc.pdf", "file_chars": 42,
                                 "_tokens": {"in": 5, "out": 3, "cache": 1}},
                     seq=1)

    record = _records("t-full")[0]
    assert record["file_name"] == "doc.pdf"
    assert record["file_chars"] == 42
    assert record["tokens"] == {"in": 5, "out": 3, "cache": 1}


def test_the_render_payload_does_not_reach_the_file():
    """Events are feed state; storing them would double the file for nothing."""
    M = _mirror()
    events = [{"kind": "tool_result", "payload": "x" * 2000}]

    M.append_message("t-events", {"role": "assistant", "content": "visible",
                                   "_events": events}, seq=1)

    record = _records("t-events")[0]
    assert record["content"] == "visible"
    assert "payload" not in str(record)


def test_a_failed_append_is_reported_and_never_raises(monkeypatch):
    """A mirror must not turn a working chat into a failed one."""
    M = _mirror()

    def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(M, "_transcript", _boom)

    result = M.append_message("t-boom", {"role": "user", "content": "x"}, seq=1)

    assert result["ok"] is False
    assert "disk on fire" in result["error"]


def test_a_non_dict_message_is_refused():
    """A caller mistake is answered, not stored as a broken record."""
    M = _mirror()

    result = M.append_message("t-bad", "not a message", seq=1)

    assert result["ok"] is False
    assert _records("t-bad") == []


# ─── rewrite ─────────────────────────────────────────────────────────────────


def test_a_rewrite_replaces_the_whole_file():
    """A full save that only appended would keep deleted messages visible."""
    M = _mirror()
    M.append_message("t-rewrite", {"role": "user", "content": "old"}, seq=1)
    M.append_message("t-rewrite", {"role": "assistant", "content": "old reply"},
                     seq=2)

    result = M.rewrite_messages("t-rewrite", [
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new reply"},
    ])

    assert result["ok"] is True
    assert result["count"] == 2
    assert [r["content"] for r in _records("t-rewrite")] == ["new", "new reply"]


def test_a_rewrite_with_fewer_messages_shrinks_the_file():
    """Replacement, not merge: the count has to go down as well as up."""
    M = _mirror()
    M.rewrite_messages("t-shrink", [{"role": "user", "content": f"m{i}"}
                                     for i in range(5)])

    M.rewrite_messages("t-shrink", [{"role": "user", "content": "only"}])

    assert [r["content"] for r in _records("t-shrink")] == ["only"]


def test_a_rewrite_skips_entries_that_are_not_messages():
    """One malformed entry must not cost the rest of the conversation."""
    M = _mirror()

    result = M.rewrite_messages("t-skip", [
        {"role": "user", "content": "kept"},
        "not a dict",
        {"role": "assistant", "content": "also kept"},
    ])

    assert result["count"] == 2
    assert [r["content"] for r in _records("t-skip")] == ["kept", "also kept"]


# ─── reading back ────────────────────────────────────────────────────────────


def test_reading_returns_the_shape_the_table_reader_produces():
    """A caller switches between the two sources without translating."""
    M = _mirror()
    M.append_message("t-read", {"role": "user", "content": "question",
                                 "file_name": "a.txt", "file_chars": 3,
                                 "_tokens": {"in": 2, "out": 1, "cache": 0}},
                     seq=1)
    M.append_message("t-read", {"role": "assistant", "content": "answer",
                                 "hidden": True}, seq=2)

    messages = M.read_messages("t-read")

    assert messages[0] == {"role": "user", "content": "question",
                           "ts": messages[0]["ts"], "file_name": "a.txt",
                           "file_chars": 3,
                           "_tokens": {"in": 2, "out": 1, "cache": 0}}
    assert messages[0]["ts"], "a message must keep its timestamp"
    assert messages[1]["hidden"] is True
    assert "_tokens" not in messages[1]


def test_reading_an_unknown_thread_returns_an_empty_list():
    """A dialogue without a transcript is not an error, it is an empty file."""
    M = _mirror()

    assert M.read_messages("never-written") == []


def test_a_checkpoint_never_becomes_a_message():
    """Progress markers are not something the user said."""
    from dev_agent import transcript as T
    M = _mirror()
    M.append_message("t-cp", {"role": "user", "content": "real"}, seq=1)
    T.append_checkpoint("t-cp", T.build_checkpoint(0, step=2, status="running"), 2)

    messages = M.read_messages("t-cp")

    assert [m["content"] for m in messages] == ["real"]


def test_a_torn_line_is_skipped_instead_of_breaking_the_read():
    """A crash mid-write must cost one record, not the conversation."""
    from dev_agent import transcript as T
    M = _mirror()
    M.append_message("t-torn", {"role": "user", "content": "good"}, seq=1)
    with open(T.transcript_path("t-torn"), "a", encoding="utf-8") as fh:
        fh.write('{"role": "assistant", "content": "torn"')
    M.append_message("t-torn", {"role": "assistant", "content": "after"}, seq=2)

    messages = M.read_messages("t-torn")

    assert [m["content"] for m in messages] == ["good", "after"]


# ─── completeness ────────────────────────────────────────────────────────────


def test_a_file_shorter_than_the_index_is_reported_incomplete():
    """Retention may drop the oldest segment; the table still has every message."""
    M = _mirror()

    assert M.is_complete({"transcript_path": "x", "message_count": 10}, [1, 2]) is False
    assert M.is_complete({"transcript_path": "x", "message_count": 2}, [1, 2]) is True
    assert M.is_complete({}, [1, 2]) is False
    assert M.is_complete({"transcript_path": "x", "message_count": 0}, []) is False


# ─── removal ─────────────────────────────────────────────────────────────────


def test_removing_a_thread_takes_its_folder_with_it():
    """A deleted dialogue must not leave a transcript nothing points at."""
    M = _mirror()
    M.append_message("t-del", {"role": "user", "content": "gone"}, seq=1)

    assert M.remove("t-del") is True
    assert M.read_messages("t-del") == []
    assert M.remove("t-del") is False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
