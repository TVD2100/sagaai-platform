# -*- coding: utf-8 -*-
"""tests/test_skills_adaptation.py - targeted regression tests for the skills
adaptation feature.

Covers the developer/adapted ownership model introduced in SPEC FR12:

  Core registry:
    - external skills default to unknown / not adapted,
    - explicit platform imports are SagaAI / adapted,
    - legacy registry records are backfilled sensibly,
    - set_skill_adapted() marks a skill as adapted,
    - list_skills(adapted_only=True) and get_enabled_skills_metadata()
      hide non-adapted skills from orchestrator prompts.

  DevAgent tool:
    - mark_skill_adapted(skill_id) flips the record through the public
      registry API and rejects unknown ids.

  UI:
    - the skills-library page renders the "Adapt" button only for
      non-adapted skills and hands the task over to the DevAgent
      orchestrator page.
"""
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR isolating skills/ and the DB from the real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_slib_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

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
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    importlib.reload(paths_mod)
    shutil.rmtree(tmp, ignore_errors=True)


class TestAdaptationRegistry:
    def _import_ext(self, isolated_data_dir, name="ExtSkill"):
        from core.skills_library import import_skill_from_folder
        src = os.path.join(isolated_data_dir, "ext_" + name.replace(" ", "_"))
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "f.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        result = import_skill_from_folder(src, name=name, description="Ext desc")
        assert result.get("ok")
        return result["skill"]["id"]

    def test_external_import_defaults_to_not_adapted(self, isolated_data_dir):
        from core.skills_library import (
            get_skill, get_enabled_skills_metadata, UNKNOWN_DEVELOPER,
        )
        sid = self._import_ext(isolated_data_dir)
        rec = get_skill(sid)
        assert rec["developer"] == UNKNOWN_DEVELOPER
        assert rec["adapted"] is False
        assert get_enabled_skills_metadata([sid]) == []

    def test_explicit_platform_import_is_adapted(self, isolated_data_dir):
        from core.skills_library import (
            import_skill_from_folder, get_skill, PLATFORM_DEVELOPER,
        )
        src = os.path.join(isolated_data_dir, "plat_src")
        os.makedirs(src)
        with open(os.path.join(src, "p.txt"), "w", encoding="utf-8") as f:
            f.write("p")
        result = import_skill_from_folder(
            src, name="Platform", developer=PLATFORM_DEVELOPER, adapted=True,
        )
        rec = get_skill(result["skill"]["id"])
        assert rec["developer"] == PLATFORM_DEVELOPER
        assert rec["adapted"] is True

    def test_set_skill_adapted_marks_it(self, isolated_data_dir):
        from core.skills_library import (
            set_skill_adapted, get_skill, get_enabled_skills_metadata,
        )
        sid = self._import_ext(isolated_data_dir)
        assert set_skill_adapted(sid) is True
        assert get_skill(sid)["adapted"] is True
        metas = get_enabled_skills_metadata([sid])
        assert [m["id"] for m in metas] == [sid]

    def test_set_skill_adapted_rejects_unknown_and_invalid(self, isolated_data_dir):
        from core.skills_library import set_skill_adapted
        assert set_skill_adapted("deadbeef") is False
        assert set_skill_adapted("not-hex-") is False
        assert set_skill_adapted("") is False

    def test_list_skills_adapted_only(self, isolated_data_dir):
        from core.skills_library import import_skill_from_folder, list_skills
        src = os.path.join(isolated_data_dir, "plat_only")
        os.makedirs(src)
        with open(os.path.join(src, "p.txt"), "w", encoding="utf-8") as f:
            f.write("p")
        res = import_skill_from_folder(src, name="PlatformOnly", adapted=True)
        plat_id = res["skill"]["id"]
        ext_id = self._import_ext(isolated_data_dir, name="ExternalOnly")

        all_ids = {s["id"] for s in list_skills()}
        assert {plat_id, ext_id} <= all_ids

        adapted_ids = {s["id"] for s in list_skills(adapted_only=True)}
        assert plat_id in adapted_ids
        assert ext_id not in adapted_ids

    def test_legacy_records_are_backfilled(self, isolated_data_dir):
        from core.skills_library import (
            _load_registry, _save_registry, get_skill,
            get_enabled_skills_metadata, PLATFORM_DEVELOPER, UNKNOWN_DEVELOPER,
        )
        registry = _load_registry()
        registry["abcd1234"] = {
            "name": "Legacy Default", "description": "", "folder": "Legacy_Default",
            "source": "defaults/Legacy_Default",
        }
        registry["bbcd1234"] = {
            "name": "Legacy External", "description": "", "folder": "Legacy_External",
            "source": "",
        }
        assert _save_registry(registry)

        rec_default = get_skill("abcd1234")
        assert rec_default["developer"] == PLATFORM_DEVELOPER
        assert rec_default["adapted"] is True

        rec_ext = get_skill("bbcd1234")
        assert rec_ext["developer"] == UNKNOWN_DEVELOPER
        assert rec_ext["adapted"] is False

        metas = get_enabled_skills_metadata(["abcd1234", "bbcd1234"])
        assert [m["id"] for m in metas] == ["abcd1234"]


