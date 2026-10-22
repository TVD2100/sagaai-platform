# -*- coding: utf-8 -*-
"""
Scenario test: MCP connector end-to-end via the demo stdio server (Л-6.2).

Walks the feature through its public entry points the way a user would use it:
  1. Happy path: create an mcp connection (command: python + stdlib server),
     enable it on an orchestrator, list tools, find them in the catalog,
     call a tool by its dynamic name through the dispatcher and get the
     result back.
  2. Server errors: a failing server tool surfaces as a result with
     ``is_error: True`` and the reported message.

Runs fully offline: the MCP server is ``tests/fixtures/mcp_demo_server.py``
started as a real stdio subprocess.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

import core.paths


FIXTURE_SERVER = Path(__file__).parent.parent / "fixtures" / "mcp_demo_server.py"


@pytest.fixture()
def isolated_data_dir():
    """Point DATA_DIR and DB paths at a throwaway temp directory."""
    tmp = tempfile.mkdtemp(prefix="sagaai_mcp_scenario_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    old_env_key = os.environ.get("SAGAAI_ENCRYPTION_KEY")
    os.environ["SAGAAI_DATA_DIR"] = tmp
    from cryptography.fernet import Fernet
    os.environ["SAGAAI_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    old_attrs = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH",
                 "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_attrs[attr] = getattr(core.paths, attr, None)
    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    from storage.db import reset_engine, reset_devagent_engine
    reset_engine()
    reset_devagent_engine()

    import core.mcp_tools as mcp_tools
    mcp_tools.clear_discovery_cache()
    mcp_tools.MCP_DYNAMIC_REGISTRY.clear()

    yield tmp

    from core import mcp_client
    for key in list(mcp_client._PROCESS_REGISTRY):
        mcp_client.drop_client(key)
    reset_engine()
    reset_devagent_engine()

    if old_env:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    if old_env_key:
        os.environ["SAGAAI_ENCRYPTION_KEY"] = old_env_key
    else:
        os.environ.pop("SAGAAI_ENCRYPTION_KEY", None)
    for attr, value in old_attrs.items():
        if value is not None:
            setattr(core.paths, attr, value)
    shutil.rmtree(tmp, ignore_errors=True)


def test_mcp_e2e_demo_scenario(isolated_data_dir):
    """Full user path: connector -> catalog -> dynamic call -> result."""
    from core import connectors
    from core.orchestrators import create_orchestrator, set_enabled_connections

    slug = "mcp_e2e_orch"
    create_orchestrator(slug, "Mcp E2E", "Test")

    # 1. The user creates a local MCP connection (python + stdlib server).
    conn = connectors.create_connection(
        "mcp", "Demo Server",
        config={"command": sys.executable, "args": [str(FIXTURE_SERVER)]},
        secrets=None,
    )
    conn_id = conn["id"]
    assert conn["service"] == "mcp"
    assert connectors.get_connection(conn_id)["service"] == "mcp"

    # 2. Enable the connection on the orchestrator.
    assert set_enabled_connections(slug, [conn_id]) is True

    import core.mcp_tools as mcp_tools
    mcp_tools.clear_discovery_cache()

    # 3. The user lists the server tools.
    listed = mcp_tools.mcp_list_tools(connector_id=conn_id)
    assert listed["ok"] is True
    server_names = {t["name"] for t in listed["result"]["tools"]}
    assert {"echo", "fail"} <= server_names

    # 4. The tools are advertised in the orchestrator catalog with the
    #    dynamic mcp__<conn>__<name> naming.
    from core.orchestrator_tools import build_tool_catalog
    dynamic_echo = f"mcp__{conn_id}__echo"
    catalog_names = [t["name"] for t in build_tool_catalog(slug)]
    assert "mcp_list_tools" in catalog_names
    assert dynamic_echo in catalog_names

    # 5. The dispatching layer resolves the dynamic name and calls the
    #    server tool; the result comes back to the agent loop.
    from dev_agent.universal_agent import UniversalDevAgent
    agent = UniversalDevAgent()
    agent.attach_orchestrator(slug)
    result = agent.dispatch(dynamic_echo, {"text": "hello"})
    assert result["ok"] is True
    assert result["result"]["tool"] == "echo"
    assert result["result"]["content"] == "echo:hello"
    assert result["result"]["is_error"] is False

    # 6. A server-side error surfaces as a clean is_error result.
    failed = agent.dispatch(f"mcp__{conn_id}__fail", {})
    assert failed["ok"] is True
    assert failed["result"]["is_error"] is True
    assert failed["result"]["content"] == "boom"
