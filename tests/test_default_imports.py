# -*- coding: utf-8 -*-
"""
test_default_imports.py - tests for the defaults/ layout and import wiring.

Verifies that:
  1. core.defaults loads both DevAgent (new format) and YaAgent (legacy-compatible)
     default orchestrators, including instructions with front-matter parsing.
  2. The built-in bootstrap (ensure_builtin_orchestrators) creates DevAgent with its
     prompt taken from dev_agent/system_prompt.md (single source of truth) and
     imports default orchestrators from defaults/ in a fresh install.
  3. Default instructions from defaults/orchestrators/dev_agent/instructions/*.md
     are seeded into the DevAgent orchestrator; deleting a file excludes it.
  4. When defaults/orchestrators/ is absent, no default orchestrators are
     imported (the legacy presets fallback has been removed).
"""
import os
import sys
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "defaults"


@pytest.fixture
def isolated_data_dir(tmp_path):
    """Temporary DATA_DIR isolating tests from real data."""
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = str(tmp_path)

    import core.paths as paths_mod
    old_attrs = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR", "RAG_BASES_DIR"):
        old_attrs[attr] = getattr(paths_mod, attr, None)

    paths_mod.DATA_DIR = str(tmp_path)
    paths_mod.DB_PATH = os.path.join(str(tmp_path), "sagaai.db")
    paths_mod.DEVAGENT_DB_PATH = os.path.join(str(tmp_path), "devagent.db")
    paths_mod.HISTORY_DIR = os.path.join(str(tmp_path), "history")
    paths_mod.SYSTEM_PROMPTS_DIR = os.path.join(str(tmp_path), "system_prompts")
    paths_mod.RAG_BASES_DIR = os.path.join(str(tmp_path), "rag_bases")

    # core.rag caches RAG_BASES_DIR/_DEFAULTS_MARKER_FILE at import time.
    # Reload it so the default RAG bases seeding stays under the temporary
    # DATA_DIR instead of leaking into the real (package-root) DATA_DIR.
    import importlib
    import core.rag as rag_mod
    importlib.reload(rag_mod)

    import storage.db as db_mod
    db_mod.DB_PATH = paths_mod.DB_PATH
    db_mod.DEVAGENT_DB_PATH = paths_mod.DEVAGENT_DB_PATH

    from storage.db import reset_engine, reset_devagent_engine
    reset_engine()
    reset_devagent_engine()

    yield tmp_path

    reset_engine()
    reset_devagent_engine()

    if old_env is not None:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr, val in old_attrs.items():
        if val is not None:
            setattr(paths_mod, attr, val)

    # Re-read the original RAG_BASES_DIR so subsequent tests see the real
    # package-root DATA_DIR again (core.rag was reloaded for this test).
    importlib.reload(rag_mod)


class TestDefaultsLoaders:
    def test_list_default_orchestrators(self):
        from core import defaults as d
        slugs = d.list_default_orchestrator_slugs()
        assert "dev_agent" in slugs
        assert "ya_agent" in slugs

    def test_load_dev_agent_returns_none(self):
        """DevAgent is NOT a default orchestrator: its prompt lives in
        dev_agent/system_prompt.md, not in defaults/orchestrators/dev_agent/."""
        from core import defaults as d
        data = d.load_default_orchestrator("dev_agent")
        assert data is None

    def test_load_ya_agent_legacy_format(self):
        from core import defaults as d
        data = d.load_default_orchestrator("ya_agent")
        assert data is not None
        assert data["name"] == "YaAgent"
        assert data["prompt_text"].strip()

    def test_dev_agent_instructions_loaded(self):
        from core import defaults as d
        import core.defaults as defaults_mod
        instr_dir = os.path.join(defaults_mod.orchestrators_dir(), "dev_agent", "instructions")
        assert os.path.isdir(instr_dir)
        files = [f for f in sorted(os.listdir(instr_dir)) if f.endswith(".md")]
        ids = set()
        for fname in files:
            fpath = os.path.join(instr_dir, fname)
            raw = open(fpath, "r", encoding="utf-8").read()
            meta, _body = d.parse_front_matter(raw, default_id=fname[:-3])
            ids.add(meta.get("id") or fname[:-3])
        assert "assistant_creator" in ids
        assert "employee_creator" in ids
        assert "self_reflection" in ids

    def test_global_settings(self):
        from core import defaults as d
        settings = d.load_global_settings()
        assert "ui_lang" in settings

    def test_front_matter_parser(self):
        from core.defaults import parse_front_matter
        text = "---\nid: test_one\nname: Test One\n---\n\nBody line\n"
        meta, body = parse_front_matter(text)
        assert meta["id"] == "test_one"
        assert meta["name"] == "Test One"
        assert body == "Body line\n"


