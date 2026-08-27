# -*- coding: utf-8 -*-
"""
Tests for core.assistant_folders and the folder-based assistant CRUD.
Uses a temporary DATA_DIR so no real DB or config is touched.
"""
import os
import sys
import json
import tempfile
import shutil
from pathlib import Path

import pytest

# Allow importing the sagaai package from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR that isolates assistant folders from real DB."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_asst_")
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

    # Make fresh engine point at the tmp DB.
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


# ── Test core.assistant_folders module ────────────────────────────────────────

class TestAssistantFolders:
    def test_normalize_slug(self):
        from core.assistant_folders import normalize_slug
        assert normalize_slug("My Assistant") == "my_assistant"
        assert normalize_slug("Ünïcode Name") in ("n_code_name", "unicode_name")
        assert normalize_slug("Редактор текста") == "redaktor_teksta"
        assert normalize_slug("") == "assistant"
        assert normalize_slug("  A-B_C  ") == "a_b_c"

    def test_ensure_and_remove_dir(self, isolated_data_dir):
        from core.assistant_folders import (
            ensure_assistant_dir, remove_assistant_dir, assistant_folder_exists,
        )
        slug = "my_assistant"
        ensure_assistant_dir(slug)
        assert assistant_folder_exists(slug)
        remove_assistant_dir(slug)
        assert not assistant_folder_exists(slug)

    def test_bundle_save_and_load(self, isolated_data_dir):
        from core.assistant_folders import save_assistant_bundle, load_assistant_bundle
        slug = "bundle_test"
        data = {"slug": slug, "name": "Test", "model": "deepseek-v4-pro"}
        assert save_assistant_bundle(slug, data)
        loaded = load_assistant_bundle(slug)
        assert loaded is not None
        assert loaded["name"] == "Test"
        assert loaded["slug"] == slug

    def test_bundle_load_missing(self, isolated_data_dir):
        from core.assistant_folders import load_assistant_bundle
        assert load_assistant_bundle("nonexistent") is None

    def test_prompt_save_and_load(self, isolated_data_dir):
        from core.assistant_folders import save_assistant_prompt, load_assistant_prompt
        slug = "prompt_test"
        assert save_assistant_prompt(slug, "## Role\nYou are a test assistant.")
        assert load_assistant_prompt(slug) == "## Role\nYou are a test assistant."

    def test_files_crud(self, isolated_data_dir):
        from core.assistant_folders import (
            save_assistant_file, list_assistant_files,
            load_assistant_file_content, delete_assistant_file,
        )
        slug = "files_test"
        assert save_assistant_file(slug, "doc", "content one")
        assert save_assistant_file(slug, "doc2.txt", "content two")
        files = list_assistant_files(slug)
        assert files == ["doc.txt", "doc2.txt"]
        assert load_assistant_file_content(slug, "doc.txt") == "content one"
        assert delete_assistant_file(slug, "doc.txt")
        assert list_assistant_files(slug) == ["doc2.txt"]

    def test_export_import_folder_roundtrip(self, isolated_data_dir):
        from core.assistant_folders import (
            save_assistant_bundle, save_assistant_prompt, save_assistant_file,
            export_assistant_folder, import_assistant_folder,
            load_assistant_bundle, load_assistant_prompt, list_assistant_files,
        )
        slug = "export_me"
        save_assistant_bundle(slug, {"slug": slug, "name": "Export", "model": "m1"})
        save_assistant_prompt(slug, "## Role\nExport assistant.")
        save_assistant_file(slug, "notes", "hello")
        data = export_assistant_folder(slug)
        assert data is not None
        assert data["bundle"]["name"] == "Export"
        assert data["prompt_text"] == "## Role\nExport assistant."
        assert data["files"]["notes.txt"] == "hello"

        target = "imported"
        assert import_assistant_folder(target, data)
        assert load_assistant_bundle(target)["name"] == "Export"
        assert load_assistant_prompt(target) == "## Role\nExport assistant."
        assert list_assistant_files(target) == ["notes.txt"]

    def test_export_nonexistent_folder(self, isolated_data_dir):
        from core.assistant_folders import export_assistant_folder
        assert export_assistant_folder("nope_not_here") is None

    def test_list_folder_names(self, isolated_data_dir):
        from core.assistant_folders import ensure_assistant_dir, list_assistant_folder_names
        ensure_assistant_dir("alpha")
        ensure_assistant_dir("beta")
        assert list_assistant_folder_names() == ["alpha", "beta"]

    def test_sync_assistant_to_folder(self, isolated_data_dir):
        from core.assistant_folders import sync_assistant_to_folder
        from core.assistant_folders import (
            load_assistant_bundle, load_assistant_prompt,
        )
        assistant = {
            "id": "abc12345",
            "slug": "synced",
            "name": "Synced",
            "service": "DeepSeek",
            "model": "deepseek-v4-pro",
            "temperature": 0.5,
            "description": "desc",
            "tools": ["read_file"],
            "max_tool_calls": 4,
            "max_tokens": 1000,
            "text": "## Role\nSynced assistant.",
        }
        assert sync_assistant_to_folder(assistant)
        bundle = load_assistant_bundle("synced")
        assert bundle["name"] == "Synced"
        assert bundle["model"] == "deepseek-v4-pro"
        assert load_assistant_prompt("synced") == "## Role\nSynced assistant."

    def test_set_and_get_web_search_settings(self, isolated_data_dir):
        from core.assistant_folders import (
            save_assistant_bundle, set_assistant_web_search_settings,
            get_assistant_web_search_settings,
        )
        slug = "websearch_test"
        save_assistant_bundle(slug, {"slug": slug, "name": "WS"})
        assert set_assistant_web_search_settings(
            slug, context_size="high", allowed_domains="docs.yandex.ru, example.com"
        )
        settings = get_assistant_web_search_settings(slug)
        assert settings["context_size"] == "high"
        assert settings["allowed_domains"] == ["docs.yandex.ru", "example.com"]
        # Removing overrides stores nothing.
        assert set_assistant_web_search_settings(slug, context_size="", allowed_domains=[])
        assert get_assistant_web_search_settings(slug) == {}

    def test_set_web_search_settings_missing_folder(self, isolated_data_dir):
        from core.assistant_folders import set_assistant_web_search_settings
        assert not set_assistant_web_search_settings("no_folder", context_size="low")

    def test_sync_preserves_web_search_settings(self, isolated_data_dir):
        from core.assistant_folders import (
            save_assistant_bundle, set_assistant_web_search_settings,
            sync_assistant_to_folder, load_assistant_bundle,
        )
        slug = "ws_sync_test"
        save_assistant_bundle(slug, {"slug": slug, "name": "WSSync"})
        set_assistant_web_search_settings(
            slug, context_size="low", allowed_domains=["api.example.org"]
        )
        sync_assistant_to_folder({
            "id": "zzz99999", "slug": slug, "name": "WSSync",
            "service": "YandexAI", "model": "m", "temperature": 0.3,
            "description": "", "tools": [], "max_tool_calls": None,
            "max_tokens": None, "text": "# Role\n",
        })
        bundle = load_assistant_bundle(slug)
        assert bundle["web_search_context_size"] == "low"
        assert bundle["web_search_allowed_domains"] == ["api.example.org"]


