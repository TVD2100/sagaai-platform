# -*- coding: utf-8 -*-
"""tests.test_mcp_catalog - catalog/security tests for the mcp connector.

Covers the integration points of the MCP tool layer (Л-5.5):
  - the mcp branch of ``_connection_catalog`` emits the static tools plus
    the dynamic ``mcp__<conn_id>__<tool>`` tools when an mcp connection is
    enabled and the demo server responds;
  - disabled tools (both static and dynamic names) vanish from the catalog
    and from the rendered prompt block;
  - ``env_json`` secrets never appear in catalog descriptions or in the
    discovery/call payloads (plaintext is decrypted only to build the
    server environment).

The demo server from ``tests/fixtures/mcp_demo_server.py`` runs as a real
stdio subprocess.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


@pytest.fixture
def isolated_data_dir():
    """Isolate platform data dirs and mcp module state for one test."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_mcp_catalog_")
    old_env_data = os.environ.get("SAGAAI_DATA_DIR")
    old_env_key = os.environ.get("SAGAAI_ENCRYPTION_KEY")
    from cryptography.fernet import Fernet
    os.environ["SAGAAI_DATA_DIR"] = tmp
    os.environ["SAGAAI_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    import core.paths
    old_paths = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH",
                 "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_paths[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    import core.mcp_tools as mcp_tools
    mcp_tools.clear_discovery_cache()
    mcp_tools.MCP_DYNAMIC_REGISTRY.clear()

    reset_engine()
    reset_devagent_engine()

    yield tmp

    from core import mcp_client
    for key in list(mcp_client._PROCESS_REGISTRY):
        mcp_client.drop_client(key)
    reset_engine()
    reset_devagent_engine()

    if old_env_data:
        os.environ["SAGAAI_DATA_DIR"] = old_env_data
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    if old_env_key:
        os.environ["SAGAAI_ENCRYPTION_KEY"] = old_env_key
    else:
        os.environ.pop("SAGAAI_ENCRYPTION_KEY", None)

    for attr, value in old_paths.items():
        if value is not None:
            setattr(core.paths, attr, value)

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def orch_slug(isolated_data_dir):
    from core.orchestrators import create_orchestrator
    slug = "mcp_cat"
    create_orchestrator(slug, "Mcp Cat", "Test")
    return slug


def _make_connection(orch_slug: str, env_secret: str | None = None):
    """Create an mcp connection against the demo server and enable it.

    Returns the public connection manifest.
    """
    from core.connectors import create_connection
    from core.orchestrators import set_enabled_connections
    secrets = None
    if env_secret is not None:
        secrets = {"env_json": env_secret}
    conn = create_connection(
        "mcp", "Demo",
        config={"command": sys.executable, "args": [str(FIXTURE_SERVER)]},
        secrets=secrets,
    )
    set_enabled_connections(orch_slug, [conn["id"]])
    import core.mcp_tools as mcp_tools
    mcp_tools.clear_discovery_cache()
    return conn


# ─── catalog emission ───────────────────────────────────────────────────────


def test_catalog_contains_static_mcp_tools_without_connections(orch_slug):
    from core.orchestrator_tools import build_tool_catalog
    names = [t["name"] for t in build_tool_catalog(orch_slug)]
    assert "mcp_list_tools" not in names
    assert "mcp_call_tool" not in names


def test_catalog_contains_static_and_dynamic_mcp_tools(orch_slug):
    conn = _make_connection(orch_slug)
    from core.orchestrator_tools import build_tool_catalog
    catalog = build_tool_catalog(orch_slug)
    names = [t["name"] for t in catalog]
    assert "mcp_list_tools" in names
    assert "mcp_call_tool" in names
    expected = {"echo", "fail", "image", "slow"}
    dynamic = {
        n.split("__", 2)[2]
        for n in names if n.startswith(f"mcp__{conn['id']}__")
    }
    assert expected == dynamic, (expected, dynamic)


def test_catalog_description_does_not_include_env_suffix(orch_slug):
    _make_connection(orch_slug)
    from core.orchestrator_tools import build_tool_catalog
    catalog = build_tool_catalog(orch_slug)
    for t in catalog:
        if t["name"].startswith("mcp__"):
            assert t["desc"]


def test_connection_catalog_returns_empty_without_enabled_mcp(orch_slug):
    import core.mcp_tools as mcp_tools
    from core.orchestrator_tools import _connection_catalog
    _make_connection(orch_slug)
    from core.orchestrators import set_enabled_connections
    set_enabled_connections(orch_slug, [])
    mcp_tools.clear_discovery_cache()
    names = [t["name"] for t in _connection_catalog(orch_slug)]
    assert not any(n.startswith("mcp") for n in names)


# ─── disabled tools ─────────────────────────────────────────────────────────


def test_disabled_static_tools_vanish_from_catalog_and_block(orch_slug):
    _make_connection(orch_slug)
    from core.orchestrator_tools import build_tool_catalog, render_available_tools_block
    disabled = {"mcp_list_tools"}
    names = [t["name"] for t in build_tool_catalog(orch_slug, disabled=disabled)]
    assert "mcp_list_tools" not in names
    assert "mcp_call_tool" in names
    block = render_available_tools_block(orch_slug, disabled=disabled)
    assert "mcp_list_tools" not in block


def test_disabled_dynamic_tool_vanish_from_catalog_and_block(orch_slug):
    conn = _make_connection(orch_slug)
    from core.orchestrator_tools import build_tool_catalog, render_available_tools_block
    dynamic_echo = f"mcp__{conn['id']}__echo"
    disabled = {dynamic_echo}
    names = [t["name"] for t in build_tool_catalog(orch_slug, disabled=disabled)]
    assert dynamic_echo not in names
    assert "mcp_call_tool" in names
    block = render_available_tools_block(orch_slug, disabled=disabled)
    assert dynamic_echo not in block


# ─── secret hygiene ─────────────────────────────────────────────────────────


def test_env_json_secret_never_appears_in_catalog_or_payload(orch_slug):
    """The plaintext env_json secret never surfaces in public results."""
    import core.mcp_tools as mcp_tools
    conn = _make_connection(orch_slug, env_secret=json.dumps({"MY_TOKEN": "top-secret-value"}))
    from core.orchestrator_tools import build_tool_catalog
    import core.connectors as connectors

    secret_plain = "top-secret-value"
    catalog = build_tool_catalog(orch_slug)
    names = [t["name"] for t in catalog]
    assert "mcp_list_tools" in names
    assert all(secret_plain not in t["desc"] for t in catalog)

    tools_result = mcp_tools.mcp_list_tools(connector_id=conn["id"])
    assert tools_result.get("ok") is True
    assert secret_plain not in json.dumps(tools_result)

    call_result = mcp_tools.mcp_call_tool(
        connector_id=conn["id"], tool_name="echo", arguments={"text": "x"})
    assert call_result.get("ok") is True
    assert secret_plain not in json.dumps(call_result)

    public = connectors.get_connection(conn["id"])
    masked = public.get("secrets_masked") or {}
    assert secret_plain not in str(masked)
