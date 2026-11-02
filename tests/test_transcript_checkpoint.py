# -*- coding: utf-8 -*-
"""tests/test_transcript_checkpoint.py - the checkpoint record.

A checkpoint answers a question neither the transcript nor the journal can
answer alone: WHERE the child was when it died. It differs from a message in
three ways, and each one is a test here:

- it is written on a step BOUNDARY, so every message before it is on disk;
- it is invisible to a reader - a checkpoint that appeared in a window would
  shift every absolute index the model was given;
- it carries the files the child already wrote, which is what stops a
  resumed child from regenerating work that is already on disk.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import transcript as T  # noqa: E402


@pytest.fixture(autouse=True)
def history_root(tmp_path, monkeypatch):
    """Point the transcript store at a fresh folder.

    The index row is faked too: these tests exercise the FILE layer, and a
    real row would drag the whole devagent database into them for a path that
    the reader only uses to find the parent folder.
    """
    monkeypatch.setattr(T, "history_root", lambda: tmp_path)
    from core.orchestration import transcript_reader as R
    monkeypatch.setattr(R, "_meta", lambda tid: {
        "transcript_path": f"{tid}/transcript.jsonl",
        "parent_thread_id": None,
    })
    return tmp_path


def _append(thread_id, role, content):
    """Append one plain message record."""
    return T.append_record({"role": role, "content": content, "ts": "", "v": 1},
                           thread_id)


def _checkpoint(thread_id, seq, **kw):
    """Append one checkpoint record."""
    record = T.build_checkpoint(0, **kw)
    return T.append_checkpoint(thread_id, record, seq)


# ─── the record itself ─────────────────────────────────────────────────────


def test_a_checkpoint_marks_its_role():
    """The role is what every reader filters on."""
    record = T.build_checkpoint(5, step=3, max_steps=10, status="running")

    assert record["role"] == T.CHECKPOINT_ROLE
    assert T.is_checkpoint(record) is True


def test_a_message_is_not_a_checkpoint():
    assert T.is_checkpoint({"role": "assistant", "content": "hi"}) is False


def test_a_checkpoint_carries_the_step_and_the_written_files():
    """Those two facts are the reason the record exists."""
    record = T.build_checkpoint(1, step=4, max_steps=9,
                                written=["a.py", "b.py"], pending=["step 5"])

    assert record["step"] == 4
    assert record["max_steps"] == 9
    assert record["written"] == ["a.py", "b.py"]
    assert record["pending"] == ["step 5"]


def test_a_long_list_is_cut_not_stored_whole():
    """A checkpoint describes a position, it does not copy the plan."""
    record = T.build_checkpoint(1, plan=[f"s{i}" for i in range(200)])

    assert len(record["plan"]) == T.MAX_CHECKPOINT_ITEMS


def test_a_checkpoint_never_carries_message_content():
    """It is metadata; a payload would double the log for no reason."""
    record = T.build_checkpoint(1, note="a task")

    assert record["content"] == ""


# ─── it lives in the transcript but is not a message ───────────────────────


def test_the_newest_checkpoint_wins():
    """Only the last one describes the state worth continuing from."""
    _checkpoint("t1", 1, step=1)
    _append("t1", "assistant", "working")
    _checkpoint("t1", 3, step=2)

    latest = T.last_checkpoint("t1")

    assert latest["step"] == 2


def test_a_thread_without_checkpoints_answers_none():
    _append("t2", "assistant", "just a message")

    assert T.last_checkpoint("t2") is None


def test_a_checkpoint_is_readable_from_an_unknown_thread():
    """A missing log is a fact, not an exception."""
    assert T.last_checkpoint("never-existed") is None


def test_checkpoints_interleave_with_messages_in_order():
    """The file is one log: a reader sees both kinds in write order."""
    _append("t3", "user", "go")
    _checkpoint("t3", 2, step=1)
    _append("t3", "assistant", "done")

    kinds = [r["role"] for r in T.iter_records("t3")]

    assert kinds == ["user", T.CHECKPOINT_ROLE, "assistant"]


# ─── the reader must not see them ──────────────────────────────────────────


def test_a_window_does_not_show_checkpoints():
    """A checkpoint in a window would be read as something the child said."""
    from core.orchestration import transcript_reader as R
    _append("t4", "user", "first")
    _checkpoint("t4", 2, step=1)
    _append("t4", "assistant", "second")

    window = R.read_window("t4")

    assert [m["content"] for m in window["messages"]] == ["first", "second"]


def test_checkpoints_do_not_shift_the_absolute_indices():
    """An index the model received must point at the same message later."""
    from core.orchestration import transcript_reader as R
    _append("t5", "user", "first")
    _checkpoint("t5", 2, step=1)
    _checkpoint("t5", 3, step=2)
    _append("t5", "assistant", "second")

    window = R.read_window("t5")

    assert [m["index"] for m in window["messages"]] == [0, 1]


def test_tail_ignores_checkpoints_as_well():
    """The mode a resume reads must agree with the paged one."""
    from core.orchestration import transcript_reader as R
    _append("t6", "user", "only message")
    _checkpoint("t6", 2, step=1)

    window = R.read_tail("t6")

    assert [m["content"] for m in window["messages"]] == ["only message"]


# ─── the sink writes them where a resume can find them ─────────────────────


def test_the_sink_records_a_checkpoint_after_a_drain():
    """The writer and the reader must agree on the location."""
    from core.orchestration.transcript_sink import TranscriptSink

    class State:
        history = [{"role": "user", "content": "go"},
                   {"role": "assistant",
                    "content": "```json\n{\"tool\": \"propose_file\", "
                               "\"args\": {\"path\": \"core/x.py\"}}\n```"}]
        steps = 2
        max_steps = 10
        task = "a task"
        total_tokens_in = 5
        total_tokens_out = 7
        total_tokens_cache = 0

    sink = TranscriptSink(thread_id="t7", parent_thread_id="parent")
    sink.drain(State())
    sink.checkpoint(State(), status="running")

    saved = T.last_checkpoint("t7", "parent")
    assert saved["step"] == 2
    assert saved["written"] == ["core/x.py"]
    assert saved["tokens"]["in"] == 5
