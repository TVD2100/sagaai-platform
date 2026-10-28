# -*- coding: utf-8 -*-
"""tests/test_orchestrator_subagent_ui.py - the live-subagent surface of the dialog.

The dialog must show what its children are doing and must stop polling the
moment they are done: an unconditional auto-refresh would keep every idle
dialog polling forever. These tests pin the selection rule (only running
children of THIS tree), the idle rule (nothing running - nothing rendered)
and the localization keys the surface depends on.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ui.pages.orchestrator as ORCH  # noqa: E402


class _FakeService:
    """Stand-in for the orchestration registry."""

    def __init__(self, runs):
        self.runs = list(runs)
        self.asked_for = []

    def list_runs(self, root_run_id=None):
        self.asked_for.append(root_run_id)
        return list(self.runs)


def _state_with_root(monkeypatch, root):
    """Point the module at a dialog whose tree root is ``root``."""
    monkeypatch.setattr(ORCH, "_ss",
                        lambda slug, key, default=None: root if key == "active_root_run_id" else default)


def _install_service(monkeypatch, runs):
    fake = _FakeService(runs)
    import core.orchestration.service as service_mod
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)
    return fake


# ─── selection rule ───────────────────────────────────────────────────────


def test_only_running_children_of_the_tree_are_reported(monkeypatch):
    _state_with_root(monkeypatch, "root1")
    fake = _install_service(monkeypatch, [
        {"run_id": "root1", "status": "running"},
        {"run_id": "a", "status": "running", "agent_slug": "dev_agent"},
        {"run_id": "b", "status": "done", "agent_slug": "dev_agent"},
        {"run_id": "c", "status": "running", "agent_slug": "teacher_assistant"},
    ])

    alive = ORCH._live_subagent_runs("dev_agent")

    assert [r["run_id"] for r in alive] == ["a", "c"]
    assert fake.asked_for == ["root1"]


def test_the_root_itself_is_not_a_subagent(monkeypatch):
    """The dialog's own record must not be counted as a live child."""
    _state_with_root(monkeypatch, "root1")
    _install_service(monkeypatch, [{"run_id": "root1", "status": "running"}])

    assert ORCH._live_subagent_runs("dev_agent") == []


def test_a_dialog_without_a_tree_reports_nothing(monkeypatch):
    _state_with_root(monkeypatch, "")
    fake = _install_service(monkeypatch, [
        {"run_id": "a", "status": "running"},
    ])

    assert ORCH._live_subagent_runs("dev_agent") == []
    assert fake.asked_for == []


def test_an_unreadable_registry_does_not_break_the_page(monkeypatch):
    _state_with_root(monkeypatch, "root1")
    import core.orchestration.service as service_mod

    def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(service_mod, "get_service", _boom)

    assert ORCH._live_subagent_runs("dev_agent") == []


# ─── the status line is idle-aware ────────────────────────────────────────


def test_no_status_line_when_nothing_runs(monkeypatch):
    """An idle dialog must not render the auto-refresh fragment."""
    _state_with_root(monkeypatch, "root1")
    _install_service(monkeypatch, [{"run_id": "root1", "status": "running"}])
    calls = []
    monkeypatch.setattr(ORCH.st, "caption", lambda *a, **k: calls.append(a))

    ORCH._render_subagent_status("dev_agent", "en")

    assert calls == []


def test_the_status_line_counts_the_children(monkeypatch):
    _state_with_root(monkeypatch, "root1")
    _install_service(monkeypatch, [
        {"run_id": "a", "status": "running", "agent_slug": "dev_agent"},
        {"run_id": "b", "status": "running", "agent_slug": "dev_agent"},
    ])
    captured = []
    monkeypatch.setattr(ORCH.st, "caption", lambda text, **k: captured.append(text))
    monkeypatch.setattr(ORCH, "t", lambda key, lang=None, **kw: key + str(kw.get("count")))

    ORCH._render_subagent_status("dev_agent", "en")

    assert captured and captured[0].endswith("2")


def test_a_demoted_child_does_not_keep_the_line_up(monkeypatch):
    """A record the registry demoted to ``failed`` must not count as live.

    This is the user-visible half of the zombie fix: the status line used to
    survive on a stored ``running`` claim whose thread was already gone, so the
    dialog kept telling the user that work was in progress.
    """
    _state_with_root(monkeypatch, "root1")
    _install_service(monkeypatch, [
        {"run_id": "z", "status": "failed", "error_code": "stale_run",
         "agent_slug": "dev_agent"},
    ])
    calls = []
    monkeypatch.setattr(ORCH.st, "caption", lambda *a, **k: calls.append(a))

    ORCH._render_subagent_status("dev_agent", "en")

    assert calls == []


def test_only_the_root_running_leaves_the_dialog_idle(monkeypatch):
    """The dialog's own record is not a child; no line without children."""
    _state_with_root(monkeypatch, "root1")
    _install_service(monkeypatch, [
        {"run_id": "root1", "status": "running", "agent_slug": "dev_agent"},
    ])
    calls = []
    monkeypatch.setattr(ORCH.st, "caption", lambda *a, **k: calls.append(a))

    ORCH._render_subagent_status("dev_agent", "en")

    assert calls == []


# ─── localization ─────────────────────────────────────────────────────────


def test_every_language_has_the_new_keys():
    root = os.path.join(os.path.dirname(__file__), "..")
    for folder in ("langs", os.path.join("defaults", "langs")):
        for name in ("en.json", "ru.json", "zh-CN.json"):
            path = os.path.join(root, folder, name)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            assert "orch_subagents_running" in data, path
            assert "orch_subagent_notice" in data, path
            assert "{count}" in data["orch_subagents_running"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
