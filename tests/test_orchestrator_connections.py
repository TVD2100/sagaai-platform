# -*- coding: utf-8 -*-
"""Tests for orchestrator connection integration (enabled_connections)."""
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest import mock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    tmp = tempfile.mkdtemp(prefix="sagaai_test_conn_")
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
    slug = "conn_orch"
    create_orchestrator(slug, "Conn Orch", "Test")
    return slug


FAKE_CONN = {"service": "github", "name": "My GitHub", "account": "alice"}
FAKE_TOOLS = [{"name": "github_list_repos", "desc": "List repositories."}]


def test_default_enabled_connections_empty(orch_slug):
    from core.orchestrators import get_enabled_connections
    assert get_enabled_connections(orch_slug) == []


def test_set_get_enabled_connections(orch_slug):
    from core.orchestrators import get_enabled_connections, set_enabled_connections
    assert set_enabled_connections(orch_slug, ["conn1", "conn2", "conn1", "  "])
    assert get_enabled_connections(orch_slug) == ["conn1", "conn2"]


def test_set_enabled_connections_missing_orchestrator(isolated_data_dir):
    from core.orchestrators import set_enabled_connections
    assert set_enabled_connections("no_such_orch", ["c1"]) is False


def test_prompt_extended_with_connections(orch_slug):
    from core.orchestrators import _extend_prompt_with_connections, set_enabled_connections
    set_enabled_connections(orch_slug, ["conn_github"])
    with mock.patch("core.connectors.get_connection", return_value=FAKE_CONN), mock.patch("core.github_tools.get_tools", return_value=FAKE_TOOLS):
        prompt = _extend_prompt_with_connections("Base prompt", orch_slug)

    assert "## Available service connections" in prompt
    assert "conn_github" in prompt
    assert "alice" in prompt
    assert "github_list_repos" in prompt
    assert "Quick usage notes" in prompt
    assert "Always pass `connector_id`" in prompt
    assert "`github_upload_file` creates a NEW file" in prompt
    assert "`github_read_file` first" in prompt


def test_prompt_unchanged_when_no_connections(orch_slug):
    from core.orchestrators import _extend_prompt_with_connections
    assert _extend_prompt_with_connections("Base prompt", orch_slug) == "Base prompt"


def test_build_assistant_dicts_includes_connections_block(orch_slug):
    from core.orchestrators import build_assistant_dicts, set_enabled_connections
    set_enabled_connections(orch_slug, ["conn_github"])
    with mock.patch("core.connectors.get_connection", return_value=FAKE_CONN), mock.patch("core.github_tools.get_tools", return_value=FAKE_TOOLS):
        strong, weak = build_assistant_dicts(orch_slug)

    assert "## Available service connections" in strong.get("text", "")
    assert "conn_github" in strong.get("text", "")


def test_devagent_default_config_has_key():
    import core.orchestrators as orch_mod
    assert "enabled_connections" in orch_mod._DEVAGENT_DEFAULT_CONFIG