class TestBootstrapDefaults:
    def test_dev_agent_and_defaults_created(self, isolated_data_dir):
        from core.default_imports import ensure_all_defaults
        result = ensure_all_defaults()
        orch = result["orchestrators"]
        assert orch.get("dev_agent") == "created"
        assert orch.get("ya_agent") == "created"

    def test_default_orchestrator_config(self, isolated_data_dir):
        from core.orchestrators import get_orchestrator
        from core.default_imports import ensure_all_defaults
        ensure_all_defaults()
        ya = get_orchestrator("ya_agent")
        assert ya is not None
        assert ya["name"] == "YaAgent"
        assert ya["config"]["strong_service"] == "YandexAI"

    def test_default_orchestrator_gets_full_toolset(self, isolated_data_dir):
        from core.orchestrators import get_orchestrator
        from core.default_imports import ensure_all_defaults
        ensure_all_defaults()
        ya = get_orchestrator("ya_agent")
        tools = ya.get("tools") or []
        for name in ("read_file", "propose_file", "run_test", "web_search", "list_skills_library"):
            assert name in tools

    def test_assistant_web_search_settings_persist_in_manifest(self, isolated_data_dir):
        """Per-assistant web-search overrides are stored in the runtime folder
        manifest of a created assistant (constrains where its web search may
        look). The bundled 'ai_studio_docs' preset was intentionally removed."""
        from core.default_imports import ensure_all_defaults
        from core.assistants import create_assistant, get_assistant_by_id
        from core.assistant_folders import (
            get_assistant_web_search_settings,
            set_assistant_web_search_settings,
        )

        ensure_all_defaults()
        pid = create_assistant(
            name="WebSearchOverridesBot", service="YandexAI", model="m",
            temperature=0.3, text="sys",
        )
        assistant = get_assistant_by_id(pid)
        assert assistant is not None
        slug = assistant["slug"]

        assert set_assistant_web_search_settings(
            slug, context_size="medium",
            allowed_domains=["yandex.cloud", "aistudio.yandex.ru"],
        )

        settings = get_assistant_web_search_settings(slug)
        assert settings.get("context_size") == "medium"
        assert settings.get("allowed_domains") == ["yandex.cloud", "aistudio.yandex.ru"]
        assert get_assistant_web_search_settings("missing_slug") == {}

    def test_default_instructions_seeded(self, isolated_data_dir):
        from core.default_imports import ensure_all_defaults
        from core.orchestrators import orch_list_instructions
        ensure_all_defaults()
        ids = [i["id"] for i in orch_list_instructions("dev_agent")]
        assert "assistant_creator" in ids
        assert "employee_creator" in ids
        assert "self_reflection" in ids

    def test_legacy_orchestrator_creator_migrates_to_employee_creator(self, isolated_data_dir):
        """A pre-existing 'orchestrator_creator' instruction (old id) is migrated
        into the canonical 'employee_creator' instruction, keeping user-edited
        text, and the legacy row is removed."""
        from core.orchestrators import (
            DEVAGENT_SLUG,
            orch_get_instruction,
            orch_save_instruction,
        )
        from core.default_imports import ensure_all_defaults

        legacy_text = (
            "Legacy Employee Creator prompt (user-edited).\n"
            "Create employees via create_orchestrator(...)"
        )
        assert orch_save_instruction(
            DEVAGENT_SLUG, "orchestrator_creator",
            name="Employee Creator (legacy)",
            description="Legacy description",
            prompt_text=legacy_text,
        )

        ensure_all_defaults()

        migrated = orch_get_instruction(DEVAGENT_SLUG, "employee_creator")
        assert migrated is not None
        assert migrated["name"] == "Employee Creator (legacy)"
        assert migrated["description"] == "Legacy description"
        assert legacy_text in migrated["text"]

        # The legacy id must no longer exist.
        assert orch_get_instruction(DEVAGENT_SLUG, "orchestrator_creator") is None

    def test_idempotent(self, isolated_data_dir):
        from core.default_imports import ensure_all_defaults
        first = ensure_all_defaults()
        second = ensure_all_defaults()
        assert first["orchestrators"]["ya_agent"] == "created"
        assert second["orchestrators"]["ya_agent"] == "exists"

    def test_removing_default_folder_excludes_it(self, isolated_data_dir):
        """Deleting a default orchestrator folder removes it from the import."""
        ya_dir = DEFAULTS / "orchestrators" / "ya_agent"
        backup_dir = DEFAULTS / "orchestrators" / ".ya_agent_bak"
        try:
            if ya_dir.exists():
                shutil.move(str(ya_dir), str(backup_dir))
            from core.default_imports import ensure_all_defaults
            result = ensure_all_defaults()
            # The folder is gone, so YaAgent must not be imported.
            assert result["orchestrators"].get("ya_agent") is None
            from core.orchestrators import get_orchestrator
            assert get_orchestrator("ya_agent") is None
        finally:
            if backup_dir.exists() and not ya_dir.exists():
                shutil.move(str(backup_dir), str(ya_dir))


