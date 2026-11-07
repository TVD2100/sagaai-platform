# -*- coding: utf-8 -*-
"""tests/scenarios/test_sandbox_scenario.py - user-level sandbox scenarios.

Scenarios walk the PUBLIC execution tool ``ToolExecutor.run_code`` the way a
user experiences it, plus the settings panel that reports what this machine
can honour:

  Scenario 1 - harmless code runs and the journal holds the outcome.
  Scenario 2 - a profile whose isolation this machine cannot reach is refused
               BEFORE anything executes, and the refusal is journaled.
  Scenario 3 - the settings panel shows every profile with its real status.
  Scenario 4 - a caller cannot widen a limit through the tool argument.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.sandbox import profiles
from core.sandbox.contracts import ResourceLimits, SandboxRequest, SandboxResult
from core.sandbox.policy import SandboxProvider, SandboxRegistry
from storage import sandbox_journal


class _StubProvider(SandboxProvider):
    """Provider that records the run instead of spawning a process."""

    name = "stub"
    isolation = "process"

    def __init__(self) -> None:
        self.calls = []

    def available(self) -> bool:
        return True

    def run(self, request, profile, limits):
        self.calls.append((request, profile, limits))
        return SandboxResult(ok=True, provider=self.name, isolation=self.isolation,
                             returncode=0, stdout="scenario-ok", duration_ms=3)


@pytest.fixture()
def engine(tmp_path):
    """A throwaway database carrying the journal table."""
    from storage.db import _ensure_sandbox_tables

    eng = create_engine("sqlite:///" + str(tmp_path / "scenario.db"),
                        connect_args={"check_same_thread": False})
    _ensure_sandbox_tables(eng)
    yield eng
    eng.dispose()


def _request(profile: str = profiles.LEGACY_PROCESS) -> SandboxRequest:
    return SandboxRequest(tool="run_code", argv=("python", "-c", "print(1)"),
                          cwd=".", env={"PATH": "/usr/bin"}, profile=profile,
                          thread_id="thread-scenario")


def test_scenario_harmless_code_runs_and_is_journaled(engine):
    """Scenario 1: a normal snippet runs and leaves a journal row.

    Given an available process provider and the journal,
    when  the user runs harmless code,
    then  the run succeeds and the journal holds its outcome.
    """
    from core.sandbox import runner

    stub = _StubProvider()
    result = runner.execute(_request(), profile=profiles.LEGACY_PROCESS,
                            providers=[stub], journal_engine=engine)

    assert result.ok is True and result.stdout == "scenario-ok"
    rows = sandbox_journal.recent_runs(engine, thread_id="thread-scenario")
    assert len(rows) == 1
    assert rows[0]["ok"] == 1 and rows[0]["denied"] == 0
    assert rows[0]["isolation"] == "process"


def test_scenario_unreachable_isolation_is_refused_and_journaled(engine):
    """Scenario 2: the network profile is refused on a process-only machine.

    Given a provider set that only reaches 'process' isolation,
    when  a run asks for the network profile (which requires 'os'),
    then  nothing executes, the caller gets a denial and the journal has a
    refused row - the guarantee stays auditable.
    """
    from core.sandbox import runner

    stub = _StubProvider()
    result = runner.execute(_request(profiles.NETWORK), profile=profiles.NETWORK,
                            providers=[stub], journal_engine=engine)

    assert result.denied is True and result.ok is False
    assert stub.calls == [], "a refused run must never reach a provider"
    rows = sandbox_journal.recent_runs(engine, thread_id="thread-scenario")
    assert rows[0]["denied"] == 1
    assert "refused" in sandbox_journal.summarise(rows[0])


def test_scenario_settings_panel_reports_every_profile():
    """Scenario 3: the settings panel lists each profile with a real status.

    Given the settings panel of an orchestrator,
    when  it renders on this machine,
    then  every declared profile appears and each status matches what the
    providers of this machine can actually honour.
    """
    from tests._st_mock import install_streamlit_mock

    with install_streamlit_mock() as st:
        st.session_state.update({"ui_lang": "English"})
        import ui.pages.orchestrator as orch_mod

        # The panel resolves streamlit through the page facade, so the facade
        # is bound to this mock first - the same way the other page tests
        # install their double.
        orch_mod.st = st
        from ui.pages.orchestrator_sandbox import render_sandbox_settings

        render_sandbox_settings("dev_agent", "English")

    tables = [args[0] for name, args, _kw in st.calls if name == "table"]
    assert tables, "the sandbox panel rendered no tables"
    # The panel renders several tables (providers, profiles, recent runs);
    # the profile one is identified by its keys, not by its position, so
    # adding another section cannot silently turn this test into a check of
    # the wrong table.
    profile_rows = next(
        (table for table in tables
         if table and isinstance(table[0], dict) and "profile" in table[0]
         and "isolation" in table[0]),
        None,
    )
    assert profile_rows, "the panel rendered no profile table"
    listed = {row["profile"] for row in profile_rows}
    assert listed == set(profiles.profile_names())
    status = {row["profile"]: row["status"] for row in profile_rows}
    from core.sandbox.policy import select_provider
    from core.sandbox.providers import default_providers

    providers = default_providers()
    for name in profiles.profile_names():
        expected = ("available" if select_provider(profiles.get_profile(name), providers)
                    is not None else "refused (isolation unavailable)")
        assert status[name] == expected, name
    # The legacy profile wraps nothing, so it never depends on the machine.
    assert status[profiles.LEGACY_PROCESS] == "available"


def test_scenario_a_tool_argument_cannot_widen_a_limit():
    """Scenario 4: the caller-side timeout can only tighten the profile.

    Given the legacy profile with its own timeout ceiling,
    when  a caller passes a much larger timeout,
    then  the provider receives the profile ceiling, not the argument.
    """
    stub = _StubProvider()
    profile = profiles.get_profile(profiles.LEGACY_PROCESS)
    assert profile is not None
    huge = profile.limits.timeout_s * 10
    SandboxRegistry([stub]).run(profiles.LEGACY_PROCESS, _request(),
                                limits_override=ResourceLimits(timeout_s=huge))
    assert stub.calls[0][2].timeout_s == profile.limits.timeout_s


def test_scenario_sandbox_panel_opens_the_other_tab():
    """Scenario 5: the sandbox panel comes first in the Other tab.

    Given the settings page of an orchestrator,
    when  the Other tab is rendered,
    then  the sandbox panel paints before the miscellaneous settings, so a
    user looking for how code is isolated does not scroll past them.
    """
    from unittest.mock import patch

    from tests._st_mock import StopRerun, install_streamlit_mock

    slug = "dev_agent"
    orch = {"slug": slug, "name": "DevAgent", "prompt_text": "P",
            "config": {}, "is_builtin": True, "max_steps": 100}
    with install_streamlit_mock() as st:
        st.session_state.update({"ui_lang": "English"})
        import ui.pages.orchestrator as orch_mod
        import ui.pages.orchestrator_settings as settings_mod

        orch_mod.st = st
        settings_mod.st = st
        with patch.object(settings_mod, "get_orchestrator", return_value=dict(orch)), \
             patch.object(orch_mod, "get_orchestrator", return_value=dict(orch)):
            try:
                settings_mod.page_orchestrator_settings(slug)
            except StopRerun:
                pass

    sandbox_at = next(i for i, (name, _a, _k) in enumerate(st.calls)
                      if name == "table")
    other_at = next(i for i, (name, _a, k) in enumerate(st.calls)
                    if name == "number_input"
                    and str(k.get("key", "")).startswith("orch_max_steps_"))
    assert sandbox_at < other_at, "the sandbox panel must open the Other tab"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