# ── Test folder-based assistant CRUD (core.assistants) ───────────────────────

class TestAssistantCRUDWithFolders:
    def test_create_assistant_creates_folder_and_slug(self, isolated_data_dir):
        from core.assistants import create_assistant, get_assistant_by_slug, delete_assistant
        from core.assistant_folders import assistant_folder_exists
        pid = create_assistant(
            "My Test Assistant", "DeepSeek", "deepseek-v4-pro", 0.5,
            "## Role\nYou are a test assistant.",
        )
        assert pid is not None
        by_slug = get_assistant_by_slug("my_test_assistant")
        assert by_slug is not None
        assert by_slug["id"] == pid
        assert assistant_folder_exists("my_test_assistant")
        delete_assistant(pid)
        assert not assistant_folder_exists("my_test_assistant")

    def test_unique_slug_on_duplicate_name(self, isolated_data_dir):
        from core.assistants import create_assistant, delete_assistant
        from core.assistants import load_assistants_index
        pid1 = create_assistant("Same Name", "DeepSeek", "m", 0.5, "## Role\nOne")
        pid2 = create_assistant("Same Name", "DeepSeek", "m", 0.5, "## Role\nTwo")
        assert pid1 is not None and pid2 is not None
        slugs = {a["slug"] for a in load_assistants_index()}
        assert "same_name" in slugs
        assert "same_name_2" in slugs
        delete_assistant(pid1)
        delete_assistant(pid2)

    def test_update_assistant_resyncs_folder(self, isolated_data_dir):
        from core.assistants import create_assistant, update_assistant, delete_assistant
        from core.assistant_folders import load_assistant_prompt
        pid = create_assistant("Updatable", "DeepSeek", "m", 0.5, "## Role\nBefore")
        assert pid
        assert update_assistant(pid, "Updatable", "DeepSeek", "m", 0.7,
                                "## Role\nAfter", tools=["read_file"])
        assert load_assistant_prompt("updatable") == "## Role\nAfter"
        delete_assistant(pid)

    def test_save_prompt_text_resyncs_folder(self, isolated_data_dir):
        from core.assistants import create_assistant, save_assistant_prompt_text, delete_assistant
        from core.assistant_folders import load_assistant_prompt
        pid = create_assistant("PromptSync", "DeepSeek", "m", 0.5, "## Role\nOld")
        assert pid
        assert save_assistant_prompt_text(pid, "## Role\nNew Prompt")
        assert load_assistant_prompt("promptsync") == "## Role\nNew Prompt"
        delete_assistant(pid)

    def test_export_and_import_assistant(self, isolated_data_dir):
        from core.assistants import (
            create_assistant, export_assistant, import_assistant, delete_assistant,
            get_assistant_by_slug,
        )
        pid = create_assistant(
            "Exporter", "DeepSeek", "deepseek-v4-pro", 0.5,
            "## Role\nExport me.", description="d", tools=["read_file"],
        )
        assert pid
        data = export_assistant(pid)
        assert data is not None
        assert data["format"] == "sagaai_assistant/v1"
        assert data["prompt_text"] == "## Role\nExport me."

        res = import_assistant(data, overwrite=False)
        assert res["ok"]
        assert res["slug"] == "exporter_2"  # original slug already exists
        imported = get_assistant_by_slug("exporter_2")
        assert imported is not None
        delete_assistant(pid)
        delete_assistant(res["id"])

    def test_import_rejects_bad_format(self, isolated_data_dir):
        from core.assistants import import_assistant
        res = import_assistant({"format": "unknown"})
        assert not res["ok"]

    def test_reload_assistant_from_folder(self, isolated_data_dir):
        from core.assistant_folders import save_assistant_bundle, save_assistant_prompt
        from core.assistants import reload_assistant_from_folder, get_assistant_by_slug
        slug = "from_folder"
        save_assistant_bundle(slug, {
            "slug": slug, "name": "From Folder", "service": "YandexAI",
            "model": "alice", "temperature": 0.1,
        })
        save_assistant_prompt(slug, "## Role\nLoaded from folder.")
        res = reload_assistant_from_folder(slug)
        assert res["ok"], res
        assert res["action"] == "created"
        a = get_assistant_by_slug(slug)
        assert a is not None
        assert a["name"] == "From Folder"
        assert a["model"] == "alice"

    def test_reload_rejects_folder_without_manifest(self, isolated_data_dir):
        from core.assistant_folders import ensure_assistant_dir
        from core.assistants import reload_assistant_from_folder
        ensure_assistant_dir("no_manifest")
        res = reload_assistant_from_folder("no_manifest")
        assert not res["ok"]


