# -*- coding: utf-8 -*-
"""tests/test_subagent_transcript_modes.py - the read_subagent_thread modes.

A parent reads a child's conversation to answer three different questions,
and each has its own mode:

- "show me page N" - ``window``, the original behaviour, unchanged;
- "where did it stop" - ``tail``, which is what a resume actually needs and
  cannot be expressed with an offset without knowing the total first;
- "what is around this hit" - ``around``, pairing with a search result;
- "how long and how expensive" - ``meta``, which must not read the content.

These tests drive the TOOL, not the reader: the tool is what the model calls,
and its argument handling is where a mode gets lost.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    ERR_INVALID_ARGS,
    ERR_RUN_NOT_FOUND,
)


class FakeService:
    """A registry holding the given run records."""

    def __init__(self, records=None):
        self._records = dict(records or {})

    def get_record(self, run_id):
        return self._records.get(run_id)


class Record:
    """A live run record with the fields the tools read."""

    def __init__(self, **kw):
        self._data = kw

    def to_record(self):
        return dict(self._data)


def _service():
    """One root run and one child run of the same tree."""
    return FakeService({
        "root": Record(run_id="root", root_run_id="root",
                       child_thread_id="th-root"),
        "kid": Record(run_id="kid", root_run_id="root", parent_run_id="root",
                      child_thread_id="th-kid", status="running"),
    })


@pytest.fixture
def reader_calls(monkeypatch):
    """Replace the reader with a recorder; return the recorded calls."""
    calls = []

    def _fake(name):
        def _inner(thread_id, **kwargs):
            calls.append({"fn": name, "thread_id": thread_id, **kwargs})
            return {"ok": True, "thread_id": thread_id, "total": 3,
                    "messages": [{"index": 0, "role": "assistant",
                                  "content": "working"}]}
        return _inner

    import core.orchestration.transcript_reader as R
    for name in ("read_window", "read_tail", "read_around", "describe"):
        monkeypatch.setattr(R, name, _fake(name))
    return calls


# ─── the default mode is unchanged ─────────────────────────────────────────


def test_the_default_mode_is_a_window(reader_calls):
    """Existing callers keep the paged behaviour they were written against."""
    result = tools.read_subagent_thread(run_id="kid", offset=2, limit=7,
                                        service=_service())

    assert result["ok"] is True
    assert result["mode"] == "window"
    assert reader_calls[0]["fn"] == "read_window"
    assert reader_calls[0]["offset"] == 2
    assert reader_calls[0]["limit"] == 7


def test_the_tool_still_reports_the_run_identity(reader_calls):
    """The transcript is always read in the context of its run."""
    result = tools.read_subagent_thread(run_id="kid", service=_service())

    assert result["run_id"] == "kid"
    assert result["child_thread_id"] == "th-kid"
    assert result["status"] == "running"


# ─── the recovery modes ────────────────────────────────────────────────────


def test_tail_reads_from_the_end(reader_calls):
    """"Where did it stop" cannot be asked with an offset."""
    result = tools.read_subagent_thread(run_id="kid", mode="tail", limit=8,
                                        service=_service())

    assert result["mode"] == "tail"
    assert reader_calls[0]["fn"] == "read_tail"
    assert reader_calls[0]["limit"] == 8


def test_around_uses_the_absolute_index(reader_calls):
    """A search hit pairs with the exact index to read around."""
    result = tools.read_subagent_thread(run_id="kid", mode="around", index=42,
                                        before=3, after=4, service=_service())

    assert result["mode"] == "around"
    call = reader_calls[0]
    assert call["fn"] == "read_around"
    assert call["index"] == 42
    assert call["before"] == 3
    assert call["after"] == 4


def test_around_falls_back_to_offset_when_no_index_is_given(reader_calls):
    """A caller that passes an offset must still land on the right message."""
    tools.read_subagent_thread(run_id="kid", mode="around", offset=11,
                               service=_service())

    assert reader_calls[0]["index"] == 11


def test_meta_returns_a_description_without_messages(reader_calls):
    """Counting a transcript must not read its content."""
    result = tools.read_subagent_thread(run_id="kid", mode="meta",
                                        service=_service())

    assert result["mode"] == "meta"
    assert reader_calls[0]["fn"] == "describe"
    # A description has no messages on purpose; it must not be nagged about it.
    assert "note" not in result


def test_an_unknown_mode_falls_back_to_a_window(reader_calls):
    """A typo must not answer with nothing."""
    result = tools.read_subagent_thread(run_id="kid", mode="banana",
                                        service=_service())

    assert reader_calls[0]["fn"] == "read_window"
    assert result["mode"] == "window"


# ─── the paths that do not depend on a transcript ──────────────────────────


def test_a_missing_run_id_is_an_argument_error():
    result = tools.read_subagent_thread(run_id="", service=_service())

    assert result["ok"] is False
    assert result["error_code"] == ERR_INVALID_ARGS


def test_an_unknown_run_is_reported_as_not_found():
    result = tools.read_subagent_thread(run_id="ghost", service=_service())

    assert result["ok"] is False
    assert result["error_code"] == ERR_RUN_NOT_FOUND


def test_a_run_without_a_child_thread_explains_itself():
    """No transcript is a fact to report, not an empty success."""
    service = FakeService({"root": Record(run_id="root", child_thread_id="")})

    result = tools.read_subagent_thread(run_id="root", service=service)

    assert result["ok"] is False
    assert "journal" in str(result.get("note") or "").lower()


def test_a_failing_reader_is_reported_not_hidden(reader_calls, monkeypatch):
    """A damaged transcript must surface as an error, not as empty output."""
    import core.orchestration.transcript_reader as R
    monkeypatch.setattr(R, "read_window",
                        lambda *a, **k: {"ok": False, "error": "corrupt"})

    result = tools.read_subagent_thread(run_id="kid", service=_service())

    assert result["ok"] is False
    assert "corrupt" in result["error"]


def test_the_catalog_entry_documents_every_mode():
    """A mode the model does not know about is a mode it will not use."""
    entry = tools.read_subagent_thread_tool_entry()

    for mode in ("window", "tail", "around", "meta"):
        assert mode in entry["desc"], f"undocumented mode: {mode}"
