# -*- coding: utf-8 -*-
"""Tests for UniversalDevAgent 'disabled_tools' gating in dispatch.

When an orchestrator config lists a tool in ``disabled_tools``, calling that
tool (including custom functions and connection tools) must return a
structured ``disabled: true`` error instead of executing.
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    """Isolate the platform data dir so orchestrator rows/folders are fresh."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_gating_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

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
def orch_slug(isolated_data_dir):
    from core.orchestrators import create_orchestrator
    slug = "gating_orch"
    create_orchestrator(slug, "Gating Orch", "Test")
    return slug


CUSTOM_FN_CODE = "def invoke(**kwargs):\n    return {'ok': True, 'source': 'custom'}\n"


def _make_agent(orch_slug):
    from dev_agent.universal_agent import UniversalDevAgent
    agent = UniversalDevAgent()
    agent.attach_orchestrator(orch_slug)
    return agent


class TestDispatchToolGating:

    def test_disabled_core_tool_is_blocked(self, orch_slug):
        from core.orchestrators import set_disabled_tools
        set_disabled_tools(orch_slug, ["read_file"])
        agent = _make_agent(orch_slug)
        result = agent.dispatch("read_file", {"path": "whatever.txt"})
        assert result.get("ok") is False
        assert result.get("disabled") is True
        assert "disabled" in result.get("error", "").lower()

    def test_enabled_core_tool_still_works(self, orch_slug):
        from core.orchestrators import set_disabled_tools
        set_disabled_tools(orch_slug, ["read_file"])
        agent = _make_agent(orch_slug)
        result = agent.dispatch("scan_folder", {})
        assert result.get("ok") is True

    def test_gate_blocks_legacy_alias_both_ways(self, orch_slug):
        from core.orchestrators import set_disabled_tools
        # list_skills is the legacy alias of list_assistants: disabling one
        # must block both spellings.
        set_disabled_tools(orch_slug, ["list_skills"])
        agent = _make_agent(orch_slug)
        result_alias = agent.dispatch("list_skills", {})
        result_canon = agent.dispatch("list_assistants", {})
        assert result_alias.get("disabled") is True
        assert result_canon.get("disabled") is True

    def test_disabled_custom_function_is_blocked(self, orch_slug):
        from core.orchestrator_folders import save_orchestrator_function
        from core.orchestrators import set_disabled_tools
        assert save_orchestrator_function(orch_slug, "my_metric", CUSTOM_FN_CODE)
        set_disabled_tools(orch_slug, ["my_metric"])
        agent = _make_agent(orch_slug)
        assert "my_metric" in agent._extra
        result = agent.dispatch("my_metric", {})
        assert result.get("ok") is False
        assert result.get("disabled") is True

    def test_disabled_connection_tool_is_blocked(self, orch_slug):
        from core.connectors import create_connection
        from core.orchestrators import set_enabled_connections, set_disabled_tools
        conn = create_connection("github_rest", "Rest Conn", "tok")
        set_enabled_connections(orch_slug, [conn["id"]])
        set_disabled_tools(orch_slug, ["ghr_list_repos"])
        agent = _make_agent(orch_slug)
        assert "ghr_list_repos" in agent._extra
        result = agent.dispatch("ghr_list_repos", {"connector_id": conn["id"]})
        assert result.get("ok") is False
        assert result.get("disabled") is True

    def test_default_devagent_has_nothing_disabled(self, isolated_data_dir):
        """Without attach_orchestrator the fallback slug is dev_agent, and by
        default its disabled_tools list is empty: tools execute normally."""
        from core.orchestrators import ensure_builtin_orchestrators
        from dev_agent.universal_agent import UniversalDevAgent
        ensure_builtin_orchestrators()
        agent = UniversalDevAgent()
        result = agent.dispatch("scan_folder", {})
        assert result.get("ok") is True
