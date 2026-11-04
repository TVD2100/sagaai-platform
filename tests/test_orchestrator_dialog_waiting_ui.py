# -*- coding: utf-8 -*-
"""tests/test_orchestrator_dialog_waiting_ui.py - the waiting surface.

A parked child is invisible to the running line: it holds no pool slot and
its thread is gone. These tests pin the three user-visible facts the dialog
must show anyway - that a child waits, what it asks, and that the fact
survives a page reload - plus the rule that only a QUESTION counts as a wait.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ui.pages.orchestrator as ORCH  # noqa: E402


class _FakeService:
    """Stand-in for the orchestration registry."""

    def __init__(self, runs, roots=None):
        self.runs = list(runs)
        self.roots = dict(roots or {})
        self.asked_for = []
        self.roots_asked_for = []

    def list_runs(self, root_run_id=None):
        self.asked_for.append(root_run_id)
        return list(self.runs)

    def roots_for_thread(self, thread_id):
        self.roots_asked_for.append(thread_id)
        return list(self.roots.get(thread_id) or [])


def _install(monkeypatch, runs, *, root="root1", thread_id="", roots=None):
    """Point the page at a dialog tree and an in-memory registry."""
    fake = _FakeService(runs, roots)
    import core.orchestration.service as service_mod
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)

    def _fake_ss(slug, key, default=None):
        if key == "active_root_run_id":
            return root
        if key == "thread_id":
            return thread_id
        return default

    monkeypatch.setattr(ORCH, "_ss", _fake_ss)
    written = {}

    def _fake_set_ss(slug, key, value):
        written[key] = value

    monkeypatch.setattr(ORCH, "_set_ss", _fake_set_ss)
    fake.written = written
    return fake


def _parked(run_id="a", agent="dev_agent", text="Which format?", **extra):
    record = {
        "run_id": run_id, "agent_slug": agent, "status": "awaiting_input",
        "await_reason": "question",
        "question": {"text": text, "options": ["markdown", "docx"],
                     "done": "read the sources", "blocked": "need a format"},
    }
    record.update(extra)
    return record


def _render(monkeypatch):
    """Render the status line and return the captions it emitted."""
    captions = []
    monkeypatch.setattr(ORCH.st, "caption",
                        lambda text, **kw: captions.append(str(text)))
    monkeypatch.setattr(ORCH, "t",
                        lambda key, lang=None, **kw: key + "|" + str(kw))
    ORCH._render_subagent_status("dev_agent", "en")
    return captions


# ─── selection rule ───────────────────────────────────────────────────────


def test_a_parked_child_is_reported_as_waiting(monkeypatch):
    _install(monkeypatch, [_parked()])

    waiting = ORCH._waiting_subagent_runs("dev_agent")

    assert [r["run_id"] for r in waiting] == ["a"]


def test_a_running_child_is_not_waiting(monkeypatch):
    _install(monkeypatch, [{"run_id": "a", "status": "running",
                            "agent_slug": "dev_agent"}])

    assert ORCH._waiting_subagent_runs("dev_agent") == []


def test_a_run_out_of_continuations_is_not_a_wait(monkeypatch):
    """No question means nobody can answer: resuming is a different action."""
    _install(monkeypatch, [{"run_id": "a", "status": "awaiting_input",
                            "await_reason": "max_steps",
                            "agent_slug": "dev_agent"}])

    assert ORCH._waiting_subagent_runs("dev_agent") == []


def test_an_answered_question_is_no_longer_a_wait(monkeypatch):
    _install(monkeypatch, [_parked(question={"text": "Which format?",
                                             "answer": "markdown"})])

    assert ORCH._waiting_subagent_runs("dev_agent") == []


def test_the_dialog_root_is_never_reported_as_its_own_child(monkeypatch):
    _install(monkeypatch, [_parked(run_id="root1")])

    assert ORCH._waiting_subagent_runs("dev_agent") == []


def test_a_missing_question_object_is_not_a_wait(monkeypatch):
    _install(monkeypatch, [{"run_id": "a", "status": "awaiting_input",
                            "await_reason": "question", "agent_slug": "dev"}])

    assert ORCH._waiting_subagent_runs("dev_agent") == []


def test_an_unreadable_registry_hides_the_line(monkeypatch):
    import core.orchestration.service as service_mod

    def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(service_mod, "get_service", _boom)
    monkeypatch.setattr(ORCH, "_ss", lambda slug, key, default=None: "root1")

    assert ORCH._waiting_subagent_runs("dev_agent") == []


# ─── the branch is recoverable after a reload ─────────────────────────────


def test_the_root_is_recovered_from_the_thread(monkeypatch):
    """A reloaded page forgets the root; the store still knows it."""
    fake = _install(monkeypatch, [], root="", thread_id="t1",
                    roots={"t1": ["recovered", "older"]})

    root = ORCH._dialog_tree_root("dev_agent")

    assert root == "recovered"
    assert fake.roots_asked_for == ["t1"]
    assert fake.written.get("active_root_run_id") == "recovered"


def test_a_known_root_is_not_looked_up_again(monkeypatch):
    fake = _install(monkeypatch, [], root="root1", thread_id="t1",
                    roots={"t1": ["recovered"]})

    assert ORCH._dialog_tree_root("dev_agent") == "root1"
    assert fake.roots_asked_for == []


def test_a_thread_without_records_has_no_root(monkeypatch):
    fake = _install(monkeypatch, [], root="", thread_id="t1", roots={})

    assert ORCH._dialog_tree_root("dev_agent") == ""
    assert fake.written == {}


def test_a_new_dialog_asks_for_no_root(monkeypatch):
    fake = _install(monkeypatch, [], root="", thread_id="")

    assert ORCH._dialog_tree_root("dev_agent") == ""
    assert fake.roots_asked_for == []


def test_a_waiting_child_stays_visible_after_a_reload(monkeypatch):
    """End to end: no session root, but the waiting line still appears."""
    _install(monkeypatch, [_parked()], root="", thread_id="t1",
             roots={"t1": ["root1"]})

    captions = _render(monkeypatch)

    assert any("orch_subagent_waiting" in text for text in captions)


# ─── the status line ──────────────────────────────────────────────────────


def test_the_waiting_line_names_the_agent_and_the_question(monkeypatch):
    _install(monkeypatch, [_parked()])

    captions = _render(monkeypatch)

    line = [c for c in captions if "orch_subagent_waiting" in c][0]
    assert "dev_agent" in line
    assert "Which format?" in line


def test_a_long_question_is_shortened(monkeypatch):
    _install(monkeypatch, [_parked(text="x" * 500)])

    captions = _render(monkeypatch)

    line = [c for c in captions if "orch_subagent_waiting" in c][0]
    assert "..." in line
    assert len(line) < 400


def test_a_waiting_child_is_shown_without_a_running_child(monkeypatch):
    """The waiting line must not depend on the running one being present."""
    _install(monkeypatch, [_parked()])

    captions = _render(monkeypatch)

    assert captions, "a parked child produced no line at all"
    assert not any("orch_subagents_running" in c for c in captions)


def test_many_waiting_children_are_summarised(monkeypatch):
    runs = [_parked(run_id="a"), _parked(run_id="b"),
            _parked(run_id="c"), _parked(run_id="d")]
    _install(monkeypatch, runs)

    captions = _render(monkeypatch)

    assert any("orch_subagents_waiting" in c for c in captions)


def test_an_idle_dialog_renders_nothing(monkeypatch):
    _install(monkeypatch, [{"run_id": "root1", "status": "running"}])

    assert _render(monkeypatch) == []


# ─── the question in the feed ─────────────────────────────────────────────


def test_the_parked_question_is_rendered_with_options(monkeypatch):
    rendered = []
    monkeypatch.setattr(ORCH.st, "markdown",
                        lambda text, **kw: rendered.append(str(text)))
    monkeypatch.setattr(ORCH.st, "caption",
                        lambda text, **kw: rendered.append(str(text)))

    ORCH._render_parked_question(
        {"text": "Which format?", "options": ["markdown", "docx"],
         "done": "read the sources", "blocked": "need a format"},
        "en")

    joined = "\n".join(rendered)
    assert "Which format?" in joined
    assert "- markdown" in joined
    assert "- docx" in joined
    assert "read the sources" in joined
    assert "need a format" in joined


def test_an_empty_question_renders_nothing(monkeypatch):
    rendered = []
    monkeypatch.setattr(ORCH.st, "markdown",
                        lambda text, **kw: rendered.append(str(text)))
    monkeypatch.setattr(ORCH.st, "caption",
                        lambda text, **kw: rendered.append(str(text)))

    ORCH._render_parked_question({}, "en")

    assert rendered == []


def test_the_ask_parent_result_is_labelled_and_expanded(monkeypatch):
    """A parked tool result is a question, not a finished operation."""
    expanders = []
    markdowns = []

    class _Expander:
        def __init__(self, header, expanded=False):
            expanders.append((header, expanded))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ORCH.st, "expander", _Expander)
    monkeypatch.setattr(ORCH.st, "markdown",
                        lambda text, **kw: markdowns.append(str(text)))
    monkeypatch.setattr(ORCH.st, "caption", lambda *a, **kw: None)
    monkeypatch.setattr(ORCH.st, "code", lambda *a, **kw: None)
    monkeypatch.setattr(ORCH, "t", lambda key, lang=None, **kw: key)

    ORCH._render_tool_result({
        "type": "tool_result", "tool": "ask_parent",
        "result": {"ok": True, "parked": True, "run_id": "a",
                   "await_reason": "question",
                   "question": {"text": "Which format?"}},
    }, "en")

    header, expanded = expanders[0]
    assert "orch_subagent_question" in header
    assert expanded is True
    assert any("Which format?" in text for text in markdowns)


def test_an_ordinary_result_is_not_labelled_as_a_question(monkeypatch):
    expanders = []

    class _Expander:
        def __init__(self, header, expanded=False):
            expanders.append((header, expanded))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ORCH.st, "expander", _Expander)
    monkeypatch.setattr(ORCH.st, "markdown", lambda *a, **kw: None)
    monkeypatch.setattr(ORCH.st, "caption", lambda *a, **kw: None)
    monkeypatch.setattr(ORCH.st, "code", lambda *a, **kw: None)
    monkeypatch.setattr(ORCH, "t", lambda key, lang=None, **kw: key)

    ORCH._render_tool_result({
        "type": "tool_result", "tool": "read_file",
        "result": {"ok": True, "path": "a.py", "total_lines": 10},
    }, "en")

    header, expanded = expanders[0]
    assert "orch_subagent_question" not in header
    assert expanded is False


# ─── localization ─────────────────────────────────────────────────────────


def test_every_language_has_the_waiting_keys():
    import json

    root = os.path.join(os.path.dirname(__file__), "..")
    for folder in ("langs", os.path.join("defaults", "langs")):
        for name in ("en.json", "ru.json", "zh-CN.json"):
            path = os.path.join(root, folder, name)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for key in ("orch_subagents_waiting", "orch_subagent_waiting",
                        "orch_subagent_question"):
                assert key in data, (path, key)
            assert "{count}" in data["orch_subagents_waiting"]
            assert "{question}" in data["orch_subagent_waiting"]
            assert "{agent}" in data["orch_subagent_waiting"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
