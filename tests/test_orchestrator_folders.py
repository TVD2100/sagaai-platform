# -*- coding: utf-8 -*-
"""
Tests for core.orchestrator_folders and orchestrator CRUD with folders.
Uses a temporary DATA_DIR so no real DB or config is touched.
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

# Allow importing the sagaai package from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR that isolates orchestrator folders from real DB."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_orch_")
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

    # Drop any cached SQLAlchemy engines so they point at the tmp DB.
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


# ── Test orchestrator_folders module ──────────────────────────────────────────

class TestOrchestratorFolders:
    def test_ensure_and_remove_dir(self, isolated_data_dir):
        from core.orchestrator_folders import (
            ensure_orchestrator_dir, remove_orchestrator_dir, orchestrator_folder_exists,
        )
        slug = "my_orch"
        ensure_orchestrator_dir(slug)
        assert orchestrator_folder_exists(slug)
        remove_orchestrator_dir(slug)
        assert not orchestrator_folder_exists(slug)

    def test_bundle_save_and_load(self, isolated_data_dir):
        from core.orchestrator_folders import save_orchestrator_bundle, load_orchestrator_bundle
        slug = "bundle_test"
        data = {"slug": slug, "name": "Test", "prompt_text": "Hello"}
        assert save_orchestrator_bundle(slug, data)
        loaded = load_orchestrator_bundle(slug)
        assert loaded is not None
        assert loaded["name"] == "Test"
        assert loaded["prompt_text"] == "Hello"

    def test_bundle_load_missing(self, isolated_data_dir):
        from core.orchestrator_folders import load_orchestrator_bundle
        assert load_orchestrator_bundle("nonexistent") is None

    def test_save_and_get_function(self, isolated_data_dir):
        from core.orchestrator_folders import save_orchestrator_function, get_orchestrator_function
        code = "def invoke(**kwargs):\n    return {'ok': True}\n"
        assert save_orchestrator_function("func_test", "do_thing", code)
        fn = get_orchestrator_function("func_test", "do_thing")
        assert fn is not None
        assert fn["name"] == "do_thing"
        assert fn["code"] == code

    def test_save_function_bad_name_rejected(self, isolated_data_dir):
        from core.orchestrator_folders import save_orchestrator_function
        assert not save_orchestrator_function("bad", "1nvalid name", "code")
        assert not save_orchestrator_function("bad", "", "code")

    def test_list_and_delete_functions(self, isolated_data_dir):
        from core.orchestrator_folders import (
            save_orchestrator_function, list_orchestrator_functions,
            delete_orchestrator_function, get_orchestrator_function,
        )
        slug = "del_test"
        save_orchestrator_function(slug, "a", "code_a")
        save_orchestrator_function(slug, "b", "code_b")
        funcs = list_orchestrator_functions(slug)
        assert len(funcs) == 2
        delete_orchestrator_function(slug, "a")
        assert get_orchestrator_function(slug, "a") is None
        assert get_orchestrator_function(slug, "b") is not None

    def test_load_and_call_custom_function(self, isolated_data_dir):
        from core.orchestrator_folders import (
            save_orchestrator_function, load_orchestrator_function_module,
        )
        code = (
            "def invoke(**kwargs):\n"
            "    return {'ok': True, 'sum': sum(kwargs.values())}\n"
        )
        save_orchestrator_function("calc", "add", code)
        fn = load_orchestrator_function_module("calc", "add")
        assert fn is not None
        result = fn(a=1, b=2, c=3)
        assert result == {"ok": True, "sum": 6}

    def test_load_missing_function_returns_none(self, isolated_data_dir):
        from core.orchestrator_folders import load_orchestrator_function_module
        assert load_orchestrator_function_module("none", "nope") is None

    def test_load_all_functions(self, isolated_data_dir):
        from core.orchestrator_folders import (
            save_orchestrator_function, load_all_orchestrator_functions,
        )
        save_orchestrator_function("all_test", "f1", "def invoke(**kw): return {'ok':True}\n")
        save_orchestrator_function("all_test", "f2", "def invoke(**kw): return {'ok':False}\n")
        all_fn = load_all_orchestrator_functions("all_test")
        assert len(all_fn) == 2
        assert "f1" in all_fn
        assert "f2" in all_fn

    def test_instructions_crud(self, isolated_data_dir):
        from core.orchestrator_folders import (
            save_orchestrator_instruction, list_orchestrator_instructions,
            get_orchestrator_instruction, delete_orchestrator_instruction,
        )
        slug = "instr_test"
        assert save_orchestrator_instruction(slug, "i1", "Name", "Desc", "Prompt")
        lst = list_orchestrator_instructions(slug)
        assert len(lst) == 1
        assert lst[0]["id"] == "i1"
        inst = get_orchestrator_instruction(slug, "i1")
        assert inst["name"] == "Name"
        assert inst["text"] == "Prompt"
        assert delete_orchestrator_instruction(slug, "i1")
        assert len(list_orchestrator_instructions(slug)) == 0

    def test_instruction_autogenerated_id(self, isolated_data_dir):
        from core.orchestrator_folders import save_orchestrator_instruction, list_orchestrator_instructions
        slug = "autoid"
        assert save_orchestrator_instruction(slug, "", "Auto", "", "Prompt")
        lst = list_orchestrator_instructions(slug)
        assert len(lst) == 1
        assert len(lst[0]["id"]) == 8  # random hex

    def test_export_import_folder_roundtrip(self, isolated_data_dir):
        from core.orchestrator_folders import (
            save_orchestrator_bundle, save_orchestrator_function,
            save_orchestrator_instruction, export_orchestrator_folder,
            import_orchestrator_folder, get_orchestrator_function,
            get_orchestrator_instruction,
        )
        slug = "export_test"
        save_orchestrator_bundle(slug, {"slug": slug, "name": "E"})
        save_orchestrator_function(slug, "f1", "def invoke(**kw): return {'ok': True}\n")
        save_orchestrator_instruction(slug, "i1", "Inst", "", "P")
        data = export_orchestrator_folder(slug)
        assert data is not None
        assert "f1" in data["functions"]
        assert "i1" in data["instructions"]

        target = "imported"
        assert import_orchestrator_folder(target, data)
        fn = get_orchestrator_function(target, "f1")
        assert fn is not None
        assert fn["code"] == "def invoke(**kw): return {'ok': True}\n"
        inst = get_orchestrator_instruction(target, "i1")
        assert inst is not None
        assert inst["name"] == "Inst"

    def test_export_nonexistent_folder(self, isolated_data_dir):
        from core.orchestrator_folders import export_orchestrator_folder
        assert export_orchestrator_folder("nope_not_here") is None


# ── Test bootstrap (Orchestrator Creator instruction) ─────────────────────────

class TestBootstrapInstructions:
    def test_orchestrator_creator_instruction_id_exists(self):
        from core.bootstrap import ORCHESTRATOR_CREATOR_INSTRUCTION_ID
        assert ORCHESTRATOR_CREATOR_INSTRUCTION_ID == "orchestrator_creator"

    def test_employee_creator_prompt_is_long_enough(self):
        import os
        import core.defaults as defaults_mod
        from core.orchestrators import DEVAGENT_SLUG

        path = os.path.join(
            defaults_mod.orchestrators_dir(), DEVAGENT_SLUG, "instructions", "employee_creator.md"
        )
        with open(path, "r", encoding="utf-8") as f:
            _meta, body = defaults_mod.parse_front_matter(f.read(), default_id="employee_creator")
        assert len(body) > 500
        assert "Employee Creator" in body
        assert "create_orchestrator" in body


# ── Test UniversalDevAgent orchestrator tools ──────────────────────────────

class TestUniversalAgentOrchestratorTools:
    def test_orchestrator_tools_in_catalog(self, isolated_data_dir):
        from dev_agent.universal_agent import WORKSPACE_TOOL_CATALOG
        tool_names = {t["name"] for t in WORKSPACE_TOOL_CATALOG}
        expected = {
            "list_orchestrators", "get_orchestrator", "create_orchestrator",
            "update_orchestrator", "delete_orchestrator", "reload_orchestrator",
            "list_orchestrator_functions", "get_orchestrator_function",
            "save_orchestrator_function", "delete_orchestrator_function",
            "list_orchestrator_instructions", "get_orchestrator_instruction",
            "save_orchestrator_instruction", "delete_orchestrator_instruction",
        }
        assert expected <= tool_names, f"Missing: {expected - tool_names}"

    def test_dispatcher_has_orchestrator_methods(self, isolated_data_dir):
        from dev_agent.universal_agent import UniversalDevAgent
        agent = UniversalDevAgent()
        extra_keys = set(agent._extra.keys())
        assert "list_orchestrators" in extra_keys
        assert "create_orchestrator" in extra_keys
        assert "reload_orchestrator" in extra_keys

    def test_dispatch_update_accepts_sort_order(self, isolated_data_dir):
        from dev_agent.universal_agent import UniversalDevAgent
        agent = UniversalDevAgent()
        created = agent.dispatch("create_orchestrator", {"slug": "svo", "name": "Sort"})
        assert created["ok"] is True, created
        res = agent.dispatch("update_orchestrator", {"slug": "svo", "sort_order": 50})
        assert res["ok"] is True, res
        assert res["fields"] == ["sort_order"]
        agent.dispatch("delete_orchestrator", {"slug": "svo"})

    def test_attach_orchestrator_loads_functions(self, isolated_data_dir):
        from dev_agent.universal_agent import UniversalDevAgent
        from core.orchestrator_folders import save_orchestrator_function
        slug = "attached"
        code = "def invoke(**kw): return {'ok': True, 'custom': 42}\n"
        save_orchestrator_function(slug, "custom_tool", code)

        agent = UniversalDevAgent()
        agent.attach_orchestrator(slug)
        result = agent.dispatch("custom_tool", {})
        assert result == {"ok": True, "custom": 42}


# ── Test orchestrator CRUD with folders (via core.orchestrators) ──────────

class TestOrchestratorCRUDWithFolders:
    def test_create_orchestrator_creates_folder(self, isolated_data_dir):
        from core.orchestrators import create_orchestrator, get_orchestrator_by_slug, delete_orchestrator
        from core.orchestrator_folders import orchestrator_folder_exists
        slug = "crud_folder_test"
        created = create_orchestrator(slug, "CRUD Test", "A test")
        assert created is not None
        # The orchestrator row exists
        orch = get_orchestrator_by_slug(slug)
        assert orch is not None
        assert orch["name"] == "CRUD Test"
        # The folder should be created
        assert orchestrator_folder_exists(slug)
        # Cleanup
        delete_orchestrator(slug)
        assert not orchestrator_folder_exists(slug)

    def test_orch_list_functions_integration(self, isolated_data_dir):
        from core.orchestrators import create_orchestrator, orch_save_function, orch_list_functions, delete_orchestrator
        slug = "func_integration"
        create_orchestrator(slug, "Func Test")
        assert orch_save_function(slug, "test_fn", "def invoke(**kw): return {'ok':True}\n")
        funcs = orch_list_functions(slug)
        assert len(funcs) == 1
        assert funcs[0]["name"] == "test_fn"
        delete_orchestrator(slug)

    def test_orch_list_instructions_integration(self, isolated_data_dir):
        from core.orchestrators import create_orchestrator, orch_save_instruction, orch_list_instructions, delete_orchestrator
        slug = "instr_integration"
        create_orchestrator(slug, "Instr Test")
        assert orch_save_instruction(slug, "i1", "Name", "D", "P")
        lst = orch_list_instructions(slug)
        assert len(lst) == 1
        delete_orchestrator(slug)

    def test_export_includes_functions(self, isolated_data_dir):
        from core.orchestrators import create_orchestrator, orch_save_function, export_orchestrator, delete_orchestrator
        slug = "export_with_fn"
        create_orchestrator(slug, "Export Fn Test")
        orch_save_function(slug, "hello", "def invoke(**kw): return {'greeting': 'hi'}\n")
        data = export_orchestrator(slug)
        assert data is not None
        assert "functions" in data
        assert data["functions"]["hello"] == "def invoke(**kw): return {'greeting': 'hi'}\n"
        delete_orchestrator(slug)

    def test_import_includes_functions(self, isolated_data_dir):
        from core.orchestrators import import_orchestrator, delete_orchestrator
        from core.orchestrator_folders import get_orchestrator_function
        data = {
            "format": "sagaai_orchestrator/v1",
            "slug": "imp_fun",
            "name": "Import Func",
            "prompt_text": "prompt",
            "functions": {
                "my_fn": "def invoke(**kw): return {'ok':True}\n"
            },
            "instructions": [
                {"id": "inst_x", "name": "MyInst", "description": "", "prompt_text": "text"}
            ],
        }
        result = import_orchestrator(data)
        assert result["ok"] is True, result.get("error")
        assert result["functions_imported"] == 1
        fn = get_orchestrator_function("imp_fun", "my_fn")
        assert fn is not None
        delete_orchestrator("imp_fun")


# ── New slug safety & lifecycle coverage ─────────────────────────────────

class TestSlugSafety:
    def test_safe_slug_mapping(self):
        from core.orchestrator_folders import safe_orchestrator_slug
        assert safe_orchestrator_slug("My Bot") == "my_bot"
        assert safe_orchestrator_slug("../../evil") == "evil"
        assert safe_orchestrator_slug("a--b..c//d") == "a_b_c_d"
        assert safe_orchestrator_slug("  Upside-DOWN  ") == "upside_down"
        assert safe_orchestrator_slug("") == ""
        assert safe_orchestrator_slug(None) == ""

    def test_dir_never_escapes_root(self, isolated_data_dir):
        from core.orchestrator_folders import get_orchestrator_dir, get_orchestrators_root
        root = get_orchestrators_root()
        evil_dir = get_orchestrator_dir("../evil")
        assert evil_dir.startswith(root + os.sep)
        assert ".." not in os.path.relpath(evil_dir, root).split(os.sep)
        assert get_orchestrator_dir("a/b\c") == os.path.join(root, "a_b_c")

    def test_empty_slug_falls_back_to_unnamed(self, isolated_data_dir):
        from core.orchestrator_folders import get_orchestrator_dir, get_orchestrators_root
        assert get_orchestrator_dir("!!!") == os.path.join(get_orchestrators_root(), "unnamed")


class TestOrchestratorLifecycleGuards:
    def test_create_normalizes_and_rejects_duplicates(self, isolated_data_dir):
        from core.orchestrators import create_orchestrator, delete_orchestrator
        from core.orchestrator_folders import orchestrator_folder_exists
        assert create_orchestrator("My Bot", "My Bot")
        assert orchestrator_folder_exists("my_bot")
        assert create_orchestrator("my_bot", "dup") is None
        assert create_orchestrator("!!!", "bad") is None
        delete_orchestrator("my_bot")

    def test_import_rejects_path_traversal_slug(self, isolated_data_dir):
        from core.orchestrators import import_orchestrator
        data = {"format": "sagaai_orchestrator/v1", "slug": "../evil", "name": "Evil"}
        res = import_orchestrator(data)
        assert res["ok"] is False

    def test_import_uses_normalized_slug(self, isolated_data_dir):
        from core.orchestrators import import_orchestrator, delete_orchestrator, get_orchestrator_by_slug
        data = {"format": "sagaai_orchestrator/v1", "slug": "MyBot", "name": "My Bot"}
        res = import_orchestrator(data)
        assert res["ok"] is True, res.get("error")
        assert res["slug"] == "mybot"
        assert get_orchestrator_by_slug("mybot") is not None
        delete_orchestrator(res["slug"])

    def test_instruction_save_returns_effective_id(self, isolated_data_dir):
        from core.orchestrators import create_orchestrator, orch_save_instruction, delete_orchestrator, orch_get_instruction
        create_orchestrator("id_test", "Id Test")
        rid = orch_save_instruction("id_test", "", "Auto", "d", "p")
        assert isinstance(rid, str) and len(rid) == 8
        assert orch_get_instruction("id_test", rid) is not None
        fixed = orch_save_instruction("id_test", "fixed_id", "Fixed", "d", "p")
        assert fixed == "fixed_id"
        delete_orchestrator("id_test")

    def test_delete_purges_instruction_cache(self, isolated_data_dir):
        from core.orchestrators import (create_orchestrator, orch_save_instruction,
                                        delete_orchestrator)
        from storage.repository import repo_list_orchestrator_instructions
        create_orchestrator("cache_test", "Cache Test")
        orch_save_instruction("cache_test", "i1", "I", "d", "p")
        assert len(repo_list_orchestrator_instructions("cache_test")) == 1
        assert delete_orchestrator("cache_test")
        assert len(repo_list_orchestrator_instructions("cache_test")) == 0

    def test_reload_from_folder_roundtrip(self, isolated_data_dir):
        from core.orchestrators import (create_orchestrator, reload_orchestrator_from_folder,
                                        delete_orchestrator)
        from core.orchestrator_folders import save_orchestrator_bundle
        create_orchestrator("reload_t", "Old Name")
        save_orchestrator_bundle("reload_t", {"slug": "reload_t", "name": "New Name",
                                             "description": "rel", "prompt_text": "New prompt"})
        res = reload_orchestrator_from_folder("reload_t")
        assert res["ok"] is True and res["action"] == "updated"
        from core.orchestrators import get_orchestrator
        orch = get_orchestrator("reload_t")
        assert orch["name"] == "New Name"
        assert orch["prompt_text"] == "New prompt"
        delete_orchestrator("reload_t")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
