"""
Scenario tests for the canonical PROJECT_MAP regeneration pipeline
(scripts/regenerate_project_map.py + dev_agent.workspace_tools):

  given  a repository with a file_versions.json manifest,
  when   the map is regenerated from the manifest scope,
  then   only published files appear in the table, methods and classes are
         visible, the header records a content fingerprint, the fingerprint
         detects later staleness, and a second regeneration without changes
         reproduces the same map (the original bug: zero-indent symbol scan
         kept class methods out of the map).
"""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(os.path.dirname(HERE))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent import config as dev_config
from dev_agent import workspace_tools as wt

SCRIPT_PATH = Path(PKG_ROOT) / "scripts" / "regenerate_project_map.py"


def _load_script():
    """Import the regeneration script as a module (import must not write)."""
    spec = importlib.util.spec_from_file_location("regen_project_map_scenario", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
def repo_ws(tmp_path):
    """A miniature published repository: manifest + sources + local files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def run():\n"
        "    return helper.value()\n\n\n"
        "class Runner:\n"
        "    def go(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    (repo / "helper.py").write_text("def value():\n    return 42\n", encoding="utf-8")
    (repo / "LICENSE").write_text("MIT\n", encoding="utf-8")
    # Local-only content that must stay OUT of the map.
    (repo / "local_notes.txt").write_text("secret local notes\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("# local changelog\n", encoding="utf-8")
    manifest = {
        "schema": 1,
        "app_version": "9.9.9",
        "channel": "https://example.invalid/repo",
        "release_note": {"ru": "r", "en": "e"},
        "updated": "2026-01-01T00:00:00Z",
        "units": {
            "core": {
                "version": "1.0.0",
                "note": {},
                "files": {
                    "app.py": {"version": "1.0.0", "sha256": "0" * 64},
                    "helper.py": {"version": "1.0.0", "sha256": "1" * 64},
                    "LICENSE": {"version": "1.0.0", "sha256": "2" * 64},
                },
            }
        },
        "selectable": {
            "README.md": {"version": "1.0.0", "sha256": "3" * 64},
        },
    }
    (repo / "file_versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    res = wt.set_workspace(str(repo))
    assert res["ok"]
    yield repo
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


def _table_paths(md):
    paths = []
    for ln in md.split("\n"):
        if ln.startswith("| `"):
            cell = ln.split(" | ")[0]
            if cell.endswith("`"):
                paths.append(cell[3:-1])
    return paths


def _recorded_fingerprint(md):
    for ln in md.split("\n"):
        marker = "- Отпечаток содержимого: `sha256:"
        if ln.startswith(marker):
            return ln[len(marker):].split("`", 1)[0]
    return ""


def test_publish_set_from_manifest_and_malformed_tolerance(repo_ws):
    mod = _load_script()
    manifest = json.loads((repo_ws / "file_versions.json").read_text(encoding="utf-8"))
    pub = mod.build_publish_set(manifest)
    assert pub["paths"] == ["LICENSE", "README.md", "app.py", "helper.py"]
    assert pub["count"] == 4
    assert mod.build_publish_set({"units": {"x": None}, "selectable": "bad"}) == {
        "paths": [], "count": 0,
    }


def test_regeneration_scopes_table_to_published_files(repo_ws):
    mod = _load_script()
    before = hashlib.sha256((repo_ws / "PROJECT_MAP.md").read_bytes()).hexdigest() if (repo_ws / "PROJECT_MAP.md").exists() else ""
    res = mod.main()
    assert res["ok"], res
    md = (repo_ws / "PROJECT_MAP.md").read_text(encoding="utf-8")
    rows = _table_paths(md)
    # Published sources are present; local files and unlisted content are not.
    assert "app.py" in rows and "helper.py" in rows
    assert "local_notes.txt" not in rows and "CHANGELOG.md" not in rows
    assert "local_notes.txt" not in md and "secret local notes" not in md
    # The header records the inclusion policy and a fingerprint.
    assert "- Состав: " in md and "file_versions.json" in md
    assert _recorded_fingerprint(md)
    # Extensionless published files appear in the extra section.
    assert "## Прочие публикуемые файлы (вне текстового скана)" in md
    assert "- `LICENSE`" in md


def test_methods_visible_and_fingerprint_detects_staleness(repo_ws):
    mod = _load_script()
    assert mod.main()["ok"]
    md = (repo_ws / "PROJECT_MAP.md").read_text(encoding="utf-8")
    # The original bug: class methods were invisible to the map.
    assert "- `Runner.go` (func, строка" in md
    assert "- `run` (func, строка" in md

    recorded = _recorded_fingerprint(md)
    assert recorded
    pub = mod.build_publish_set(
        json.loads((repo_ws / "file_versions.json").read_text(encoding="utf-8"))
    )
    live = wt.build_project_map(include_paths=pub["paths"])["fingerprint"]
    assert live == recorded  # freshly generated map is fresh

    (repo_ws / "app.py").write_text(
        "def run():\n    return helper.value() + 1\n", encoding="utf-8"
    )
    changed = wt.build_project_map(include_paths=pub["paths"])["fingerprint"]
    assert changed != recorded  # staleness is detectable


def test_regeneration_is_reproducible(repo_ws):
    mod = _load_script()
    assert mod.main()["ok"]
    md1 = (repo_ws / "PROJECT_MAP.md").read_text(encoding="utf-8")
    assert mod.main()["ok"]
    md2 = (repo_ws / "PROJECT_MAP.md").read_text(encoding="utf-8")

    def _stable(md):
        return [ln for ln in md.split("\n") if not ln.startswith("- Обновлено:")]

    assert _stable(md1) == _stable(md2)
    assert _recorded_fingerprint(md1) == _recorded_fingerprint(md2)
    assert _table_paths(md1) == _table_paths(md2)
