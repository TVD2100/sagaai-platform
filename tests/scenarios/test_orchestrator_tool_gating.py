# -*- coding: utf-8 -*-
"""tests/scenarios/test_orchestrator_tool_gating.py - user-level scenario
for the orchestrator tool catalog and the disabled-tools blacklist.

Scenarios (given -> when -> then), walking the same public entry points
the platform and the UI use:

  Scenario 1 - every orchestrator prompt gets the English "## Available tools"
               catalog: a plain prompt keeps its text, gains the block, and
               the catalog lists only ENABLED tools (a disabled tool is
               absent while its neighbours remain).

  Scenario 2 - gating end-to-end through the agent dispatcher: an employee
               with read_file disabled cannot call it (structured error,
               nothing executed), other tools keep working, a custom function
               is listed in the prompt and gets blocked as well, and the
               model is never told it could call a disabled tool.

  Scenario 3 - the Functions tab renders the "System functions" section with
               one checkbox per system tool, and unchecking read_file + Save
               really persists it (new prompt without read_file, dispatcher
               blocks it).

  Scenario 4 - an employee realises the mistake, re-enables the tool via
               set_disabled_tools and reloads the page: the checkbox shows
               checked, the prompt lists the tool again and dispatch works.

  Scenario 5 - the canonical dev_agent and ya_agent prompts don't duplicate
               the tool catalog: they delegate the full list to the
               auto-added '## Available tools' block, keep their in-line
               usage rules, and contain no tool-catalog table.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR that isolates orchestrators from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_gating_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    from storage.db import reset_engine, reset_devagent_engine
    reset_engine()
    reset_devagent_engine()

    yield tmp

    reset_engine()
    reset_devagent_engine()

    if old_data_dir:
        os.environ["SAGAAI_DATA_DIR"] = old_data_dir
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def orchid(isolated_data_dir):
    """A custom employee orchestrator with a known plain prompt."""
    from core.orchestrators import create_orchestrator
    slug = "gating_emp"
    assert create_orchestrator(slug, "GatingEmp", prompt_text="Be helpful.")
    return slug


# ─── Scenario 1: the prompt catalog lists only enabled tools ----------------

def test_prompt_gets_tool_catalog_excluding_disabled(isolated_data_dir, orchid):
    """Given an employee with a plain prompt and read_file disabled,
    when  the (strong assistant) prompt is built,
    then  the prompt keeps the base text, gains the '## Available tools'
          English block, read_file is absent from it while a neighbour tool
          is present, and the model is never told it could call read_file.
    """
    from core.orchestrators import (
        build_assistant_dicts, set_disabled_tools, get_disabled_tools,
    )

    assert set_disabled_tools(orchid, ["read_file"])
    assert get_disabled_tools(orchid) == ["read_file"]

    strong, _weak = build_assistant_dicts(orchid)
    prompt = strong["text"]

    assert prompt.startswith("Be helpful.")
    assert "## Available tools" in prompt
    assert "- `read_file`" not in prompt
    assert "- `scan_folder`" in prompt  # neighbour stays enabled


# ─── Scenario 2: dispatch blocks a disabled tool, not everything ------------

CUSTOM_FN_CODE = "def invoke(**kwargs):\n    return {'ok': True, 'source': 'sink_custom'}\n"


def test_gating_end_to_end_through_dispatcher(isolated_data_dir, orchid):
    """Given read_file and custom 'sink_probe' disabled on the employee,
    when  the agent (attached to that orchestrator) dispatches them and a
          still-enabled tool,
    then  the disabled calls return a structured disabled-error and execute
          nothing, the enabled tool works, and the prompt catalog hides both
          disabled names (the model cannot be misled).
    """
    from core.orchestrators import set_disabled_tools, build_assistant_dicts
    from core.orchestrator_folders import save_orchestrator_function
    from dev_agent.universal_agent import UniversalDevAgent

    assert save_orchestrator_function(orchid, "sink_probe", CUSTOM_FN_CODE)
    assert set_disabled_tools(orchid, ["read_file", "sink_probe"])

    agent = UniversalDevAgent()
    agent.attach_orchestrator(orchid)

    blocked = agent.dispatch("read_file", {"path": "missing.txt"})
    assert blocked["ok"] is False
    assert blocked.get("disabled") is True
    assert "disabled" in blocked.get("error", "").lower()

    # The custom function is loaded but must not execute while disabled.
    assert "sink_probe" in agent._extra
    blocked_custom = agent.dispatch("sink_probe", {})
    assert blocked_custom["ok"] is False
    assert blocked_custom.get("disabled") is True

    enabled = agent.dispatch("scan_folder", {})
    assert enabled["ok"] is True

    strong, _weak = build_assistant_dicts(orchid)
    prompt = strong["text"]
    assert "## Available tools" in prompt
    assert "- `read_file`" not in prompt
    assert "- `sink_probe`" not in prompt
    assert "- `scan_folder`" in prompt


# ─── Scenario 3: the Functions tab checkboxes persist the choice ------------

def test_functions_tab_uncheck_and_save_persists(isolated_data_dir, orchid):
    """Given the employee's Functions settings tab open with all system-tool
          checkboxes checked,
    when  the user unchecks read_file and clicks the save button,
    then  the real repository stores disabled_tools=['read_file'], the newly
          built prompt no longer lists read_file, and dispatch blocks it.
    """
    from core.orchestrators import get_disabled_tools, build_assistant_dicts
    from dev_agent.universal_agent import UniversalDevAgent

    with install_streamlit_mock() as st:
        settings_mod = importlib.import_module("ui.pages.orchestrator_settings")
        st.session_state.update({
            "_defaults_seeded": True,
            "ui_lang": "English",
            "current_page": f"orchestrator_settings:{orchid}",
        })

        def _render():
            try:
                settings_mod.page_orchestrator_settings(orchid)
            except StopRerun:
                pass

        _render()
        assert st.errors == [], "settings render emitted errors: %r" % st.errors

        checkbox_key = f"orch_sysfunc_{orchid}_read_file"
        rendered = {
            kwargs.get("key")
            for _name, _args, kwargs in st.calls
            if kwargs.get("key") == checkbox_key
        }
        assert checkbox_key in rendered, "read_file checkbox missing"

        # Real Streamlit updates the stateful slot before the user action.
        st.session_state[checkbox_key] = False
        st.click(f"orch_save_sysfunc_{orchid}")
        _render()

        assert st.errors == [], "save render emitted errors: %r" % st.errors

    assert get_disabled_tools(orchid) == ["read_file"]

    strong, _weak = build_assistant_dicts(orchid)
    assert "- `read_file`" not in strong["text"]

    agent = UniversalDevAgent()
    agent.attach_orchestrator(orchid)
    result = agent.dispatch("read_file", {"path": "whatever.txt"})
    assert result["ok"] is False
    assert result.get("disabled") is True


# ─── Scenario 4: re-enabling restores the checkbox, prompt and dispatch -----

def test_reenable_restores_checkbox_prompt_and_dispatch(isolated_data_dir, orchid):
    """Given read_file was disabled (previous session),
    when  the user re-enables it via set_disabled_tools and reloads the
          settings page,
    then  the checkbox renders checked, the prompt lists read_file again and
          the dispatcher executes it normally.
    """
    from core.orchestrators import (
        set_disabled_tools, build_assistant_dicts,
    )
    from dev_agent.universal_agent import UniversalDevAgent

    assert set_disabled_tools(orchid, ["read_file"])
    assert set_disabled_tools(orchid, [])  # re-enable everything

    with install_streamlit_mock() as st:
        settings_mod = importlib.import_module("ui.pages.orchestrator_settings")
        st.session_state.update({
            "_defaults_seeded": True,
            "ui_lang": "English",
            "current_page": f"orchestrator_settings:{orchid}",
        })
        try:
            settings_mod.page_orchestrator_settings(orchid)
        except StopRerun:
            pass
        assert st.errors == [], "settings render emitted errors: %r" % st.errors

        checkbox_calls = [
            kwargs
            for _name, _args, kwargs in st.calls
            if kwargs.get("key") == f"orch_sysfunc_{orchid}_read_file"
        ]
        assert checkbox_calls, "read_file checkbox missing"
        assert checkbox_calls[-1]["value"] is True, "checkbox not re-checked"

    strong, _weak = build_assistant_dicts(orchid)
    assert "- `read_file`" in strong["text"]

    agent = UniversalDevAgent()
    agent.attach_orchestrator(orchid)
    result = agent.dispatch("read_file", {"path": "tests/scenarios/test_orchestrator_tool_gating.py"})
    assert result["ok"] is True, result

# ─── Scenario 5: canonical prompts delegate the catalog -----------------------

CANONICAL_PROMPT_FILES = {
    "dev_agent": (
        Path(__file__).resolve().parent.parent.parent
        / "dev_agent" / "system_prompt.md"
    ),
    "ya_agent": (
        Path(__file__).resolve().parent.parent.parent
        / "defaults" / "orchestrators" / "ya_agent" / "system_prompt.md"
    ),
}


def test_canonical_prompts_do_not_duplicate_the_tool_catalog():
    """Given the canonical prompt files shipped with the repository,
    when  they are inspected,
    then  each prompt delegates the full tool list to the auto-added
          '## Available tools' block, keeps its tool-usage rules in-line
          and contains no duplicated tool-catalog table; dev_agent is v3.10
          and ya_agent is v2.7.
    """
    dev = CANONICAL_PROMPT_FILES["dev_agent"].read_text(encoding="utf-8")
    ya = CANONICAL_PROMPT_FILES["ya_agent"].read_text(encoding="utf-8")

    # Version headers carry the bumped versions.
    assert dev.splitlines()[0].endswith("(v3.10)"), dev.splitlines()[0]
    assert ya.splitlines()[0].endswith("(v2.7)"), ya.splitlines()[0]

    # Both prompts delegate the catalog to the auto-added block.
    assert "## Available tools" in dev
    assert "## Available tools" in ya

    # The in-line usage rules survived the table removal.
    assert "Call only the tools listed in that block" in dev
    assert "Вызывайте только перечисленные в нём инструменты" in ya

    # No duplicated tool-catalog table remains in either prompt.
    assert "| Tool | Purpose |" not in dev
    assert "| Инструмент | Назначение |" not in ya
