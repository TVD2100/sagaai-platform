# -*- coding: utf-8 -*-
"""tests/test_orchestrator_delegation_prompt.py - the delegation block in the prompt.

The model cannot delegate to somebody it cannot name: the system prompt must
list every callable agent and assistant together with the exact argument
(``agent_slug`` / ``assistant_id``) the tool contract expects. These tests
pin the block's content and its gating: an orchestrator without any allowed
assistant must not advertise ``invoke_assistant`` at all.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine  # noqa: E402

SLUG = "deleg_prompt_orch"


@pytest.fixture
def isolated_data_dir():
    """Point core.paths (and the DB engines) at a throwaway data folder."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_deleg_prompt_")
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

    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR"):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def orch(isolated_data_dir):
    """A fresh orchestrator with no delegation rights configured."""
    from core.orchestrators import create_orchestrator
    create_orchestrator(SLUG, "Deleg Prompt Orch", "test orchestrator")
    return SLUG


def _block(orch_slug: str) -> str:
    """Return the delegation part of the prompt for the given orchestrator."""
    from core.orchestrators import _extend_prompt_with_delegation
    return _extend_prompt_with_delegation("BASE", orch_slug)


def test_block_always_lists_self_delegation(orch):
    """Even with no other agent, the orchestrator itself is spawnable."""
    text = _block(orch)

    assert "## Available agents (spawn_agent)" in text
    assert "`%s`" % orch in text
    assert "self-delegation" in text


def test_block_lists_allowed_agents_with_their_slugs(orch):
    """Every allowed agent appears with the exact ``agent_slug`` argument."""
    from core.orchestrators import create_orchestrator, set_allowed_agents
    create_orchestrator("helper_agent", "Helper Agent", "helps")
    assert set_allowed_agents(orch, ["helper_agent"])

    text = _block(orch)

    assert "`helper_agent`" in text
    assert "Helper Agent" in text


def test_block_hides_assistants_without_any_right(orch):
    """No allowed assistant: the block must not advertise invoke_assistant."""
    text = _block(orch)

    assert "## Available assistants" not in text
    assert "invoke_assistant" not in text


def _make_assistant(name: str = "Analyst") -> str:
    """Create a real assistant card and return its id.

    ``set_allowed_assistants`` drops ids that match no live card, so the
    fixture must create a genuine assistant rather than invent an id.
    """
    from core.assistants import create_assistant
    aid = create_assistant(name, "DeepSeek", "deepseek-flash", 0.2, "prompt")
    assert aid
    return aid


def test_block_lists_allowed_assistants_with_their_ids(orch):
    """An allowed assistant appears with the id the tool wants."""
    from core.orchestrators import set_allowed_assistants
    aid = _make_assistant("Analyst")
    assert set_allowed_assistants(orch, [aid])

    text = _block(orch)

    assert "## Available assistants (invoke_assistant)" in text
    assert "`%s`" % aid in text
    assert "Analyst" in text
    assert "context_files" in text


def test_block_keeps_the_original_prompt_as_a_prefix(orch):
    """The block is appended, never substituted for the base prompt."""
    text = _block(orch)

    assert text.startswith("BASE\n\n## Available agents")


def test_block_alone_when_the_prompt_is_empty(orch):
    """An orchestrator without a prompt still gets a clean block."""
    from core.orchestrators import _extend_prompt_with_delegation

    text = _extend_prompt_with_delegation("", orch)

    assert text.startswith("## Available agents")
    assert not text.startswith("\n")


def test_assistant_block_is_separated_from_the_agent_block(orch):
    """Both blocks appear, the assistant one after a blank separator line."""
    from core.orchestrators import set_allowed_assistants
    aid = _make_assistant("Analyst")
    assert set_allowed_assistants(orch, [aid])

    text = _block(orch)

    assert text.index("Available agents") < text.index("Available assistants")
    assert "\n\n## Available assistants" in text


def test_unknown_assistant_name_does_not_break_the_block(orch):
    """A stale id stays listed even when the assistant is gone from the DB.

    ``set_allowed_assistants`` refuses unknown ids, so the stale entry is
    written straight into the config - exactly the state a deleted assistant
    leaves behind in an existing installation.
    """
    from core.orchestrators import get_orchestrator
    from storage.repository import repo_update_orchestrator

    full = get_orchestrator(orch)
    cfg = dict(full.get("config") or {})
    cfg["allowed_assistants"] = ["gone_id"]
    assert repo_update_orchestrator(full["id"], config=cfg)

    text = _block(orch)

    assert "`gone_id`" in text
    assert "name unknown" in text


def test_build_assistant_dicts_includes_the_block(orch):
    """The real prompt builder carries the block into the assistant dict."""
    from core.orchestrators import build_assistant_dicts

    strong, weak = build_assistant_dicts(orch)

    assert "## Available agents (spawn_agent)" in strong["text"]
    assert weak["text"] == strong["text"]
