# -*- coding: utf-8 -*-
"""A4 tests: the append-only snapshot chain survives thread persistence.

Cache-friendly harness contract: hidden system snapshots (task state /
thread context) are stored through ``core.threads_devagent`` with the
``hidden`` marker embedded in the JSON content prefix (the messages table
has no hidden column), restored by ``load_thread_messages`` and recognised
by their content prefix after the round-trip. A reloaded chain must not
duplicate an unchanged snapshot on the next refresh; plain messages must
stay visible; the reloaded length is a valid ``saved_msg_count`` anchor so
the next UI persist step writes only genuinely new entries.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import agent_loop as al


TS_TEXT = "CURRENT TASK STATE:\ntask: demo\nprogress: 1/2"
TC_TEXT = "## CURRENT THREAD ARTIFACTS DIR\nthread_id: t1"


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point SagaAI at a fresh temp data dir and reset engine caches."""
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


def _seeded_state(monkeypatch):
    """A two-message history with both snapshots refreshed in."""
    monkeypatch.setattr(al, "_maybe_task_state_context", lambda: TS_TEXT)
    monkeypatch.setattr(al, "_maybe_thread_context", lambda state: TC_TEXT)
    state = al.AgentLoopState()
    state.history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert al.refresh_context_snapshots(state) == 2
    return state


def _persist_like_ui(tid, history):
    """Persist entries exactly like ui/pages/orchestrator.py::_do_step."""
    from core.threads_devagent import append_thread_message
    for msg in history:
        append_thread_message(
            tid, msg.get("role", "user"), msg.get("content", ""),
            file_name=msg.get("file_name", ""), file_chars=msg.get("file_chars", 0),
            events=msg.get("_events"), tokens=msg.get("_tokens"),
            hidden=bool(msg.get("hidden")),
        )


def test_snapshot_chain_roundtrip_keeps_hidden(monkeypatch):
    """Snapshots persist with the hidden marker and survive the reload."""
    from core.threads_devagent import create_devagent_thread, load_thread_messages

    state = _seeded_state(monkeypatch)
    tid = create_devagent_thread(title="snapshot roundtrip",
                                 orchestrator_slug="dev_agent")
    _persist_like_ui(tid, state.history)

    loaded = load_thread_messages(tid)
    assert len(loaded) == len(state.history) == 4
    assert [m["content"] for m in loaded] == [m["content"] for m in state.history]
    assert [m["role"] for m in loaded] == ["user", "assistant", "system", "system"]

    # Plain messages stay visible; snapshots come back hidden and are still
    # recognised by their content prefix (the in-memory kind is not stored).
    assert [m.get("hidden") for m in loaded[:2]] == [None, None]
    snaps = loaded[2:]
    assert all(m.get("hidden") is True for m in snaps)
    assert al._snapshot_kind_of(snaps[0]) == al._SNAPSHOT_TS
    assert al._snapshot_kind_of(snaps[1]) == al._SNAPSHOT_TC
    assert "_snapshot_kind" not in snaps[0]

    # Resume: an unchanged chain appends nothing after the reload - no
    # duplicate snapshot, the wire prefix stays byte-stable.
    resumed = al.AgentLoopState()
    resumed.history = loaded
    before = len(resumed.history)
    assert al.refresh_context_snapshots(resumed) == 0
    assert len(resumed.history) == before


def test_saved_count_anchor_after_reload(monkeypatch):
    """The reloaded length is a valid saved_msg_count anchor: the next UI
    persist step writes only genuinely new entries (no reload duplicates)."""
    from core.threads_devagent import create_devagent_thread, load_thread_messages

    state = _seeded_state(monkeypatch)
    tid = create_devagent_thread(title="saved count", orchestrator_slug="dev_agent")
    _persist_like_ui(tid, state.history)

    loaded = load_thread_messages(tid)
    history = list(loaded)
    saved_count = len(loaded)
    history.append({"role": "assistant", "content": "next step answer"})
    _persist_like_ui(tid, history[saved_count:])

    after = load_thread_messages(tid)
    assert len(after) == len(history) == 5
    assert sum(1 for m in after if m.get("hidden")) == 2
    assert after[-1]["content"] == "next step answer"
    assert after[-1].get("hidden") is None


def test_save_thread_messages_keeps_hidden_marker():
    """The full-history save path embeds hidden the same way as append."""
    from core.threads_devagent import (
        create_devagent_thread, save_thread_messages, load_thread_messages,
    )

    tid = create_devagent_thread(title="save hidden", orchestrator_slug="dev_agent")
    snapshot = al._snapshot_content(TS_TEXT)
    save_thread_messages(tid, [
        {"role": "user", "content": "задача"},
        {"role": "system", "content": snapshot, "hidden": True},
    ])

    msgs = load_thread_messages(tid)
    assert [m["content"] for m in msgs] == ["задача", snapshot]
    assert msgs[1].get("hidden") is True
    assert msgs[0].get("hidden") is None
    assert al._snapshot_kind_of(msgs[1]) == al._SNAPSHOT_TS
