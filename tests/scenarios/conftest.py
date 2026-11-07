# -*- coding: utf-8 -*-
"""tests/scenarios/conftest.py - state restoration for the scenario suite.

The scenarios render the real orchestrator pages, reset dialogs and open the
empty (no folder selected) state - that IS the behaviour under test. Switching
the process-global workspace state is therefore expected here, so the folder
that does it also owns the restore: every scenario hands the state it changed
back through ``_restore_dev_agent_state``.
"""
import pytest


@pytest.fixture(autouse=True)
def _restore_scenario_state(_restore_dev_agent_state):
    """Return the workspace state after each scenario that changed it."""
    yield
