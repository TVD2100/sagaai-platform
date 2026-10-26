# -*- coding: utf-8 -*-
"""
Tests for the ``memory_dir`` argument of the orchestrator dispatcher tools.

The working-data folder is stored inside ``config`` (the key the prompt
assembly reads), so the dispatcher must fold a top-level ``memory_dir`` into
the config without dropping the other keys, and an empty value must remove
the setting instead of storing an empty string.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture()
def isolated_data_dir():
    """Temporary DATA_DIR so the tests never touch the real database."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_memdir_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR"):
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

    for attr, value in old_values.items():
        if value is not None:
            setattr(core.paths, attr, value)

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def agent(isolated_data_dir):
    from dev_agent.universal_agent import UniversalDevAgent
    return UniversalDevAgent()


def _config(slug):
    from core.orchestrators import get_orchestrator
    return get_orchestrator(slug)["config"] or {}


# ─── create ─────────────────────────────────────────────────────────────────


def test_create_stores_memory_dir_in_config(agent):
    result = agent.dispatch("create_orchestrator", {
        "slug": "mem_create",
        "name": "Memory Create",
        "memory_dir": "bases/mem_create",
    })
    assert result["ok"] is True, result
    assert _config("mem_create")["memory_dir"] == "bases/mem_create"


def test_create_without_memory_dir_keeps_config_clean(agent):
    agent.dispatch("create_orchestrator", {"slug": "mem_plain", "name": "Plain"})
    assert "memory_dir" not in _config("mem_plain")


def test_create_keeps_explicit_config_keys(agent):
    result = agent.dispatch("create_orchestrator", {
        "slug": "mem_cfg",
        "name": "Memory Cfg",
        "config": {"model": "gpt-5", "ui_lang": "ru"},
        "memory_dir": "mem_cfg_data",
    })
    assert result["ok"] is True, result
    cfg = _config("mem_cfg")
    assert cfg["memory_dir"] == "mem_cfg_data"
    assert cfg["model"] == "gpt-5"
    assert cfg["ui_lang"] == "ru"


def test_create_memory_dir_is_trimmed(agent):
    agent.dispatch("create_orchestrator", {
        "slug": "mem_trim",
        "name": "Trim",
        "memory_dir": "   mem_trim_data   ",
    })
    assert _config("mem_trim")["memory_dir"] == "mem_trim_data"


# ─── update ─────────────────────────────────────────────────────────────────


def test_update_sets_memory_dir(agent):
    agent.dispatch("create_orchestrator", {"slug": "mem_upd", "name": "Upd"})
    result = agent.dispatch("update_orchestrator", {
        "slug": "mem_upd",
        "memory_dir": "data/upd",
    })
    assert result["ok"] is True, result
    assert _config("mem_upd")["memory_dir"] == "data/upd"


def test_update_empty_value_drops_the_key(agent):
    agent.dispatch("create_orchestrator", {
        "slug": "mem_drop",
        "name": "Drop",
        "memory_dir": "data/drop",
    })
    assert _config("mem_drop")["memory_dir"] == "data/drop"
    agent.dispatch("update_orchestrator", {"slug": "mem_drop", "memory_dir": ""})
    assert "memory_dir" not in _config("mem_drop")


def test_update_preserves_other_config_keys(agent):
    agent.dispatch("create_orchestrator", {
        "slug": "mem_keep",
        "name": "Keep",
        "config": {"model": "gpt-5", "temperature": 0.2},
    })
    agent.dispatch("update_orchestrator", {"slug": "mem_keep", "memory_dir": "data/keep"})
    cfg = _config("mem_keep")
    assert cfg["model"] == "gpt-5"
    assert cfg["temperature"] == 0.2
    assert cfg["memory_dir"] == "data/keep"


# ─── argument gating ────────────────────────────────────────────────────────


def test_memory_dir_is_an_accepted_argument():
    from dev_agent.universal_agent import WORKSPACE_TOOL_ARGS
    for tool in ("create_orchestrator", "update_orchestrator"):
        assert "memory_dir" in WORKSPACE_TOOL_ARGS[tool]["optional"], tool


def test_unknown_argument_is_still_rejected(agent):
    result = agent.dispatch("create_orchestrator", {
        "slug": "mem_bogus",
        "name": "Bogus",
        "memory_directory": "wrong_name",
    })
    assert result["ok"] is False
    assert "memory_directory" in str(result)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
