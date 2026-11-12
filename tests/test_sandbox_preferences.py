# -*- coding: utf-8 -*-
"""The execution preferences of an employee reach every dispatcher.

The toolbar switches live in the employee config. A dispatcher built for a
dialog, for a delegated child or for a scheduled run must all read the same
values, otherwise the user's choice reaches one of them and stops there -
and a delegated child silently runs in a stronger profile than the dialog
that started it.

These tests pin the resolution itself and the wiring of the three background
builders, which is where the value used to be lost.
"""
from __future__ import annotations

import pytest

from core.sandbox import resolve
from core.sandbox.profiles import DEFAULT_PROFILE, OFF, WORKSPACE_WRITE


@pytest.fixture()
def employee(monkeypatch):
    """Install a configurable employee record and return a setter."""
    state = {"config": {}}

    def fake_get_orchestrator(slug):
        if slug == "missing":
            return None
        return {"slug": slug, "config": dict(state["config"])}

    monkeypatch.setattr("core.orchestrators.get_orchestrator", fake_get_orchestrator)

    def set_config(**values):
        state["config"] = dict(values)

    return set_config


class FakeCore:
    """Stand-in for the tool executor of a dispatcher."""

    def __init__(self):
        self._sandbox_profile = ""
        self._safety_enabled = True
        self._web_search_enabled = False
        self.send_request = None

    def set_send_request(self, fn):
        """Record the provider call the loop would use."""
        self.send_request = fn


class FakeDispatcher:
    """Stand-in for UniversalDevAgent that records the stamped preferences."""

    def __init__(self, *args, **kwargs):
        self.core = FakeCore()
        self.child_disabled_tools = set()
        self.attached = ""

    def attach_orchestrator(self, slug):
        """Record which employee the dispatcher serves."""
        self.attached = slug


@pytest.fixture()
def fake_dispatcher(monkeypatch):
    """Replace the real dispatcher class in both import sites."""
    import dev_agent.universal_agent as universal

    monkeypatch.setattr(universal, "UniversalDevAgent", FakeDispatcher)
    return FakeDispatcher


# ── the resolution itself ───────────────────────────────────────────────────


def test_switched_off_sandbox_resolves_to_the_off_profile(employee):
    """A cleared toolbar checkbox means the off profile, not the default."""
    employee(chat_sandbox=False)
    assert resolve.sandbox_profile("dev_agent") == OFF


def test_switched_on_sandbox_keeps_the_configured_profile(employee):
    """While the checkbox is on, the stored profile is the one in force."""
    employee(chat_sandbox=True, sandbox_profile="read_only")
    assert resolve.sandbox_profile("dev_agent") == "read_only"


def test_a_missing_sandbox_key_means_the_switch_is_on(employee):
    """An install that never wrote the key keeps the isolation it had."""
    employee(sandbox_profile="network")
    assert resolve.sandbox_profile("dev_agent") == "network"


def test_an_unset_profile_means_the_policy_default(employee):
    """An empty name is returned as is: the policy resolves it, not this module."""
    employee(chat_sandbox=True)
    assert resolve.sandbox_profile("dev_agent") == ""
    assert DEFAULT_PROFILE == WORKSPACE_WRITE


def test_an_unknown_profile_is_never_replaced(employee):
    """A typo must reach the policy and be refused there."""
    employee(chat_sandbox=True, sandbox_profile="no_such_profile")
    assert resolve.sandbox_profile("dev_agent") == "no_such_profile"


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "  OFF  "])
def test_string_forms_of_false_clear_the_switch(employee, value):
    """A config written by a form may carry the word, not the boolean."""
    employee(chat_sandbox=value)
    assert resolve.sandbox_profile("dev_agent") == OFF


# ── full access: a grant of its own, never a side effect ────────────────────


def test_a_missing_env_key_keeps_the_environment_isolated(employee):
    """An install that never wrote the key keeps the filtered environment."""
    employee(chat_sandbox=True)
    assert resolve.env_isolation("dev_agent") is True


def test_full_access_is_granted_only_explicitly(employee):
    """A stored false is the grant; everything else is the isolated form."""
    employee(env_isolation=False)
    assert resolve.env_isolation("dev_agent") is False


def test_full_access_survives_a_cleared_toolbar_checkbox(employee):
    """Two switches that both mean \"no OS isolation\" stay distinct values.

    Clearing the toolbar checkbox must not read as a full-access grant: the
    environment stays filtered although the machine is unconfined.
    """
    employee(chat_sandbox=False)
    assert resolve.env_isolation("dev_agent") is True


def test_full_access_forces_the_off_profile(employee):
    """A granted run needs the machine, so it cannot stay inside a boundary."""
    employee(env_isolation=False, chat_sandbox=True, sandbox_profile="read_only")
    assert resolve.sandbox_profile("dev_agent") == OFF


def test_full_access_does_not_touch_the_confirmation(employee):
    """Access and confirmation are separate: a grant keeps the gate on."""
    employee(env_isolation=False, chat_safety_mode=True)
    assert resolve.env_isolation("dev_agent") is False
    assert resolve.safety_enabled("dev_agent") is True


