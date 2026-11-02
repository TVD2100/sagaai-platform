# -*- coding: utf-8 -*-
"""tests/test_transcript_arg_passthrough.py - arguments that must survive the loop.

A delegation tool has two layers between the model and the work: the agent
loop's ``_delegation_job`` closure and the tool function itself. The closure
builds its call from an explicit whitelist of ``args`` keys, so an argument
that the TOOL accepts but the CLOSURE does not copy is silently dropped.

That failure is invisible from the tool's side: the tool is tested directly,
its defaults make it behave, and the model's ``mode="tail"`` never arrives.
These tests therefore drive the closure, not the tool, and assert that each
recovery argument reaches the tool with the value the model gave it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import agent_loop  # noqa: E402


@pytest.fixture
def forwarded(monkeypatch):
    """Capture the kwargs the closure passes to read_subagent_thread."""
    seen = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "messages": []}

    import core.orchestration.tools as tools
    monkeypatch.setattr(tools, "read_subagent_thread", _fake)
    return seen


def _call(args):
    """Run one delegation call through the closure."""
    return agent_loop._delegation_job(
        "read_subagent_thread", args, parent_slug="dev_agent",
        parent_run_id="root", thread_id="th-parent", parent_config={},
        base_dir=None, lang="en", service=object())


# ─── the recovery arguments reach the tool ─────────────────────────────────


def test_the_mode_reaches_the_tool(forwarded):
    """A mode the closure drops is a mode the model can never use."""
    _call({"run_id": "kid", "mode": "tail"})

    assert forwarded["mode"] == "tail"


def test_the_around_arguments_reach_the_tool(forwarded):
    """Reading around a hit needs three numbers, not just the offset."""
    _call({"run_id": "kid", "mode": "around", "index": 40,
           "before": 3, "after": 7})

    assert forwarded["index"] == 40
    assert forwarded["before"] == 3
    assert forwarded["after"] == 7


def test_an_absent_index_stays_none(forwarded):
    """Zero is a real index; a missing one must not become a window start."""
    _call({"run_id": "kid"})

    assert forwarded["index"] is None


def test_index_zero_is_preserved(forwarded):
    """The first message is addressable like any other."""
    _call({"run_id": "kid", "index": 0})

    assert forwarded["index"] == 0


def test_the_original_arguments_still_work(forwarded):
    """The new parameters must not disturb the paged read."""
    _call({"run_id": "kid", "offset": 20, "limit": 5,
           "include_tool_results": True})

    assert forwarded["offset"] == 20
    assert forwarded["limit"] == 5
    assert forwarded["include_tool_results"] is True
    assert forwarded["mode"] == "window"


def test_the_defaults_are_the_documented_ones(forwarded):
    """A caller that says nothing gets the paged read with 5+5 context."""
    _call({"run_id": "kid"})

    assert forwarded["mode"] == "window"
    assert forwarded["before"] == 5
    assert forwarded["after"] == 5


def test_string_numbers_are_coerced(forwarded):
    """A model may send "3" where the tool wants an int."""
    _call({"run_id": "kid", "index": "7", "before": "2", "after": "4"})

    assert forwarded["index"] == 7
    assert forwarded["before"] == 2
    assert forwarded["after"] == 4