class TestDevAgentMarkSkillAdapted:
    def test_mark_skill_adapted_tool(self, isolated_data_dir, monkeypatch, tmp_path):
        from dev_agent import config as dconfig
        root = tmp_path / "sandbox"
        (root / "backups").mkdir(parents=True, exist_ok=True)
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dconfig, "PROJECT_ROOT", root)
        monkeypatch.setattr(dconfig, "BACKUPS_DIR", root / "backups")
        monkeypatch.setattr(dconfig, "WORKSPACE_DIR", root / "workspace")
        monkeypatch.setattr(dconfig, "CHANGELOG_FILE", root / "CHANGELOG.md")
        monkeypatch.setattr(dconfig, "PROTECTED_FILES", ())

        from core.skills_library import import_skill_from_folder, get_skill
        src = os.path.join(isolated_data_dir, "tool_src")
        os.makedirs(src)
        with open(os.path.join(src, "s.md"), "w", encoding="utf-8") as f:
            f.write("# s")
        result = import_skill_from_folder(src, name="ToolSkill")
        sid = result["skill"]["id"]
        assert get_skill(sid)["adapted"] is False

        from dev_agent.tool_executor import ToolExecutor
        te = ToolExecutor()
        out = te.mark_skill_adapted(sid)
        assert out["ok"] is True
        assert out["skill"]["adapted"] is True
        assert get_skill(sid)["adapted"] is True

        bad = te.mark_skill_adapted("deadbeef")
        assert bad["ok"] is False


# ─── UI: Adapt button hand-off ───────────────────────────────────────────────

@pytest.fixture()
def ui_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st


def _rerender(st, fn):
    try:
        fn()
    except StopRerun:
        pass


def _button_keys(st):
    return [c[2].get("key") for c in st.calls if c[0] == "button"]


def test_adapt_button_requests_adaptation(ui_env, monkeypatch):
    non_adapted = {
        "id": "abcd1234", "name": "External Skill", "description": "ext desc",
        "folder": "External_Skill", "developer": "unknown",
        "adapted": False, "source": "",
    }
    adapted = {
        "id": "bbcd1234", "name": "Platform Skill", "description": "",
        "folder": "Platform_Skill", "developer": "SagaAI",
        "adapted": True, "source": "defaults/Platform_Skill",
    }

    import core.skills_library as slib_mod
    monkeypatch.setattr(slib_mod, "list_skills", lambda: [adapted, non_adapted])
    monkeypatch.setattr(slib_mod, "get_skills_root", lambda: "/tmp/skills")

    import ui.pages.orchestrator as orch_page
    monkeypatch.setattr(orch_page, "get_orchestrator", lambda s: None)

    from ui.pages.skills_library import page_skills_library

    st = ui_env
    st.session_state.update({"ui_lang": "English"})
    _rerender(st, page_skills_library)

    keys = _button_keys(st)
    assert "slib_adapt_abcd1234" in keys, f"Adapt button missing: {keys}"
    assert "slib_adapt_bbcd1234" not in keys, f"adapted skill rendered Adapt: {keys}"

    st.click("slib_adapt_abcd1234")
    _rerender(st, page_skills_library)

    task = st.session_state.get("orch_dev_agent_pending_task")
    assert task and "abcd1234" in task, f"pending task not set: {task!r}"
    assert st.session_state.get("current_page") == "orchestrator:dev_agent"
    assert st.session_state.get("last_active_entity_id") == "dev_agent"
