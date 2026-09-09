# -*- coding: utf-8 -*-
"""
Tests for the per-request thread context injection in dev_agent.agent_loop:

- _maybe_thread_context builds a hidden block with thread_id, thread_files_dir
  and the dialog-upload listing (FACTS only - names/paths/sizes/type, no
  content parsing);
- _with_thread_context appends that block as a hidden system message;
- a missing thread id returns None; a thread without uploads still gets the
  base block (no upload listing).
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point SagaAI at a fresh temp data dir and reload path-dependent modules."""
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


class _ThreadState:
    thread_id = "ctx_thread_01"


class _NoThreadState:
    thread_id = ""


def test_thread_context_lists_dialog_uploads(tmp_path):
    """The injected block reports thread uploads as facts (name, path, size,
    text/binary kind) without embedding any file content."""
    from dev_agent import agent_loop as al
    from core.threads_devagent import save_thread_file_data
    from core.paths import get_thread_dir

    save_thread_file_data("ctx_thread_01", "note.txt", "hello\nworld\n".encode())
    save_thread_file_data("ctx_thread_01", "data.zip", b"PK\x03\x04" + bytes(range(8)))

    ctx = al._maybe_thread_context(_ThreadState())
    assert ctx is not None
    assert "## CURRENT THREAD ARTIFACTS DIR" in ctx
    assert "thread_id: ctx_thread_01" in ctx
    assert "thread_files_dir: " in ctx
    files_dir = os.path.join(get_thread_dir("ctx_thread_01"), "files")
    assert files_dir in ctx
    # Upload listing is present and factual: file names, sizes, text/binary flag.
    assert "Dialog uploads available to this thread:" in ctx
    assert "note.txt" in ctx
    assert "data.zip" in ctx
    assert "text" in ctx
    assert "binary" in ctx
    # The platform never extracts content into the context: probe payload must
    # not carry multi-line file bodies beyond short presentation probes.
    assert "hello\nworld\n" not in ctx


def test_thread_context_without_uploads_still_injects_base_block(tmp_path):
    """A thread with no uploads gets the base artifacts block, no listing."""
    from dev_agent import agent_loop as al

    ctx = al._maybe_thread_context(_ThreadState())
    assert ctx is not None
    assert "thread_id: ctx_thread_01" in ctx
    assert "thread_files_dir: " in ctx
    assert "Dialog uploads available to this thread:" not in ctx


def test_thread_context_no_thread_id_returns_none(tmp_path):
    from dev_agent import agent_loop as al

    assert al._maybe_thread_context(_NoThreadState()) is None


def test_with_thread_context_appends_hidden_system_message(tmp_path):
    """_with_thread_context appends ONE hidden system message with the block."""
    from dev_agent import agent_loop as al
    from core.threads_devagent import save_thread_file_data

    save_thread_file_data("ctx_thread_01", "a.txt", "abc".encode())
    history = [{"role": "user", "content": "hello"}]

    out = al._with_thread_context(history, _ThreadState())
    assert len(out) == len(history) + 1
    assert out[-1]["role"] == "system"
    assert out[-1].get("hidden") is True
    assert "a.txt" in out[-1]["content"]

    # Without a thread id the history stays untouched.
    assert al._with_thread_context(history, _NoThreadState()) == history


def test_thread_context_upload_listing_is_stable(tmp_path):
    """Repeated injection does not create/alter files (listing is side-effect free)."""
    from dev_agent import agent_loop as al

    ctx_first = al._maybe_thread_context(_ThreadState())
    ctx_second = al._maybe_thread_context(_ThreadState())
    assert ctx_first == ctx_second
