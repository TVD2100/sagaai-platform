# -*- coding: utf-8 -*-
"""
Tests for core.skills_library - standardized orchestrator skills.

Covers:
  - registry listing / get / update / delete,
  - safe ZIP extraction + skill install from ZIP (incl. path traversal attack),
  - skill install from a local folder,
  - GitHub URL parsing,
  - skills metadata block building for the orchestrator system prompt,
  - enabled-skills persistence through core.orchestrators.

Uses a temporary DATA_DIR so no real user data is touched.
"""
import io
import importlib
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR that isolates skills/ and the DB from the real one.

    Uses importlib.reload(core.paths) while the env var is set so that every
    module imported later (including core.skills_library) sees the temporary
    DATA_DIR. In teardown the env var is restored and core.paths is reloaded
    again to a clean state.
    """
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


def make_zip(entries) -> bytes:
    """Build an in-memory ZIP from a dict {arcname: content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestRegistry:
    def test_list_empty_when_no_registry(self, isolated_data_dir):
        from core.skills_library import list_skills
        assert list_skills() == []

    def test_install_from_folder_and_registry(self, isolated_data_dir):
        from core.skills_library import import_skill_from_folder, list_skills, get_skill, list_skill_files
        src = os.path.join(isolated_data_dir, "src_skill")
        os.makedirs(os.path.join(src, "scripts"), exist_ok=True)
        with open(os.path.join(src, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("# My skill\n")
        with open(os.path.join(src, "scripts", "run.py"), "w", encoding="utf-8") as f:
            f.write("print('hi')\n")

        result = import_skill_from_folder(src, name="My Skill", description="Does things")
        assert result.get("ok")
        sid = result["skill"]["id"]
        assert result["skill"]["name"] == "My Skill"

        skills = list_skills()
        assert len(skills) == 1
        assert skills[0]["id"] == sid
        got = get_skill(sid)
        assert got["description"] == "Does things"
        files = list_skill_files(sid)
        assert sorted(files) == sorted(["SKILL.md", os.path.join("scripts", "run.py")])

        # Files are copied, source untouched.
        with open(os.path.join(src, "SKILL.md"), "r", encoding="utf-8") as f:
            assert f.read() == "# My skill\n"

    def test_update_metadata_and_rename_folder(self, isolated_data_dir):
        from core.skills_library import import_skill_from_folder, update_skill, get_skill, get_skill_folder
        src = os.path.join(isolated_data_dir, "src2")
        os.makedirs(src)
        with open(os.path.join(src, "note.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        result = import_skill_from_folder(src, name="Old", description="Old desc")
        sid = result["skill"]["id"]
        old_folder = get_skill(sid)["folder"]

        assert update_skill(sid, name="New", description="New desc", folder="renamed_folder")
        got = get_skill(sid)
        assert got["name"] == "New"
        assert got["description"] == "New desc"
        assert got["folder"] == "renamed_folder"
        assert not os.path.exists(os.path.join(isolated_data_dir, "skills", old_folder))
        assert os.path.isdir(get_skill_folder(sid))

    def test_update_unknown_fails(self, isolated_data_dir):
        from core.skills_library import update_skill, delete_skill, get_skill
        assert not update_skill("deadbeef", name="X")
        assert not delete_skill("deadbeef")
        assert get_skill("deadbeef") is None

    def test_delete_removes_registry_and_folder(self, isolated_data_dir):
        from core.skills_library import import_skill_from_folder, delete_skill, list_skills
        src = os.path.join(isolated_data_dir, "src3")
        os.makedirs(src)
        with open(os.path.join(src, "a.txt"), "w", encoding="utf-8") as f:
            f.write("a")
        result = import_skill_from_folder(src, name="Temp")
        sid = result["skill"]["id"]
        folder = result["skill"]["folder"]
        assert os.path.isdir(os.path.join(isolated_data_dir, "skills", folder))

        assert delete_skill(sid)
        assert list_skills() == []
        assert not os.path.exists(os.path.join(isolated_data_dir, "skills", folder))


class TestZipImport:
    def test_zip_with_single_folder_wrapper(self, isolated_data_dir):
        from core.skills_library import import_skill_from_zip, list_skill_files
        zip_bytes = make_zip({
            "my-skill-master/SKILL.md": "# Skill",
            "my-skill-master/docs/readme.txt": "readme",
        })
        result = import_skill_from_zip(zip_bytes, name="ZSkill")
        assert result.get("ok")
        assert result["skill"]["name"] == "ZSkill"
        files = list_skill_files(result["skill"]["id"])
        assert sorted(files) == sorted(["SKILL.md", os.path.join("docs", "readme.txt")])

    def test_zip_flat_files(self, isolated_data_dir):
        from core.skills_library import import_skill_from_zip, list_skill_files
        zip_bytes = make_zip({
            "SKILL.md": "# Direct",
            "helper.md": "helper",
        })
        result = import_skill_from_zip(zip_bytes)
        assert result.get("ok")
        files = list_skill_files(result["skill"]["id"])
        assert "SKILL.md" in files
        assert "helper.md" in files

    def test_zip_with_subfolder_selector(self, isolated_data_dir):
        from core.skills_library import import_skill_from_zip, list_skill_files
        zip_bytes = make_zip({
            "skills-main/skills/pptx/SKILL.md": "# PPTX",
            "skills-main/skills/docx/SKILL.md": "# DOCX",
            "skills-main/README.md": "repo",
        })
        result = import_skill_from_zip(zip_bytes, name="PPTX skill", subfolder=os.path.join("skills", "pptx"))
        assert result.get("ok")
        files = list_skill_files(result["skill"]["id"])
        assert files == ["SKILL.md"]

    def test_zip_missing_subfolder_raises(self, isolated_data_dir):
        from core.skills_library import import_skill_from_zip, SkillsLibraryError
        zip_bytes = make_zip({"repo-main/SKILL.md": "x"})
        with pytest.raises(SkillsLibraryError):
            import_skill_from_zip(zip_bytes, subfolder="nope")

    def test_path_traversal_rejected(self, isolated_data_dir):
        from core.skills_library import import_skill_from_zip, SkillsLibraryError
        zip_bytes = make_zip({
            "../evil.txt": "evil",
        })
        with pytest.raises(SkillsLibraryError):
            import_skill_from_zip(zip_bytes)
        # Nothing escaped outside skills/
        assert not os.path.exists(os.path.join(isolated_data_dir, "evil.txt"))

    def test_invalid_zip_rejected(self, isolated_data_dir):
        from core.skills_library import import_skill_from_zip, SkillsLibraryError
        with pytest.raises(SkillsLibraryError):
            import_skill_from_zip(b"not a zip file at all")

    def test_empty_zip_rejected(self, isolated_data_dir):
        from core.skills_library import import_skill_from_zip, SkillsLibraryError
        with pytest.raises(SkillsLibraryError):
            import_skill_from_zip(make_zip({}))


class TestHelpers:
    def test_parse_github_url_repo(self):
        from core.skills_library import parse_github_url
        assert parse_github_url("https://github.com/anthropics/skills") == {
            "owner": "anthropics", "repo": "skills", "ref": "main", "path": ""
        }

    def test_parse_github_url_tree_path(self):
        from core.skills_library import parse_github_url
        parsed = parse_github_url("https://github.com/anthropics/skills/tree/main/skills/pptx")
        assert parsed["owner"] == "anthropics"
        assert parsed["repo"] == "skills"
        assert parsed["ref"] == "main"
        assert parsed["path"] == "skills/pptx"

    def test_parse_github_url_wrong_domain(self):
        from core.skills_library import parse_github_url
        assert parse_github_url("https://example.com/a/b") == {}

    def test_metadata_text(self, isolated_data_dir):
        from core.skills_library import (
            import_skill_from_folder, get_enabled_skills_metadata,
            build_skills_metadata_text, get_skills_root,
        )
        src = os.path.join(isolated_data_dir, "meta_src")
        os.makedirs(src)
        with open(os.path.join(src, "s.txt"), "w", encoding="utf-8") as f:
            f.write("s")
        result = import_skill_from_folder(src, name="MetaSkill", description="Meta desc", adapted=True)
        sid = result["skill"]["id"]

        metas = get_enabled_skills_metadata([sid, "deadbeef"])
        assert len(metas) == 1
        assert metas[0]["name"] == "MetaSkill"
        assert metas[0]["adapted"] is True

        text = build_skills_metadata_text([sid])
        assert "MetaSkill" in text
        assert "Meta desc" in text
        assert text == build_skills_metadata_text([sid, "deadbeef"])
        # Root folder is created
        assert os.path.isdir(get_skills_root())

    def test_metadata_text_empty(self, isolated_data_dir):
        from core.skills_library import build_skills_metadata_text, get_enabled_skills_metadata
        assert build_skills_metadata_text([]) == ""
        assert build_skills_metadata_text(["badid"]) == ""
        assert get_enabled_skills_metadata([]) == []


class TestOrchestratorIntegration:
    def test_enabled_skills_persist_in_config(self, isolated_data_dir):
        from core.orchestrators import create_orchestrator, get_enabled_skills, set_enabled_skills, delete_orchestrator
        slug = "slib_int_orch"
        created = create_orchestrator(slug, "Slib Int")
        assert created is not None
        assert get_enabled_skills(slug) == []
        assert set_enabled_skills(slug, ["aabbccdd", "aabbccdd", ""])
        assert get_enabled_skills(slug) == ["aabbccdd"]
        delete_orchestrator(slug)

    def test_build_skill_dicts_appends_skills_metadata(self, isolated_data_dir):
        from core.skills_library import import_skill_from_folder
        from core.orchestrators import (
            create_orchestrator, set_enabled_skills, build_skill_dicts, delete_orchestrator,
        )
        src = os.path.join(isolated_data_dir, "prompt_src")
        os.makedirs(src)
        with open(os.path.join(src, "k.md"), "w", encoding="utf-8") as f:
            f.write("k")
        result = import_skill_from_folder(src, name="PromptSkill", description="PD", adapted=True)
        sid = result["skill"]["id"]

        slug = "skill_dict_orch"
        assert create_orchestrator(slug, "DictOrch", prompt_text="Base prompt")
        assert set_enabled_skills(slug, [sid])

        strong, weak = build_skill_dicts(slug)
        assert "Base prompt" in strong["text"]
        assert "PromptSkill" in strong["text"]
        assert "PD" in strong["text"]
        assert strong["text"] == weak["text"]
        delete_orchestrator(slug)

    def test_foreign_skills_not_in_prompt(self, isolated_data_dir):
        from core.orchestrators import (
            create_orchestrator, build_skill_dicts, delete_orchestrator,
        )
        slug = "no_skills_orch"
        assert create_orchestrator(slug, "NoSkills", prompt_text="Plain")
        strong, _ = build_skill_dicts(slug)
        assert strong["text"].strip() == "Plain"
        assert "Available skills" not in strong["text"]
        delete_orchestrator(slug)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
