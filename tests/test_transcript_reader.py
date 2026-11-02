# -*- coding: utf-8 -*-
"""tests/test_transcript_reader.py - reading a thread from file or table.

Two properties matter more than any single mode:

- a thread written to a transcript file and a thread still held in the
  ``messages`` table produce the SAME output shape, because every caller was
  written against that shape and must not learn which source answered;
- a thread with no transcript at all keeps working - that is every dialog
  written before the change, and a reader that fails on them is not shippable
  until a migration runs.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.paths as paths  # noqa: E402
from core.orchestration import transcript_reader as R  # noqa: E402
from dev_agent import transcript as T  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Give every test a private history root."""
    monkeypatch.setattr(paths, "HISTORY_DIR", str(tmp_path / "history"))
    yield


def _serve_thread(monkeypatch, thread_id, *, parent=None, title="A dialog",
                  messages=(), transcript=True):
    """Make the reader see *thread_id* backed by a transcript file."""
    meta = {"thread_id": thread_id, "type": "devagent",
            "title": title, "assistant_name": "DevAgent",
            "assistant_id": "dev_agent", "parent_thread_id": parent,
            "kind": "subagent" if parent else "dialog"}
    if transcript:
        T.append_messages(thread_id, list(messages), parent_thread_id=parent)
        meta["transcript_path"] = f"{thread_id}/transcript.jsonl"
    monkeypatch.setattr(R, "_meta", lambda tid: dict(meta) if tid == thread_id else {})
    return meta


def _messages(n, prefix="message"):
    """Build *n* alternating messages."""
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"{prefix} {i}"} for i in range(n)]


# ─── the file path ─────────────────────────────────────────────────────────


def test_a_window_reads_the_requested_range(monkeypatch):
    """Offset and limit select exactly the messages asked for."""
    _serve_thread(monkeypatch, "thr-window", messages=_messages(10))

    out = R.read_window("thr-window", offset=3, limit=4)

    assert out["ok"] is True
    assert out["source"] == "transcript"
    assert out["total"] == 10
    assert [m["content"] for m in out["messages"]] == [
        "message 3", "message 4", "message 5", "message 6"]
    assert out["has_more"] is True


def test_indices_are_absolute_positions(monkeypatch):
    """A window deep in the thread keeps the true positions, not 0..n.

    A search hit reports an absolute index; reading around it depends on the
    window reporting the same coordinate system.
    """
    _serve_thread(monkeypatch, "thr-idx", messages=_messages(10))

    out = R.read_window("thr-idx", offset=6, limit=3)

    assert [m["index"] for m in out["messages"]] == [6, 7, 8]


def test_a_short_content_is_returned_whole(monkeypatch):
    """Ordinary messages must not be touched by the cap."""
    _serve_thread(monkeypatch, "thr-short", messages=[
        {"role": "user", "content": "a short question"}])

    out = R.read_window("thr-short")

    assert out["messages"][0]["content"] == "a short question"


def test_a_huge_message_is_capped_with_a_marker(monkeypatch):
    """One giant message must not swallow the whole context window."""
    _serve_thread(monkeypatch, "thr-big", messages=[
        {"role": "user", "content": "q" * (R.MAX_MESSAGE_CHARS + 500)}])

    out = R.read_window("thr-big", max_chars=10_000_000)

    content = out["messages"][0]["content"]
    assert "chars total" in content
    assert len(content) < R.MAX_MESSAGE_CHARS + 200


def test_hidden_entries_are_skipped_by_default(monkeypatch):
    """Service entries stay out of a normal read, as they do in the table."""
    messages = [
        {"role": "user", "content": "real question"},
        {"role": "user", "content": "[tool_result] giant payload",
         "hidden": True},
        {"role": "assistant", "content": "real answer"},
    ]
    _serve_thread(monkeypatch, "thr-hidden", messages=messages)

    out = R.read_window("thr-hidden")

    assert [m["content"] for m in out["messages"]] == [
        "real question", "real answer"]
    # The indices still count the hidden entry.
    assert [m["index"] for m in out["messages"]] == [0, 2]


def test_hidden_entries_appear_when_asked_for(monkeypatch):
    """Debugging a child needs its tool results, so they are reachable."""
    messages = [
        {"role": "user", "content": "q"},
        {"role": "user", "content": "[tool_result] payload", "hidden": True},
    ]
    _serve_thread(monkeypatch, "thr-shown", messages=messages)

    out = R.read_window("thr-shown", include_tool_results=True)

    assert len(out["messages"]) == 2


# ─── the recovery modes ────────────────────────────────────────────────────


def test_read_tail_returns_the_last_messages(monkeypatch):
    """The question after a crash is "where did it stop"."""
    _serve_thread(monkeypatch, "thr-tail", messages=_messages(30))

    out = R.read_tail("thr-tail", limit=5)

    assert [m["content"] for m in out["messages"]] == [
        "message 25", "message 26", "message 27", "message 28", "message 29"]
    assert out["total"] == 30
    assert out["offset"] == 25


