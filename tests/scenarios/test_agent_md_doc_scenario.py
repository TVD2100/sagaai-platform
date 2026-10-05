# -*- coding: utf-8 -*-
"""tests/scenarios/test_agent_md_doc_scenario.py - user-level scenarios for the
AGENT.md managed document.

Walks the app like a user would, through the public UniversalDevAgent dispatch:

  Scenario 1 - a fresh external project gets AGENT.md scaffolded via
               write_doc('agent'): the file carries the mandatory platform
               conventions and no SPEC.md is created.
  Scenario 2 - the legacy alias 'spec' reads and writes the SAME AGENT.md
               file (transition compatibility).
  Scenario 3 - once PROJECT_MAP.md and AGENT.md exist, assess_workspace
               reports 'software_with_docs'; unknown doc kinds are rejected
               with a hint naming 'agent'.
"""
import importlib
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(os.path.dirname(HERE))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent import config as dev_config  # noqa: E402
from dev_agent import workspace_tools as wt  # noqa: E402
from dev_agent.universal_agent import UniversalDevAgent  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own temporary SQLite database (no real-data writes)."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    import storage.db as db_mod
    db_mod.reset_engine()
    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)
    yield
    db_mod.reset_engine()


@pytest.fixture(autouse=True)
def clean_config_state():
    """Save/restore the process-global config around each scenario."""
    state = dev_config.snapshot_state()
    yield
    dev_config.restore_state(state)


@pytest.fixture
def empty_ws(tmp_path):
    """A fresh empty target folder, selected as the active workspace."""
    folder = tmp_path / "proj"
    res = wt.set_workspace(str(folder))
    assert res["ok"]
    assert res["working_on_install"] is False
    return folder


def test_scenario_write_doc_agent_scaffold(empty_ws):
    """Given an empty project, when write_doc('agent') runs,
    then AGENT.md carries the mandatory conventions and no SPEC.md exists."""
    agent = UniversalDevAgent()
    res = agent.dispatch("write_doc", {"doc": "agent"})
    assert res["ok"], res
    target = empty_ws / "AGENT.md"
    assert target.exists()
    assert not (empty_ws / "SPEC.md").exists()
    text = target.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# Ключевая информация для агентов (AGENT.md)"
    for fragment in ("языковых ключей", "кэш", "сторонних библиотек"):
        assert fragment in text, fragment
    read = agent.dispatch("read_doc", {"doc": "agent"})
    assert read["ok"] and read["exists"]
    assert "Ключевая информация для агентов" in read["content"]


def test_scenario_legacy_spec_alias_targets_agent_md(empty_ws):
    """Given AGENT.md exists, when the legacy 'spec' kind is used,
    then reads and writes hit the SAME AGENT.md file."""
    agent = UniversalDevAgent()
    assert agent.dispatch("write_doc", {"doc": "agent", "content": "# Agent\n"})["ok"]
    res = agent.dispatch("write_doc", {"doc": "spec", "content": "# Legacy write\n"})
    assert res["ok"], res
    assert os.path.basename(res["path"]) == "AGENT.md"
    assert not (empty_ws / "SPEC.md").exists()
    doc = agent.dispatch("read_doc", {"doc": "spec"})
    assert doc["ok"] and doc["exists"]
    assert "# Legacy write" in doc["content"]


def test_scenario_assess_and_unknown_doc_kinds(empty_ws):
    """Given map + AGENT.md exist, when assess runs, then the project counts
    as software_with_docs; unknown kinds are rejected with an 'agent' hint."""
    agent = UniversalDevAgent()
    assert agent.dispatch("write_project_map", {"responsibilities": {}})["ok"]
    assert agent.dispatch("write_doc", {"doc": "agent"})["ok"]
    assert agent.dispatch("assess_workspace", {})["state"] == "software_with_docs"
    bad_write = agent.dispatch("write_doc", {"doc": "unknown"})
    assert bad_write["ok"] is False and "agent" in bad_write["error"]
    bad_read = agent.dispatch("read_doc", {"doc": "unknown"})
    assert bad_read["ok"] is False and "agent" in bad_read["error"]
