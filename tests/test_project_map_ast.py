"""
Unit tests for the project-map upgrades in dev_agent.workspace_tools:
  - AST-based symbol extraction (classes, methods, async functions, nested
    definitions, qualified names, real line ranges);
  - the legacy regex fallback for files that fail to parse;
  - the per-file symbol cap with the rendered remainder note;
  - build_project_map(include_paths=...) filtering and extra_paths;
  - the content fingerprint (stability, sensitivity, doc exclusion);
  - the enriched write_project_map header and the extra-files section.

These tests never touch the SagaAI install: every case points the workspace
at a fresh temporary folder.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent import config as dev_config
from dev_agent import workspace_tools as wt


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Keep workspace-history writes out of the real sagaai.db."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import storage.db as db_mod
    db_mod.reset_engine()
    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)
    yield
    db_mod.reset_engine()


@pytest.fixture
def map_ws(tmp_path):
    """A tiny project exercising every symbol kind plus fallback/binary files."""
    folder = tmp_path / "proj"
    (folder / "pkg").mkdir(parents=True)
    (folder / "pkg" / "mod.py").write_text(
        "import helper\n\n"
        "class Outer:\n"
        "    field = 1\n\n"
        "    def method(self):\n"
        "        def nested():\n"
        "            return 1\n"
        "        return nested()\n\n"
        "    async def amethod(self):\n"
        "        return 2\n\n"
        "def top():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (folder / "helper.py").write_text("def run():\n    return 42\n", encoding="utf-8")
    (folder / "bad.py").write_text(
        "def broken(:\n  pass\n\n\ndef visible():\n  return 1\n", encoding="utf-8"
    )
    (folder / "data.bin").write_bytes(b"\x00\x01binary")
    (folder / "LICENSE").write_text("MIT\n", encoding="utf-8")
    res = wt.set_workspace(str(folder))
    assert res["ok"]
    yield folder
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


def _entries(pm):
    return {e["path"]: e for e in pm["entries"]}


def test_ast_symbols_capture_methods_async_and_nested(map_ws):
    pm = wt.build_project_map()
    mod = _entries(pm)["pkg/mod.py"]
    syms = {(s["name"], s["kind"]) for s in mod["symbols"]}
    assert ("Outer", "class") in syms
    assert ("Outer.method", "func") in syms
    assert ("Outer.method.nested", "func") in syms
    assert ("Outer.amethod", "async_func") in syms
    assert ("top", "func") in syms
    by_name = {s["name"]: s for s in mod["symbols"]}
    assert by_name["Outer"]["end_line"] > by_name["Outer"]["line"]
    assert by_name["Outer.method"]["end_line"] >= by_name["Outer.method"]["line"]
    assert mod["symbol_count"] == len(mod["symbols"])


def test_syntax_error_file_falls_back_to_regex(map_ws):
    pm = wt.build_project_map()
    names = {s["name"] for s in _entries(pm)["bad.py"]["symbols"]}
    assert {"broken", "visible"} <= names


def test_symbol_cap_and_remainder_note(map_ws):
    many = "\n\n".join(
        "def f%d():\n    pass" % i for i in range(wt.MAX_SYMBOLS_PER_FILE + 25)
    )
    (map_ws / "many.py").write_text(many + "\n", encoding="utf-8")
    pm = wt.build_project_map()
    entry = _entries(pm)["many.py"]
    assert len(entry["symbols"]) == wt.MAX_SYMBOLS_PER_FILE
    assert entry["symbol_count"] == wt.MAX_SYMBOLS_PER_FILE + 25
    md = wt.render_project_map_markdown(pm, {})
    assert "ещё 25 определений не показано" in md
    assert ("лимит %d на файл" % wt.MAX_SYMBOLS_PER_FILE) in md


def test_include_paths_filters_and_reports_extra(map_ws):
    pm = wt.build_project_map(
        include_paths=["pkg/mod.py", "helper.py", "LICENSE", "data.bin"],
        scope_note="published only",
    )
    assert sorted(e["path"] for e in pm["entries"]) == ["helper.py", "pkg/mod.py"]
    assert pm["extra_paths"] == ["LICENSE", "data.bin"]
    assert pm["scope_note"] == "published only"
    assert "bad.py" not in {e["path"] for e in pm["entries"]}
    # Language counts reflect the filtered set, not the whole scan.
    assert pm["languages"] == {"Python": 2}


def test_fingerprint_stable_sensitive_and_doc_excluded(map_ws):
    inc = ["pkg/mod.py", "helper.py"]
    fp1 = wt.build_project_map(include_paths=inc)["fingerprint"]
    fp2 = wt.build_project_map(include_paths=inc)["fingerprint"]
    assert fp1 == fp2
    assert len(fp1) == 64
    (map_ws / "helper.py").write_text("def run():\n    return 43\n", encoding="utf-8")
    fp3 = wt.build_project_map(include_paths=inc)["fingerprint"]
    assert fp3 != fp2
    # Managed docs sit outside the fingerprint: editing one is not staleness.
    (map_ws / "PROJECT_MAP.md").write_text("# Map\n", encoding="utf-8")
    (map_ws / "README.md").write_text("# Readme\n", encoding="utf-8")
    fp4 = wt.build_project_map(include_paths=inc)["fingerprint"]
    assert fp4 == fp3


def test_write_project_map_renders_header_and_scope(map_ws):
    res = wt.write_project_map(
        {"pkg/mod.py": "module"},
        include_paths=["pkg/mod.py", "helper.py", "LICENSE"],
        scope_note="only published files",
    )
    assert res["ok"]
    md = (map_ws / "PROJECT_MAP.md").read_text(encoding="utf-8")
    assert "- Состав: only published files" in md
    assert "- Python-символов: **" in md
    assert "- Отпечаток содержимого: `sha256:" in md
    assert "не правьте его вручную" in md
    assert "## Прочие публикуемые файлы (вне текстового скана)" in md
    assert "- `LICENSE`" in md
    assert "bad.py" not in md