# ─── Test entity sync ─────────────────────────────────────────────────────────

class TestEntitySync:
    def test_sync_assistants_imports_folder(self, isolated_data_dir):
        from core.assistant_folders import save_assistant_bundle, save_assistant_prompt
        from core.entity_sync import sync_assistants
        from core.assistants import get_assistant_by_slug
        slug = "synced_in"
        save_assistant_bundle(slug, {"slug": slug, "name": "Synced In", "model": "m"})
        save_assistant_prompt(slug, "## Role\nSynced.")
        results = sync_assistants()
        assert results.get(slug) == "created"
        assert get_assistant_by_slug(slug) is not None

    def test_sync_assistants_backfills_legacy_db_records(self, isolated_data_dir):
        from core.entity_sync import sync_assistants
        from core.assistant_folders import assistant_folder_exists
        from storage.repository import repo_create_assistant
        # Legacy record without slug and without folder.
        ok = repo_create_assistant(
            assistant_id="legacy001", slug=None, name="Legacy Assistant",
            service="S", model="M", temperature=0.5,
            prompt_text="## Role\nLegacy.",
        )
        assert ok
        results = sync_assistants()
        assert results.get("legacy_assistant") == "created_folder"
        assert assistant_folder_exists("legacy_assistant")

    def test_ensure_entity_folders_sync(self, isolated_data_dir):
        from core.entity_sync import ensure_entity_folders_sync
        result = ensure_entity_folders_sync()
        assert "assistants" in result
        assert "orchestrators" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
