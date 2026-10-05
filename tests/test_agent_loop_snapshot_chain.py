# -*- coding: utf-8 -*-
"""Tests for the append-only context snapshot chain in dev_agent.agent_loop.

Cache-friendly harness contract: the payload of request N+1 is a strict
extension of the payload of request N as long as the task state / thread
context did not change. Context blocks are therefore appended to
``state.history`` as hidden system snapshots ONLY on content change; the
legacy per-request re-injection between the economy window and the step
payload is gone.

Covered:

- refresh appends the task-state / thread-context snapshots on change and
  no-ops on identical content;
- append-only: the previous wire list stays a prefix of the next one when a
  snapshot is refreshed;
- sliding window: a snapshot that left the sent window is re-appended to the
  tail, and only while it is actually missing;
- DB round-trip: a snapshot without the in-memory ``_snapshot_kind`` marker
  (loaded from the thread store) is still recognised by its content prefix
  and is not duplicated.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import agent_loop as al


TS_TEXT = "CURRENT TASK STATE:\ntask: demo\nprogress: 1/2"
TC_TEXT = "## CURRENT THREAD ARTIFACTS DIR\nthread_id: t1"


def _state_with_history():
    state = al.AgentLoopState()
    state.history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    return state


def _patch_contexts(monkeypatch, ts_text=TS_TEXT, tc_text=TC_TEXT):
    """Deterministic snapshot sources: one task-state block and one thread block."""
    monkeypatch.setattr(al, "_maybe_task_state_context", lambda: ts_text)
    monkeypatch.setattr(al, "_maybe_thread_context", lambda state: tc_text)


def _step_wire(state):
    """Emulate the calling_llm call-site sequence for one request."""
    al.refresh_context_snapshots(state)
    return list(state.history)


def test_refresh_appends_once_and_keeps_prefix_stable(monkeypatch):
    _patch_contexts(monkeypatch)
    state = _state_with_history()

    wire1 = _step_wire(state)
    wire2 = _step_wire(state)
    # Nothing changed: the next request repeats the very same history.
    assert wire2 == wire1
    assert len(wire1) == 4  # 2 base messages + 2 snapshots
    assert al._snapshot_kind_of(wire1[-2]) == al._SNAPSHOT_TS
    assert al._snapshot_kind_of(wire1[-1]) == al._SNAPSHOT_TC

    # The content changed: a NEW snapshot is appended and the previous wire
    # stays a strict prefix of the new one (append-only, no rewrite).
    _patch_contexts(monkeypatch, ts_text=TS_TEXT + "\n- step_2 done")
    wire3 = _step_wire(state)
    assert wire3[:len(wire2)] == wire2
    assert len(wire3) == len(wire2) + 1
    assert "step_2 done" in wire3[-1]["content"]
    assert al._snapshot_kind_of(wire3[-1]) == al._SNAPSHOT_TS


def test_visibility_reappends_snapshot_after_window_slide(monkeypatch):
    _patch_contexts(monkeypatch)
    state = _state_with_history()
    _step_wire(state)

    # Emulate an outgoing window that trimmed everything (both snapshots left).
    effective = []
    before = len(state.history)
    out = al._ensure_snapshot_visibility(state, effective)
    # One fresh copy of each kind was appended to the tail and the chain
    # only GREW (append-only).
    assert len(state.history) == before + 2
    assert out[-2:] == state.history[-2:]
    assert al._snapshot_kind_of(out[-2]) == al._SNAPSHOT_TS
    assert al._snapshot_kind_of(out[-1]) == al._SNAPSHOT_TC

    # Second call with the snapshots already visible adds nothing.
    history_len = len(state.history)
    out2 = al._ensure_snapshot_visibility(state, list(state.history))
    assert len(state.history) == history_len
    assert len(out2) == history_len


def test_db_roundtrip_snapshot_recognised_by_prefix(monkeypatch):
    _patch_contexts(monkeypatch)
    state = _state_with_history()
    _step_wire(state)

    # Simulate a DB round-trip: only role/content/ts survive (no marker).
    plain = [dict(role=m.get("role"), content=m.get("content"), ts=m.get("ts"))
             for m in state.history]
    for m in plain:
        assert "_snapshot_kind" not in m
    state.history = plain

    assert al._snapshot_kind_of(plain[-2]) == al._SNAPSHOT_TS
    assert al._snapshot_kind_of(plain[-1]) == al._SNAPSHOT_TC
    before = len(state.history)
    assert al.refresh_context_snapshots(state) == 0  # no duplicate append
    assert len(state.history) == before


def test_snapshot_content_carries_supersede_note():
    text = al._snapshot_content("BLOCK")
    assert text.startswith("BLOCK")
    assert al._SNAPSHOT_SUPERSEDE_NOTE.strip() in text
