# -*- coding: utf-8 -*-
"""Tests for core.orchestrator_tools (tool catalog + Available tools block)."""
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
    tmp = tempfile.mkdtemp(prefix="sagaai_test_orch_tools_")
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
    slug = "tools_orch"
    create_orchestrator(slug, "Tools Orch", "Test")
    return slug


CUSTOM_FN_CODE = "def invoke(**kwargs):\n    return {'ok': True}\n"


# ─── Catalog assembly ───────────────────────────────────────────────────────


def test_build_tool_catalog_contains_core_and_workspace(orch_slug):
    from core.orchestrator_tools import build_tool_catalog
    names = [t["name"] for t in build_tool_catalog(orch_slug, include_connections=False)]
    assert "read_file" in names
    assert "set_workspace" in names


def test_build_tool_catalog_excludes_disabled(orch_slug):
    from core.orchestrator_tools import build_tool_catalog
    catalog = build_tool_catalog(orch_slug, include_connections=False,
                                 disabled={"read_file"})
    names = [t["name"] for t in catalog]
    assert "read_file" not in names
    assert "list_files" in names


def test_build_tool_catalog_lists_custom_function(orch_slug):
    from core.orchestrator_folders import save_orchestrator_function
    assert save_orchestrator_function(orch_slug, "my_metric", CUSTOM_FN_CODE)
    from core.orchestrator_tools import build_tool_catalog
    names = [t["name"] for t in build_tool_catalog(orch_slug, include_connections=False)]
    assert "my_metric" in names


# ─── Prompt block rendering ─────────────────────────────────────────────────


def test_render_available_tools_block_lists_enabled_tools(orch_slug):
    from core.orchestrator_tools import render_available_tools_block
    block = render_available_tools_block(orch_slug)
    assert "## Available tools" in block
    assert "`read_file`" in block
    assert "fenced JSON" in block


def test_render_block_excludes_disabled_tools(orch_slug):
    from core.orchestrator_tools import render_available_tools_block
    block = render_available_tools_block(orch_slug, disabled={"read_file", "list_skills"})
    assert "`read_file`" not in block
    # list_skills canonicalizes to list_assistants: the canonical entry is
    # dropped from the list as well.
    assert "`list_assistants`" not in block


def test_render_block_marks_custom_function(orch_slug):
    from core.orchestrator_folders import save_orchestrator_function
    assert save_orchestrator_function(orch_slug, "my_metric", CUSTOM_FN_CODE)
    from core.orchestrator_tools import render_available_tools_block
    block = render_available_tools_block(orch_slug)
    assert "`my_metric` (custom function)" in block


# ─── Name resolution helpers ────────────────────────────────────────────────


def test_resolve_tool_name_legacy_alias():
    from core.orchestrator_tools import resolve_tool_name
    assert resolve_tool_name("list_skills") == "list_assistants"
    assert resolve_tool_name("read_file") == "read_file"


def test_is_tool_disabled_alias_both_directions():
    from core.orchestrator_tools import is_tool_disabled
    assert is_tool_disabled("list_assistants", {"list_skills"})
    assert is_tool_disabled("list_skills", {"list_assistants"})
    assert not is_tool_disabled("read_file", {"list_skills"})


def test_normalize_disabled_dedup_alias_and_garbage():
    from core.orchestrator_tools import normalize_disabled
    assert normalize_disabled(["list_skills", " list_skills ", "", "read_file", 3]) == \
        ["list_assistants", "read_file"]


# ─── System tools for the settings UI ───────────────────────────────────────


def test_list_system_tools_excludes_custom_functions(orch_slug):
    from core.orchestrator_folders import save_orchestrator_function
    assert save_orchestrator_function(orch_slug, "my_metric", CUSTOM_FN_CODE)
    from core.orchestrator_tools import list_system_tools
    names = [t["name"] for t in list_system_tools(orch_slug)]
    assert "my_metric" not in names
    assert "read_file" in names
# ─── Step 3: core.orchestrators disabled_tools API ─────────────────────────


def test_get_disabled_tools_default_empty(orch_slug):
    from core.orchestrators import get_disabled_tools
    assert get_disabled_tools(orch_slug) == []


def test_set_get_disabled_tools_normalizes_aliases(orch_slug):
    from core.orchestrators import get_disabled_tools, set_disabled_tools
    assert set_disabled_tools(orch_slug, ["list_skills", " read_file ", "read_file", ""])
    assert get_disabled_tools(orch_slug) == ["list_assistants", "read_file"]


def test_set_disabled_tools_missing_orchestrator(isolated_data_dir):
    from core.orchestrators import set_disabled_tools
    assert set_disabled_tools("no_such_orch", ["read_file"]) is False


def test_extend_prompt_with_tools_appends_block(orch_slug):
    from core.orchestrators import _extend_prompt_with_tools
    prompt = _extend_prompt_with_tools("Base prompt", orch_slug)
    assert "## Available tools" in prompt
    assert "`read_file`" in prompt


def test_build_assistant_dicts_includes_tools_block(orch_slug):
    from core.orchestrators import build_assistant_dicts
    strong, _weak = build_assistant_dicts(orch_slug)
    text = strong.get("text", "")
    assert "## Available tools" in text
    assert "`read_file`" in text


def test_build_assistant_dicts_excludes_disabled_from_block(orch_slug):
    from core.orchestrators import build_assistant_dicts, set_disabled_tools
    assert set_disabled_tools(orch_slug, ["read_file"])
    strong, _weak = build_assistant_dicts(orch_slug)
    text = strong.get("text", "")
    assert "## Available tools" in text
    assert "`read_file`" not in text
    assert "`list_files`" in text


def test_devagent_default_config_has_disabled_tools_key():
    import core.orchestrators as orch_mod
    assert "disabled_tools" in orch_mod._DEVAGENT_DEFAULT_CONFIG
    assert orch_mod._devagent_default_config().get("disabled_tools") == []


def test_ensure_builtin_backfills_disabled_tools(isolated_data_dir):
    """ensure_builtin_orchestrators backfills 'disabled_tools' for old configs."""
    import uuid
    from storage.repository import repo_create_orchestrator
    from core.orchestrators import (
        DEVAGENT_SLUG, ensure_builtin_orchestrators, get_orchestrator,
    )
    repo_create_orchestrator(
        orchestrator_id=uuid.uuid4().hex[:8],
        slug=DEVAGENT_SLUG,
        name="DevAgent",
        description="legacy",
        prompt_text="legacy prompt",
        config={"strong_service": "DeepSeek"},
        is_builtin=True,
    )
    result = ensure_builtin_orchestrators()
    assert result.get(DEVAGENT_SLUG) == "updated", result
    orch = get_orchestrator(DEVAGENT_SLUG)
    assert orch is not None
    assert orch["config"].get("disabled_tools") == []