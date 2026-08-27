"""
Scenario tests for search_in_files walking the workspace-tool public API
like a developer would:
  - happy path: find a known constant across a normal project tree;
  - edge case: search a file outside the default text-extension allow-list;
  - error state: a non-existent subdir returns a structured error.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent import config as dev_config
from dev_agent import workspace_tools as wt


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "demo_project"
    (folder / "src").mkdir(parents=True)
    (folder / "data").mkdir(parents=True)
    (folder / "src" / "main.py").write_text(
        "VERSION = '1.2.3'\n\ndef run():\n    return VERSION\n", encoding="utf-8"
    )
    (folder / "src" / "config.py").write_text(
        "DEFAULT_MODE = 'production'\n", encoding="utf-8"
    )
    (folder / "data" / "export.csv").write_text(
        "key,value\nVERSION,1.2.3\n", encoding="utf-8"
    )
    (folder / "README.md").write_text(
        "# Demo\nCurrent VERSION is 1.2.3.\n", encoding="utf-8"
    )
    res = wt.set_workspace(str(folder))
    assert res["ok"]
    yield folder
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


def test_scenario_happy_path_search_across_project(project):
    """Given a project tree, when the developer searches a known literal,
    then results include every text file containing it."""
    res = wt.search_in_files("VERSION")

    assert res["ok"] is True
    paths = {r["path"]: r["line"] for r in res["results"]}
    assert "src/main.py" in paths
    assert "README.md" in paths
    # default allow-list does NOT include .csv
    assert "data/export.csv" not in paths


def test_scenario_edge_case_explicit_extensions_find_csv(project):
    """Given a .csv file outside the default text extensions, when the
    developer passes extensions=['csv'], then the file is searched."""
    res = wt.search_in_files("VERSION", extensions=["csv"])

    assert res["ok"] is True
    assert len(res["results"]) == 1
    hit = res["results"][0]
    assert hit["path"] == "data/export.csv"
    assert "VERSION" in hit["text"]


def test_scenario_error_state_missing_subdir(project):
    """Given a typo in subdir, when the developer runs the search,
    then the tool returns a structured error instead of crashing."""
    res = wt.search_in_files("VERSION", subdir="does-not-exist")

    assert res["ok"] is False
    assert "Subdir not found" in res["error"]