def test_read_tail_of_a_short_thread_returns_everything(monkeypatch):
    """A tail request larger than the thread is not an error."""
    _serve_thread(monkeypatch, "thr-short-tail", messages=_messages(3))

    out = R.read_tail("thr-short-tail", limit=20)

    assert len(out["messages"]) == 3
    assert out["offset"] == 0


def test_read_around_centres_on_the_index(monkeypatch):
    """A search hit becomes readable context without guessing an offset."""
    _serve_thread(monkeypatch, "thr-around", messages=_messages(20))

    out = R.read_around("thr-around", index=10, before=2, after=3)

    assert [m["index"] for m in out["messages"]] == [8, 9, 10, 11, 12]


def test_the_char_budget_stops_a_page_and_says_so(monkeypatch):
    """A page that cannot fit reports the cut instead of silently shrinking."""
    _serve_thread(monkeypatch, "thr-budget", messages=[
        {"role": "user", "content": "b" * 500} for _ in range(20)])

    out = R.read_window("thr-budget", limit=20, max_chars=2000)

    assert out["budget_hit"] is True
    assert len(out["messages"]) < 20


# ─── the legacy fallback ───────────────────────────────────────────────────


def test_a_thread_without_a_transcript_falls_back_to_the_table(monkeypatch):
    """Every dialog written before the change must keep opening.

    ``transcript_path`` is NULL for those rows. The reader must hand the call
    to the SQLite reader, and that reader's answer must come back unchanged -
    shape, keys and all - or the whole platform breaks on the first old
    dialog.
    """
    sentinel = {
        "ok": True, "thread_id": "old-thread", "title": "Old",
        "orchestrator": "DevAgent", "orchestrator_slug": "dev_agent",
        "total": 2, "offset": 0, "limit": 50, "count": 1, "remaining": 1,
        "has_more": True,
        "messages": [{"index": 0, "role": "user", "ts": "",
                      "content": "from the table", "file_name": "",
                      "file_chars": 0}],
    }
    import core.threads_devagent as td
    monkeypatch.setattr(td, "read_thread_window",
                        lambda *a, **k: dict(sentinel))
    monkeypatch.setattr(R, "_meta", lambda tid: {
        "thread_id": tid, "type": "devagent", "title": "Old",
        "assistant_id": "dev_agent", "transcript_path": None})

    out = R.read_window("old-thread")

    assert out["source"] == "messages"
    assert out["messages"][0]["content"] == "from the table"
    assert out["total"] == 2
    assert out["budget_hit"] is False


def test_a_broken_transcript_falls_back_instead_of_failing(monkeypatch):
    """A damaged file must not make a readable dialog unreadable."""
    import core.threads_devagent as td
    monkeypatch.setattr(td, "read_thread_window", lambda *a, **k: {
        "ok": True, "thread_id": "half-broken", "total": 1,
        "messages": [{"index": 0, "role": "user", "content": "rescued"}],
    })
    monkeypatch.setattr(R, "_meta", lambda tid: {
        "thread_id": tid, "type": "devagent",
        "transcript_path": f"{tid}/transcript.jsonl"})
    # The index claims a transcript, but nothing is on disk for this thread.

    out = R.read_window("half-broken")

    assert out["source"] == "messages"
    assert out["messages"][0]["content"] == "rescued"


def test_an_unknown_thread_still_reports_an_error(monkeypatch):
    """An unreadable thread must not look like an empty one."""
    import core.threads_devagent as td
    monkeypatch.setattr(td, "read_thread_window",
                        lambda *a, **k: {"ok": False, "error": "Thread not found: nope"})
    monkeypatch.setattr(R, "_meta", lambda tid: {})

    out = R.read_window("nope")

    assert out["ok"] is False
    assert "not found" in out["error"].lower()


# ─── the cheap description ─────────────────────────────────────────────────


def test_describe_reports_the_index_without_reading_content(monkeypatch):
    """A status line asks about counts and cost, not about the text."""
    _serve_thread(monkeypatch, "thr-desc", messages=_messages(7))

    out = R.describe("thr-desc")

    assert out["ok"] is True
    assert out["source"] == "transcript"
    assert out["transcript_on_disk"] is True
    assert out["segments"] >= 1
    assert out["file_bytes"] > 0
    assert out["parent_thread_id"] is None


def test_a_child_description_names_its_parent(monkeypatch):
    """A child is addressable through its parent, so the row says which."""
    _serve_thread(monkeypatch, "sub-1", parent="root-1", messages=_messages(2))

    out = R.describe("sub-1")

    assert out["parent_thread_id"] == "root-1"
    assert out["kind"] == "subagent"
    assert out["transcript_on_disk"] is True


def test_describe_of_a_legacy_thread_says_messages(monkeypatch):
    """The description itself marks which source backs the thread."""
    monkeypatch.setattr(R, "_meta", lambda tid: {
        "thread_id": tid, "type": "devagent", "transcript_path": None})

    out = R.describe("legacy-1")

    assert out["source"] == "messages"
    assert out["transcript_on_disk"] is False