def test_the_stamp_carries_the_access_grant(employee, fake_dispatcher):
    """The dialog builder hands the grant to the execution tools."""
    from core.orchestration import dialog_runner

    employee(env_isolation=False, chat_safety_mode=True)
    dispatcher = dialog_runner.build_dispatcher(
        "dev_agent", "thread", safety=True, web_search=False)
    assert dispatcher.core._env_isolation is False
    assert dispatcher.core._sandbox_profile == OFF


def test_the_stamp_defaults_to_the_isolated_form(employee, fake_dispatcher):
    """A dispatcher of an employee that never wrote the key stays filtered."""
    from core.orchestration import dialog_runner

    employee(chat_sandbox=True)
    dispatcher = dialog_runner.build_dispatcher(
        "dev_agent", "thread", safety=True, web_search=False)
    assert dispatcher.core._env_isolation is True


# ── the two switches are independent ────────────────────────────────────────


def test_clearing_the_sandbox_does_not_clear_the_confirmation(employee):
    """Isolation off, confirmations still on - the documented split."""
    employee(chat_sandbox=False, chat_safety_mode=True)
    assert resolve.sandbox_profile("dev_agent") == OFF
    assert resolve.safety_enabled("dev_agent") is True


def test_clearing_the_confirmation_does_not_clear_the_sandbox(employee):
    """Confirmations off, isolation still on."""
    employee(chat_sandbox=True, sandbox_profile="read_only", chat_safety_mode=False)
    assert resolve.sandbox_profile("dev_agent") == "read_only"
    assert resolve.safety_enabled("dev_agent") is False


def test_a_missing_safety_key_keeps_the_gate_on(employee):
    """Silence in the settings is not permission to skip a confirmation."""
    employee(chat_sandbox=False)
    assert resolve.safety_enabled("dev_agent") is True


def test_an_unknown_employee_falls_back_to_the_defaults(employee):
    """A missing record is read through the defaults, not through an error."""
    assert resolve.sandbox_profile("missing") == ""
    assert resolve.safety_enabled("missing") is True


# ── the wiring: every builder stamps the resolved values ────────────────────


def test_the_child_builder_inherits_the_employee_preferences(employee, fake_dispatcher):
    """A delegated child runs under the preferences of its employee.

    This is the regression that started the change: the user cleared the
    toolbar checkbox, delegated the work, and the child brought the sandbox
    back because its builder never asked the settings.
    """
    from core.orchestration import child_runner

    employee(chat_sandbox=False, chat_safety_mode=False)
    dispatcher = child_runner.build_child_dispatcher(
        "dev_agent", "/tmp/project", "child-thread", 0, {},
        {"text": "card", "service": "s", "model": "m"}, "ru", set(),
    )
    assert dispatcher.core._sandbox_profile == OFF
    assert dispatcher.core._safety_enabled is False
    assert dispatcher.core.send_request is not None


def test_the_child_builder_keeps_the_gate_when_the_employee_asked_for_it(
        employee, fake_dispatcher):
    """The inheritance is not a blanket loosening: a stored True stays True."""
    from core.orchestration import child_runner

    employee(chat_sandbox=True, sandbox_profile="read_only", chat_safety_mode=True)
    dispatcher = child_runner.build_child_dispatcher(
        "dev_agent", "/tmp/project", "child-thread", 0, {},
        {"text": "card", "service": "s", "model": "m"}, "ru", set(),
    )
    assert dispatcher.core._sandbox_profile == "read_only"
    assert dispatcher.core._safety_enabled is True


def test_the_dialog_builder_lets_the_request_tighten_the_gate(
        employee, fake_dispatcher):
    """An HTTP caller may ask for confirmations the settings no longer have.

    ``safety=True`` is an explicit request for a stricter run, so it wins
    over the stored preference; the profile has no request field and always
    comes from the settings.
    """
    from core.orchestration import dialog_runner

    employee(chat_sandbox=False, chat_safety_mode=False)
    dispatcher = dialog_runner.build_dispatcher(
        "dev_agent", "thread", safety=True, web_search=False)
    assert dispatcher.core._sandbox_profile == OFF
    assert dispatcher.core._safety_enabled is True


def test_the_dialog_builder_honours_a_caller_that_waives_the_gate(
        employee, fake_dispatcher):
    """A caller passing ``safety=False`` gets an unguarded run."""
    from core.orchestration import dialog_runner

    employee(chat_sandbox=False, chat_safety_mode=True)
    dispatcher = dialog_runner.build_dispatcher(
        "dev_agent", "thread", safety=False, web_search=False)
    assert dispatcher.core._sandbox_profile == OFF
    assert dispatcher.core._safety_enabled is False


def test_the_schedule_builder_reads_the_employee_preferences(
        employee, fake_dispatcher, monkeypatch):
    """A schedule has no dialog to inherit from, so it reads the settings."""
    from core.schedules import runner as schedule_runner

    employee(chat_sandbox=False, chat_safety_mode=False)
    monkeypatch.setattr(
        "core.orchestrators.build_assistant_dicts",
        lambda *a, **k: ({"text": "card"}, {"text": "weak"}),
    )
    schedule = type("Schedule", (), {
        "workspace": "/tmp/project", "agent_slug": "dev_agent",
    })()
    dispatcher, _strong, _weak = schedule_runner._make_dispatcher(schedule, "thread")
    assert dispatcher.core._sandbox_profile == OFF
    assert dispatcher.core._safety_enabled is False

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
