# -*- coding: utf-8 -*-
# C2 scenario tests: the batch wire survives persist and reload byte-identically.

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from storage.db import reset_engine, reset_devagent_engine

NL = chr(10)
FENCE = chr(96) * 3 + "json" + NL
BIG_CHARS = 1500000


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Isolated data dir plus a project sandbox for the scenario.
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_resume_")
    old = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    names = (
        "DATA_DIR",
        "DB_PATH",
        "DEVAGENT_DB_PATH",
        "HISTORY_DIR",
        "SYSTEM_PROMPTS_DIR",
    )
    old_values = {}
    for name in names:
        old_values[name] = getattr(core.paths, name, None)
    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")
    reset_engine()
    reset_devagent_engine()

    from dev_agent import config
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(config, "PROTECTED_FILES", ())
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", True, raising=False)
    config.ensure_runtime_dirs()

    yield root

    reset_engine()
    reset_devagent_engine()
    if old:
        os.environ["SAGAAI_DATA_DIR"] = old
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    for name in names:
        value = old_values.get(name)
        if value is not None:
            setattr(core.paths, name, value)
    shutil.rmtree(tmp, ignore_errors=True)


def _block(obj):
    # Serialize obj as the fenced JSON block the agent loop parses.
    return FENCE + json.dumps(obj, ensure_ascii=False) + NL + chr(96) * 3


def _run_batch(root, monkeypatch):
    # Run the real loop over a batch: one giant and one small result.
    import dev_agent.agent_loop as al
    from dev_agent.tool_executor import ToolExecutor

    (root / "src" / "small.txt").write_text("hello world" + NL, encoding="utf-8")
    executor = ToolExecutor()
    big = "x" * BIG_CHARS

    def fake_list_files(subdir="", max_depth=1):
        return {"ok": True, "subdir": subdir or ".", "content": big}

    monkeypatch.setattr(executor, "list_files", fake_list_files)

    batch = NL.join([
        "Reading the tree.",
        _block({"tool": "list_files", "args": {}}),
        _block({"tool": "read_file", "args": {"path": "src/small.txt"}}),
    ])
    answers = [batch, "Done." + NL + _block({"loop_status": "awaiting_user"})]
    sent = []
    cursor = {"i": 0}

    def fake_send(*args, **kwargs):
        sent.append(args[0] if args else "")
        index = cursor["i"]
        cursor["i"] = index + 1
        if index < len(answers):
            return answers[index]
        return ""

    monkeypatch.setattr(al, "send_request", fake_send)

    result = al.run_agent_loop(
        "resume identity",
        {"text": "system", "service": "mock", "model": "m", "temperature": 0.1},
        executor,
        auto_apply=True,
        max_steps=12,
    )
    assert len(sent) >= 2
    wire = sent[1]
    entries = []
    for message in result.history:
        if message.get("role") == "user" and message.get("content") == wire:
            entries.append(message)
    assert len(entries) == 1
    return wire, entries[0]


def test_scenario_wire_survives_append_reload(env, monkeypatch):
    # The appended wire bytes survive an append plus reload round-trip.
    from core.threads_devagent import (
        create_devagent_thread,
        append_thread_message,
        load_thread_messages,
    )

    wire, entry = _run_batch(env, monkeypatch)

    docs = wire.split(NL)
    assert len(docs) == 2
    first = json.loads(docs[0])["tool_result"]
    assert first["ok"] is True
    assert first["truncated"] is True
    assert first["spill_path"].startswith(".dev_agent/tool_results/")
    assert ("x" * BIG_CHARS) not in wire
    second = json.loads(docs[1])["tool_result"]
    assert second["ok"] is True
    assert "small.txt" in second.get("path", "")
    assert "hello" in second.get("content", "")

    tid = create_devagent_thread(title="c2 append", orchestrator_slug="dev_agent")
    append_thread_message(
        tid,
        "user",
        wire,
        events=entry.get("_events"),
        tokens=entry.get("_tokens"),
        hidden=bool(entry.get("hidden")),
    )

    loaded = load_thread_messages(tid)
    assert len(loaded) == 1
    assert loaded[0]["content"] == wire
    assert loaded[0].get("hidden") is True


def test_scenario_wire_survives_full_save(env, monkeypatch):
    # The same wire bytes survive a full-history save plus reload round-trip.
    from core.threads_devagent import (
        create_devagent_thread,
        save_thread_messages,
        load_thread_messages,
    )

    wire, entry = _run_batch(env, monkeypatch)

    tid = create_devagent_thread(title="c2 save", orchestrator_slug="dev_agent")
    save_thread_messages(
        tid,
        [
            {
                "role": "user",
                "content": wire,
                "hidden": bool(entry.get("hidden")),
            },
        ],
    )

    loaded = load_thread_messages(tid)
    assert loaded[0]["content"] == wire
    assert loaded[0].get("hidden") is True
    assert loaded[0]["content"].startswith('{"tool_result"')
