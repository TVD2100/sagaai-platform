# -*- coding: utf-8 -*-
"""tests/test_tool_classes.py - which tool calls change the project.

The classification exists to answer ONE question on a resume: what has
already been done? Two ways to get it wrong matter more than the rest:

- calling a write a read, which makes a resume skip unfinished work;
- calling a read a write, which makes a resume redo what is already there.

The default for an unknown tool is "mutating" on purpose: an unclassified
write that a resume skips leaves a broken file, while an unclassified read
that a resume repeats only costs tokens.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import tool_classes as C  # noqa: E402


# ─── the classification ────────────────────────────────────────────────────

def test_a_read_tool_is_not_mutating():
    assert C.is_mutating("read_file") is False
    assert C.is_mutating("search_in_files") is False


def test_a_write_tool_is_mutating():
    assert C.is_mutating("propose_file") is True
    assert C.is_mutating("apply_patch") is True


def test_an_unknown_tool_counts_as_mutating():
    """Skipping an unknown write is worse than repeating an unknown read."""
    assert C.is_mutating("some_new_write_tool") is True


def test_an_empty_name_is_not_a_write():
    assert C.is_mutating("") is False


def test_the_two_sets_do_not_overlap():
    """A tool in both sets would make the answer depend on lookup order."""
    assert not (C.READ_TOOLS & C.MUTATING_TOOLS)


def test_every_classified_write_really_appears_in_the_write_set():
    for name in ("write_doc", "snapshot_all", "task_state_update"):
        assert name in C.MUTATING_TOOLS


# ─── reading the calls out of a message ────────────────────────────────────

def _call(tool, **args):
    import json
    return "```json\n" + json.dumps({"tool": tool, "args": args}) + "\n```"


def test_a_fenced_call_is_parsed():
    calls = C.parse_calls(_call("read_file", path="a.py"))

    assert [(c.tool, c.args) for c in calls] == [("read_file", {"path": "a.py"})]


def test_prose_about_a_tool_is_not_a_call():
    """A message that merely MENTIONS a tool must not look like it ran one."""
    text = "I will use propose_file to write the module: path=core/x.py"

    assert C.parse_calls(text) == []


def test_a_broken_block_is_skipped():
    """A half-written call did not execute, so it changed nothing."""
    text = '```json\n{"tool": "propose_file", "args": {\n```'

    assert C.parse_calls(text) == []


def test_several_calls_are_returned_in_order():
    text = _call("read_file", path="a.py") + "\n" + _call("propose_file", path="b.py")

    assert [c.tool for c in C.parse_calls(text)] == ["read_file", "propose_file"]


def test_a_block_without_arguments_still_counts():
    text = '```json\n{"tool": "current_workspace"}\n```'

    calls = C.parse_calls(text)

    assert calls and calls[0].tool == "current_workspace"
    assert calls[0].args == {}


# ─── what a resume needs: the targets already touched ──────────────────────

def test_written_targets_lists_a_written_file():
    messages = [{"role": "assistant",
                 "content": _call("propose_file", path="core/new.py")}]

    assert C.written_targets(messages) == ["core/new.py"]


def test_a_read_never_appears_in_the_written_list():
    """The hint says "already written", so a read must not be in it."""
    messages = [{"role": "assistant",
                 "content": _call("read_file", path="core/read.py")}]

    assert C.written_targets(messages) == []


def test_a_user_message_is_not_scanned():
    """Only the assistant's own calls did the work."""
    messages = [{"role": "user",
                 "content": _call("propose_file", path="core/x.py")}]

    assert C.written_targets(messages) == []


def test_the_list_is_deduplicated_and_sorted():
    """A resume hint is read once; repeating a path only adds noise."""
    messages = [
        {"role": "assistant", "content": _call("propose_file", path="b.py")},
        {"role": "assistant", "content": _call("apply_patch", path="a.py")},
        {"role": "assistant", "content": _call("propose_file", path="b.py")},
    ]

    assert C.written_targets(messages) == ["a.py", "b.py"]


def test_a_call_without_a_target_is_named_by_its_tool():
    """"We ran a snapshot" is still a fact a resume must not repeat."""
    messages = [{"role": "assistant", "content": _call("snapshot_all")}]

    assert C.written_targets(messages) == ["snapshot_all"]


def test_the_doc_argument_is_read_as_a_target():
    """Writing documentation is a write, and its target is the doc name."""
    messages = [{"role": "assistant",
                 "content": _call("write_doc", doc="readme")}]

    assert C.written_targets(messages) == ["readme"]


def test_garbage_messages_do_not_raise():
    """A resume may read a half-written transcript."""
    assert C.written_targets([None, 5, {"role": "assistant", "content": None}]) == []
