# -*- coding: utf-8 -*-
"""
Tests for saving uploaded files into chat threads.

Regression test: save_thread_file() must create the thread's files
directory when it does not exist (fresh thread created via create_thread).
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point SagaAI at a fresh temp data dir and reset DB engines."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import storage.db as db_mod
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()

    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)

    yield

    db_mod.reset_engine()
    db_mod.reset_devagent_engine()


def test_create_thread_creates_files_dir(tmp_path):
    """A thread created via create_thread() gets its files dir on disk."""
    from core.threads import create_thread
    from core.paths import get_thread_dir

    tid = create_thread("asst_1", "Assistant")
    files_dir = os.path.join(get_thread_dir(tid), "files")
    assert os.path.isdir(files_dir), f"expected {files_dir} to exist"


def test_save_thread_file_creates_files_dir(tmp_path):
    """save_thread_file() must succeed even if the files dir is missing."""
    from core.threads import save_thread_file
    from core.paths import get_thread_dir

    # Simulate the pre-fix state: thread dir exists but no files/ subdir.
    tid = "20260101_000000_abcdef"
    os.makedirs(get_thread_dir(tid), exist_ok=True)

    saved = save_thread_file(tid, "notes", "line1\nline2")
    assert os.path.exists(saved)

    fname = os.path.basename(saved)
    assert fname == "notes.txt"
    with open(saved, encoding="utf-8") as f:
        assert f.read() == "line1\nline2"

    # The files dir must now exist.
    assert os.path.isdir(os.path.join(get_thread_dir(tid), "files"))
