# -*- coding: utf-8 -*-
"""Tests for the dialog-upload tools exposed by UniversalDevAgent:

- the tool catalog advertises list_thread_files / read_thread_file;
- WORKSPACE_TOOL_ARGS validates the documented argument names and rejects
  unknown ones with a structured error;
- dispatch uses the ACTIVE_THREAD_ID published by the agent loop;
- text files are read through the optional offset/limit window and binary
  uploads are reported as is_text=False instead of being parsed.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    tmp = tempfile.mkdtemp(prefix="sagaai_test_tfiles_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    reset_engine()
    reset_devagent_engine()
    yield tmp
    reset_engine()
    reset_devagent_engine()

    if old_data_dir:
        os.environ["SAGAAI_DATA_DIR"] = old_data_dir
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def active_thread(monkeypatch, isolated_data_dir):
    from dev_agent import config as dagent_config
    monkeypatch.setattr(dagent_config, "ACTIVE_THREAD_ID", "tf_tools_01")
    return "tf_tools_01"


def _agent():
    from dev_agent.universal_agent import UniversalDevAgent
    return UniversalDevAgent()


def test_catalog_advertises_thread_file_tools():
    agent = _agent()
    names = {t["name"] for t in agent.tool_catalog}
    assert "list_thread_files" in names
    assert "read_thread_file" in names


def test_thread_tools_inside_args_spec():
    from dev_agent.universal_agent import WORKSPACE_TOOL_ARGS
    assert WORKSPACE_TOOL_ARGS["list_thread_files"] == {"required": set(), "optional": set()}
    assert WORKSPACE_TOOL_ARGS["read_thread_file"]["required"] == {"file_name"}
    assert WORKSPACE_TOOL_ARGS["read_thread_file"]["optional"] == {"offset", "limit"}


def test_unknown_args_rejected_with_suggestion(isolated_data_dir):
    agent = _agent()
    res = agent.dispatch("read_thread_file", {"filename": "a.txt"})
    assert res.get("ok") is False
    assert res.get("unknown_args") == ["filename"]
    assert "file_name" in res.get("suggestion", "")


def test_dispatch_without_active_thread_fails(isolated_data_dir):
    from dev_agent import config as dagent_config
    dagent_config.ACTIVE_THREAD_ID = ""
    agent = _agent()
    res = agent.dispatch("list_thread_files", {})
    assert res.get("ok") is False
    assert "No active dialog thread" in res.get("error", "")


def test_dispatch_lists_uploads_of_active_thread(active_thread):
    from core.threads_devagent import save_thread_file_data
    save_thread_file_data("tf_tools_01", "note.txt", "line1\nline2\n".encode())
    agent = _agent()
    res = agent.dispatch("list_thread_files", {})
    assert res.get("ok") is True
    assert res.get("thread_id") == "tf_tools_01"
    names = [f["name"] for f in res["files"]]
    assert "note.txt" in names


def test_dispatch_reads_text_with_offset_limit(active_thread):
    from core.threads_devagent import save_thread_file_data
    save_thread_file_data("tf_tools_01", "note.txt", "l1\nl2\nl3\nl4\n".encode())
    agent = _agent()
    res = agent.dispatch("read_thread_file", {"file_name": "note.txt", "offset": "1", "limit": "2"})
    assert res.get("ok") is True
    assert res.get("is_text") is True
    assert res.get("offset") == 1
    assert res.get("limit") == 2
    assert res.get("content") == "l2\nl3"
    assert res.get("total_lines") == 5
    assert res.get("remaining") == 2


def test_dispatch_reports_binary_without_parsing(active_thread):
    from core.threads_devagent import save_thread_file_data
    save_thread_file_data("tf_tools_01", "archive.zip", b"PK\x03\x04" + bytes(range(16)))
    agent = _agent()
    res = agent.dispatch("read_thread_file", {"file_name": "archive.zip"})
    assert res.get("ok") is True
    assert res.get("is_text") is False
    assert res.get("content") == ""
    assert "run_code" in res.get("hint", "")


def test_dispatch_missing_file_name(active_thread):
    agent = _agent()
    res = agent.dispatch("read_thread_file", {})
    assert res.get("ok") is False
    assert "file_name" in res.get("error", "")
