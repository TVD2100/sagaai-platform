"""
Headless tests for the Universal Developer layer:
  - dev_agent.workspace_tools (set_workspace, set_target_file, scan, assess, map, docs, snapshots)
  - dev_agent.universal_agent (UniversalDevAgent dispatch routing + prompt)

These never touch the SagaAI install: every test points the workspace at a
fresh temporary folder, exercises the deterministic tools and the unified
dispatcher, then lets the OS clean up the tmpdir.
"""
import os
import sys
import importlib

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent import config as dev_config
from dev_agent import workspace_tools as wt
from dev_agent.universal_agent import UniversalDevAgent


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own temporary SQLite database.

    Prevents workspace-history pollution of the real sagaai.db when tests
    call set_workspace() with temporary folders.
    """
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    import importlib
    import storage.db as db_mod
    db_mod.reset_engine()
    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)
    yield
    db_mod.reset_engine()


@pytest.fixture
def empty_ws(tmp_path):
    """A fresh empty target folder, selected as the active workspace."""
    folder = tmp_path / "proj"
    res = wt.set_workspace(str(folder))
    assert res["ok"]
    assert res["working_on_install"] is False
    yield folder
    # Restore the workspace back to the install so other tests are unaffected.
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


@pytest.fixture
def code_ws(tmp_path):
    """A folder seeded with a tiny two-module Python project."""
    folder = tmp_path / "app"
    folder.mkdir(parents=True)
    (folder / "app.py").write_text(
        "import helper\n\n\ndef main():\n    return helper.run()\n", encoding="utf-8"
    )
    (folder / "helper.py").write_text(
        "def run():\n    return 42\n", encoding="utf-8"
    )
    res = wt.set_workspace(str(folder))
    assert res["ok"]
    yield folder
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


# ── set_workspace / config repointing ─────────────────────────────────────────

def test_set_workspace_creates_and_repoints(tmp_path):
    folder = tmp_path / "new_target"
    res = wt.set_workspace(str(folder))
    assert res["ok"]
    assert folder.exists()
    assert dev_config.PROJECT_ROOT.resolve() == folder.resolve()
    assert dev_config.WORKING_ON_INSTALL is False
    # Runtime data is sandboxed inside the target.
    assert ".dev_agent" in str(dev_config.BACKUPS_DIR)
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


def test_set_workspace_empty_path_rejected():
    res = wt.set_workspace("")
    assert res["ok"] is False


def test_external_workspace_has_no_protected_files(empty_ws):
    # On an external target, nothing is protected (the SagaAI core is elsewhere).
    assert dev_config.PROTECTED_FILES == ()
    assert dev_config.is_protected("anything.py") is False


# ── set_target_file / single-file mode ─────────────────────────────────────────

def test_set_target_file_activates_single_file_mode(code_ws):
    """Setting a target file should put the system in single-file mode."""
    target = str(code_ws / "app.py")
    res = wt.set_target_file(target)
    assert res["ok"]
    assert res["single_file_mode"] is True
    assert res["target_file"] == target
    # Workspace should be the parent dir
    assert dev_config.PROJECT_ROOT.resolve() == code_ws.resolve()


def test_set_target_file_narrows_scan_to_one_file(code_ws):
    """In single-file mode, scan_folder returns only the target file."""
    wt.set_target_file(str(code_ws / "app.py"))
    scan = wt.scan_folder()
    assert scan["ok"]
    assert scan["code_files"] == 1
    assert scan["total_files"] == 1
    assert scan["single_file_mode"] is True
    wt.set_workspace(str(code_ws))  # reset


def test_set_target_file_assess_returns_single_file_state(code_ws):
    """assess_workspace returns 'single_file' when in single-file mode."""
    wt.set_target_file(str(code_ws / "app.py"))
    res = wt.assess_workspace()
    assert res["ok"]
    assert res["state"] == "single_file"
    assert res["single_file_mode"] is True
    wt.set_workspace(str(code_ws))  # reset


def test_set_target_file_current_workspace_reports_mode(code_ws):
    """current_workspace reports single_file_mode and target_file."""
    wt.set_target_file(str(code_ws / "helper.py"))
    cur = wt.current_workspace()
    assert cur["ok"]
    assert cur["single_file_mode"] is True
    assert cur["target_file"] == str(code_ws / "helper.py")
    wt.set_workspace(str(code_ws))


def test_set_target_file_rejects_directory(code_ws):
    """Passing a directory to set_target_file should error."""
    res = wt.set_target_file(str(code_ws))
    assert res["ok"] is False


def test_set_target_file_rejects_missing_file(code_ws):
    """Passing a non-existent file should error."""
    res = wt.set_target_file(str(code_ws / "nonexistent.py"))
    assert res["ok"] is False


def test_set_workspace_clears_single_file_mode(code_ws):
    """Calling set_workspace after set_target_file clears single-file mode."""
    wt.set_target_file(str(code_ws / "app.py"))
    cur = wt.current_workspace()
    assert cur["single_file_mode"] is True
    wt.set_workspace(str(code_ws))
    cur = wt.current_workspace()
    assert cur["single_file_mode"] is False
    assert "target_file" not in cur


def test_single_file_project_map_narrows(code_ws):
    """In single-file mode, project map shows only the target file."""
    wt.set_target_file(str(code_ws / "app.py"))
    pm = wt.build_project_map()
    assert pm["ok"]
    assert pm["file_count"] == 1
    assert pm["entries"][0]["path"] == "app.py"
    wt.set_workspace(str(code_ws))


def test_dispatch_set_target_file(code_ws):
    """set_target_file works through the UniversalDevAgent dispatcher."""
    agent = UniversalDevAgent()
    res = agent.dispatch("set_target_file", {"file_path": str(code_ws / "app.py")})
    assert res["ok"]
    assert res["single_file_mode"] is True
    wt.set_workspace(str(code_ws))


# ── assess_workspace : three states ───────────────────────────────────────────

def test_assess_empty(empty_ws):
    res = wt.assess_workspace()
    assert res["ok"]
    assert res["state"] == "empty"
    assert res["code_files"] == 0


def test_assess_software_without_docs(code_ws):
    res = wt.assess_workspace()
    assert res["ok"]
    assert res["state"] == "software_without_docs"
    assert res["code_files"] == 2
    assert res["languages"].get("Python") == 2


def test_assess_software_with_docs(code_ws):
    agent = UniversalDevAgent()
    agent.dispatch("build_project_map", {})
    agent.dispatch("write_project_map", {"responsibilities": {}})
    agent.dispatch("write_doc", {"doc": "spec"})
    res = wt.assess_workspace()
    assert res["state"] == "software_with_docs"


# ── deterministic project map ─────────────────────────────────────────────────

def test_build_project_map_detects_symbols_and_deps(code_ws):
    pm = wt.build_project_map()
    assert pm["ok"]
    assert pm["file_count"] == 2
    by_path = {e["path"]: e for e in pm["entries"]}
    assert "app.py" in by_path and "helper.py" in by_path
    # app imports helper -> internal dependency captured.
    assert by_path["app.py"]["depends_on"] == ["helper"]
    # symbols extracted with line numbers.
    names = {s["name"] for s in by_path["app.py"]["symbols"]}
    assert "main" in names


def test_render_project_map_markdown_uses_responsibilities(code_ws):
    pm = wt.build_project_map()
    md = wt.render_project_map_markdown(pm, {"app.py": "Entry point"})
    assert "PROJECT_MAP" in md
    assert "Entry point" in md
    assert "`app.py`" in md
    # missing responsibility falls back to a placeholder, not a crash.
    assert "helper.py" in md


# ── docs (write/read) ─────────────────────────────────────────────────────────

def test_write_and_read_docs(code_ws):
    agent = UniversalDevAgent()
    w = agent.dispatch("write_doc", {"doc": "spec", "content": "# Spec\n\nHello.\n"})
    assert w["ok"]
    assert (code_ws / "SPEC.md").exists()
    r = agent.dispatch("read_doc", {"doc": "spec"})
    assert r["ok"] and r["exists"]
    assert "Hello." in r["content"]


def test_write_doc_unknown_kind_errors(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch("write_doc", {"doc": "nope", "content": "x"})
    assert res["ok"] is False


def test_write_project_map_tool_writes_file(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch("write_project_map", {"responsibilities": {"app.py": "Entry"}})
    assert res["ok"]
    assert (code_ws / "PROJECT_MAP.md").exists()
    content = (code_ws / "PROJECT_MAP.md").read_text(encoding="utf-8")
    assert "Entry" in content


# ── snapshots / restore ───────────────────────────────────────────────────────

def test_snapshot_and_restore_all(code_ws):
    agent = UniversalDevAgent()
    snap = agent.dispatch("snapshot_all", {"note": "baseline"})
    assert snap["ok"]
    snap_id = snap["snapshot_id"]
    assert snap["file_count"] >= 2

    listed = agent.dispatch("list_snapshots", {})
    assert listed["ok"]
    assert any(s["id"] == snap_id for s in listed["snapshots"])

    # Mutate a file directly, then restore the whole system.
    (code_ws / "helper.py").write_text("def run():\n    return 999\n", encoding="utf-8")
    res = agent.dispatch("restore_all", {"snapshot_id": snap_id})
    assert res["ok"], res
    restored = (code_ws / "helper.py").read_text(encoding="utf-8")
    assert "42" in restored


def test_restore_all_missing_snapshot(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch("restore_all", {"snapshot_id": "does-not-exist"})
    assert res["ok"] is False


# ── unified dispatch routing ──────────────────────────────────────────────────

def test_dispatch_routes_core_tools(code_ws):
    agent = UniversalDevAgent()
    # A core tool (read_file) flows through to the protected core unchanged.
    res = agent.dispatch("read_file", {"path": "app.py"})
    assert res["ok"]
    assert "import helper" in res["content"]


def test_dispatch_routes_workspace_tools(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch("assess_workspace", {})
    assert res["ok"]
    assert res["state"] == "software_without_docs"


def test_dispatch_unknown_tool_errors(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch("totally_made_up", {})
    assert res.get("ok") is False


def test_dispatch_workspace_tool_rejects_unknown_args(code_ws):
    """Unknown argument names on a workspace tool must produce a structured
    error with unknown_args and a usage suggestion pointing at the
    system-prompt documentation, not a silent kwargs swallow."""
    agent = UniversalDevAgent()
    res = agent.dispatch("search_in_files", {"query": "helper", "bogus": 1})
    assert res.get("ok") is False
    assert res.get("unknown_args") == ["bogus"]
    assert "bogus" in res.get("error", "")
    assert "system prompt" in res.get("error", "")
    assert "Usage:" in res.get("suggestion", "")


def test_dispatch_workspace_tool_known_args_still_ok(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch("search_in_files", {"query": "import helper"})
    assert res.get("ok") is True, res
    assert res.get("match_count", 0) > 0


def test_dispatch_json_compatible(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch_json({"tool": "scan_folder", "args": {}})
    assert res["ok"]
    assert res["code_files"] == 2


def test_set_workspace_via_dispatch_recreates_core(tmp_path):
    agent = UniversalDevAgent()
    folder = tmp_path / "switch_target"
    res = agent.dispatch("set_workspace", {"path": str(folder)})
    assert res["ok"]
    assert dev_config.PROJECT_ROOT.resolve() == folder.resolve()
    # The core agent now writes/reads in the new folder.
    create = agent.dispatch("propose_file", {"path": "x.py", "content": "y = 1\n"})
    assert create.get("ok")
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


def test_set_workspace_preserves_history_cache(tmp_path):
    """set_workspace must NOT wipe the economy-mode history cache.

    Regression: previously _set_workspace replaced self.core with a fresh
    ToolExecutor(), losing the _history cache. In the UI the dispatcher is
    reused across reruns, so get_history_index called after a workspace
    switch would return total=0 despite a long dialogue.
    """
    agent = UniversalDevAgent()
    history = [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Do something"},
    ]
    agent.core.set_history(history)

    folder = tmp_path / "switch_preserves"
    res = agent.dispatch("set_workspace", {"path": str(folder)})
    assert res["ok"]
    # The history cache survives the core recreation.
    idx = agent.dispatch("get_history_index", {})
    assert idx["ok"]
    assert idx["total"] == 3
    assert idx["count"] == 3
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


# ── combined system prompt ────────────────────────────────────────────────────

def test_system_prompt_combines_core_and_universal():
    agent = UniversalDevAgent()
    sp = agent.system_prompt
    assert "DevAgent" in sp
    assert "verify" in sp
    assert "loop_status" in sp
    # tool catalog exposes both core and workspace tools.
    names = {t["name"] for t in agent.tool_catalog}
    assert {"read_file", "set_workspace", "set_target_file", "snapshot_all", "restore_all"} <= names


def test_system_prompt_documents_list_files_max_depth():
    """§6 must document the max_depth argument of list_files."""
    agent = UniversalDevAgent()
    sp = agent.system_prompt
    assert "max_depth" in sp
    assert "max_depth=1" in sp


def test_system_prompt_documents_listing_scenarios():
    """§9.4 must document the list_files vs scan_folder usage scenarios and
    the search_in_files path argument."""
    agent = UniversalDevAgent()
    sp = agent.system_prompt
    assert "list_files" in sp
    assert "scan_folder" in sp
    assert "search_in_files(path=" in sp


def test_catalog_docs_list_files_max_depth():
    """The LLM-facing catalog must mention max_depth for list_files."""
    agent = UniversalDevAgent()
    entry = next(t for t in agent.tool_catalog if t["name"] == "list_files")
    assert "max_depth" in entry["desc"]


# ── tool polish: structured missing-file errors + read_file window info ────────

def test_run_code_missing_path_returns_structured_error(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch("run_code", {"path": "missing_script.py"})
    assert res["ok"] is False
    assert "not found" in res["error"]
    assert "suggestion" in res


def test_run_test_missing_path_returns_structured_error(code_ws):
    agent = UniversalDevAgent()
    res = agent.dispatch("run_test", {"path": "missing_test_file.py"})
    assert res["ok"] is False
    assert "not found" in res["error"]
    assert "suggestion" in res


def test_read_file_window_reports_remaining_and_hint(code_ws):
    (code_ws / "sample.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8"
    )
    agent = UniversalDevAgent()
    res = agent.core.read_file("sample.txt", offset=1, limit=3)
    assert res["ok"]
    assert res["total_lines"] == 10
    assert res["window_lines"] == 3
    assert res["remaining"] == 7
    assert "hint" in res
    assert "10 lines" in res["hint"]