class TestLegacyFallbacks:
    def test_orphan_orchestrator_json_format(self, isolated_data_dir):
        """A defaults/orchestrators folder with a plain legacy export JSON
        (orchestrator.json) is still importable."""
        import json
        from pathlib import Path as P
        slug_dir = DEFAULTS / "orchestrators" / "test_legacy"
        try:
            slug_dir.mkdir(parents=True, exist_ok=True)
            (slug_dir / "orchestrator.json").write_text(json.dumps({
                "format": "sagaai_orchestrator/v1",
                "slug": "test_legacy",
                "name": "TestLegacy",
                "description": "",
                "prompt_text": "You are a test legacy agent.",
                "config": {"strong_service": "DeepSeek", "strong_model": "deepseek-v4-pro"},
                "tools": [],
                "max_steps": 10,
                "auto_apply": True,
            }), encoding="utf-8")

            import core.defaults as defaults_mod
            data = defaults_mod.load_default_orchestrator("test_legacy")
            assert data is not None
            assert data["name"] == "TestLegacy"
            assert "You are a test legacy agent" in data["prompt_text"]

            from core.default_imports import ensure_all_defaults
            ensure_all_defaults()
            from core.orchestrators import get_orchestrator
            orch = get_orchestrator("test_legacy")
            assert orch is not None
            assert orch["name"] == "TestLegacy"
            # Cleanup DB row so subsequent runs are unaffected.
            from core.orchestrators import delete_orchestrator
            delete_orchestrator("test_legacy")
        finally:
            shutil.rmtree(str(slug_dir), ignore_errors=True)

    def test_no_defaults_means_no_orchestrator_import(self, isolated_data_dir, monkeypatch):
        """Without defaults/orchestrators/ nothing is imported - presets
        fallback no longer exists."""
        from core.default_imports import ensure_default_orchestrators
        import core.defaults as defaults_mod

        missing_dir = str(Path(isolated_data_dir) / "no_defaults_orchestrators")
        monkeypatch.setattr(defaults_mod, "orchestrators_dir", lambda: missing_dir)
        result = ensure_default_orchestrators()
        assert result == {}
