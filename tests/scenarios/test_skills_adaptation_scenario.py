# -*- coding: utf-8 -*-
"""tests/scenarios/test_skills_adaptation_scenario.py - user-level scenarios
for the skills-library adaptation flow (SPEC FR12).

Walks the app like a user would, through the public entry points:

  Scenario 1 - importing an external skill registers it as "not adapted" and
               it is hidden from the orchestrator system-prompt block.
  Scenario 2 - the skills-library "Adapt" button hands the task over to the
               DevAgent orchestrator dialog.
  Scenario 3 - completing the adaptation via the DevAgent tool
               ``mark_skill_adapted`` reveals the skill in the system prompt.
"""
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
def isolated_data_dir(monkeypatch):
    """Temporary DATA_DIR isolating skills/ and the DB from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_slib_scn_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    monkeypatch.setenv("SAGAAI_DATA_DIR", tmp)

    import core.paths as paths_mod
    importlib.reload(paths_mod)

    import storage.db as db_mod
    importlib.reload(db_mod)
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()

    yield tmp

    db_mod.reset_engine()
    db_mod.reset_devagent_engine()
    if old_env:
        monkeypatch.setenv("SAGAAI_DATA_DIR", old_env)
    else:
        monkeypatch.delenv("SAGAAI_DATA_DIR", raising=False)
    importlib.reload(paths_mod)
    shutil.rmtree(tmp, ignore_errors=True)


def _import_external_skill(isolated_data_dir, name="External Skill"):
    """Install a local folder as an external (non-adapted) skill."""
    from core.skills_library import import_skill_from_folder
    src = os.path.join(isolated_data_dir, "external_source")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("# External skill")
    result = import_skill_from_folder(src, name=name, description="Third-party skill")
    assert result.get("ok")
    return result["skill"]


def _make_devagent_dispatcher(monkeypatch, tmp_path):
    """Create a real UniversalDevAgent dispatcher on an isolated sandbox."""
    from dev_agent import config as dconfig
    root = tmp_path / "sandbox"
    (root / "backups").mkdir(parents=True, exist_ok=True)
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dconfig, "PROJECT_ROOT", root)
    monkeypatch.setattr(dconfig, "BACKUPS_DIR", root / "backups")
    monkeypatch.setattr(dconfig, "WORKSPACE_DIR", root / "workspace")
    monkeypatch.setattr(dconfig, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(dconfig, "PROTECTED_FILES", ())
    from dev_agent.universal_agent import UniversalDevAgent
    return UniversalDevAgent(workspace=str(root))


# ─── Scenario 1: external skill is hidden until adapted ─────────────────────

def test_external_skill_hidden_from_prompt_until_adapted(isolated_data_dir, monkeypatch, tmp_path):
    """
    Given a user imports a third-party skill into the library,
    when  the DevAgent orchestrator enables that skill,
    then  the skill is registered as not adapted and does NOT appear in
          the "Available skills" block of the system prompt.
    """
    skill = _import_external_skill(isolated_data_dir)
    from core.skills_library import get_skill, get_enabled_skills_metadata
    rec = get_skill(skill["id"])
    assert rec["adapted"] is False
    assert rec["developer"] == "unknown"
    assert get_enabled_skills_metadata([skill["id"]]) == []

    from core.orchestrators import (
        create_orchestrator, set_enabled_skills, build_skill_dicts, delete_orchestrator,
    )
    slug = "scn_adapt_orch"
    assert create_orchestrator(slug, "Scenario Orch", prompt_text="Base prompt")
    assert set_enabled_skills(slug, [skill["id"]])
    try:
        strong, weak = build_skill_dicts(slug)
        assert "Base prompt" in strong["text"]
        assert "External Skill" not in strong["text"]
        assert "Available skills" not in strong["text"]
    finally:
        delete_orchestrator(slug)


# ─── Scenario 2: Adapt button hands the task to DevAgent ────────────────────

def test_adapt_button_hands_off_to_devagent(monkeypatch):
    """
    Given the skills library shows a non-adapted skill with an Adapt button,
    when  the user clicks that button,
    then  the DevAgent orchestrator is switched to and receives a pending
          adaptation task for that skill.
    """
    external = {
        "id": "abcd1234", "name": "External Skill", "description": "ext desc",
        "folder": "External_Skill", "developer": "unknown", "adapted": False,
        "source": "",
    }
    adapted = {
        "id": "bbcd1234", "name": "Platform Skill", "description": "",
        "folder": "Platform_Skill", "developer": "SagaAI", "adapted": True,
        "source": "defaults/Platform_Skill",
    }

    import core.skills_library as slib_mod
    monkeypatch.setattr(slib_mod, "list_skills", lambda: [adapted, external])
    monkeypatch.setattr(slib_mod, "get_skills_root", lambda: "/tmp/skills")

    import ui.pages.orchestrator as orch_page
    monkeypatch.setattr(orch_page, "get_orchestrator", lambda s: None)

    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)

    with install_streamlit_mock() as st:
        from ui.pages.skills_library import page_skills_library
        st.session_state.update({"ui_lang": "English"})
        try:
            page_skills_library()
        except StopRerun:
            pass

        keys = [c[2].get("key") for c in st.calls if c[0] == "button"]
        assert "slib_adapt_abcd1234" in keys
        assert "slib_adapt_bbcd1234" not in keys

        st.click("slib_adapt_abcd1234")
        try:
            page_skills_library()
        except StopRerun:
            pass

        task = st.session_state.get("orch_dev_agent_pending_task")
        assert task and "abcd1234" in task
        assert st.session_state.get("current_page") == "orchestrator:dev_agent"
        assert st.session_state.get("last_active_entity_id") == "dev_agent"


# ─── Scenario 3: adaptation completion reveals the skill ────────────────────

def test_mark_adapted_reveals_skill_in_prompt(isolated_data_dir, monkeypatch, tmp_path):
    """
    Given the DevAgent finished adapting an external skill,
    when  it calls the mark_skill_adapted tool for that skill,
    then  the record is updated and the skill now appears in the
          "Available skills" block of the system prompt.
    """
    skill = _import_external_skill(isolated_data_dir)
    from core.skills_library import get_skill
    assert get_skill(skill["id"])["adapted"] is False

    dispatcher = _make_devagent_dispatcher(monkeypatch, tmp_path)
    out = dispatcher.dispatch("mark_skill_adapted", {"skill_id": skill["id"]})
    assert out.get("ok") is True
    assert out["skill"]["adapted"] is True
    assert get_skill(skill["id"])["adapted"] is True

    from core.skills_library import get_enabled_skills_metadata
    metas = get_enabled_skills_metadata([skill["id"]])
    assert [m["id"] for m in metas] == [skill["id"]]

    from core.orchestrators import (
        create_orchestrator, set_enabled_skills, build_skill_dicts, delete_orchestrator,
    )
    slug = "scn_adapt_orch_2"
    assert create_orchestrator(slug, "Scenario Orch 2", prompt_text="Base prompt")
    assert set_enabled_skills(slug, [skill["id"]])
    try:
        strong, _ = build_skill_dicts(slug)
        assert "Available skills" in strong["text"]
        assert "External Skill" in strong["text"]
        assert "Third-party skill" in strong["text"]
    finally:
        delete_orchestrator(slug)
