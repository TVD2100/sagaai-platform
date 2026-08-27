"""
Regression tests for the list_files core tool, focused on max_depth.

Covers:
  - default max_depth=1: only the FIRST level below base (no recursion)
  - max_depth=2..3: several levels in ONE call (flat files list)
  - each dirs entry carries the files DIRECTLY inside it
  - invalid max_depth values coerce to 1
  - subdir scoping still works
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent import config as dev_config
from dev_agent.tool_executor import ToolExecutor


@pytest.fixture
def ws(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("a", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "b.py").write_text("b", encoding="utf-8")
    (folder / "sub" / "subsub").mkdir()
    (folder / "sub" / "subsub" / "c.md").write_text("c", encoding="utf-8")
    dev_config.set_target_root(str(folder))
    yield folder
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


def _paths(res):
    return {f["path"] for f in res["files"]}


def test_default_depth_one_level_no_recursion(ws):
    ex = ToolExecutor()
    res = ex.list_files("")
    assert res["ok"] is True
    # Only files DIRECTLY inside the root appear.
    assert _paths(res) == {"a.txt"}
    # "sub" is listed as a dir with its direct files attached.
    dirs = {d["path"]: d["files"] for d in res["dirs"]}
    assert set(dirs) == {"sub"}
    assert dirs["sub"] == ["sub/b.py"]
    # "sub/subsub" itself must NOT be listed at depth 1.
    assert all(not d.startswith("sub/subsub") for d in dirs)


def test_max_depth_two_levels_flat_files(ws):
    ex = ToolExecutor()
    res = ex.list_files("", max_depth=2)
    assert res["ok"] is True
    assert res["max_depth"] == 2
    assert _paths(res) == {"a.txt", "sub/b.py"}
    dirs = {d["path"]: d["files"] for d in res["dirs"]}
    assert set(dirs) == {"sub", "sub/subsub"}
    assert dirs["sub"] == ["sub/b.py"]
    assert dirs["sub/subsub"] == ["sub/subsub/c.md"]


def test_max_depth_three_levels_full_tree(ws):
    ex = ToolExecutor()
    res = ex.list_files("", max_depth=3)
    assert res["ok"] is True
    assert _paths(res) == {"a.txt", "sub/b.py", "sub/subsub/c.md"}
    dir_paths = {d["path"] for d in res["dirs"]}
    assert dir_paths == {"sub", "sub/subsub"}


def test_invalid_max_depth_coerces_to_one(ws):
    ex = ToolExecutor()
    for bad in (0, -2, "abc", None):
        res = ex.list_files("", max_depth=bad)
        assert res["ok"] is True
        assert res["max_depth"] == 1
        assert _paths(res) == {"a.txt"}


def test_subdir_scoping_with_max_depth(ws):
    ex = ToolExecutor()
    res = ex.list_files("sub", max_depth=1)
    assert res["ok"] is True
    assert _paths(res) == {"sub/b.py"}
    dir_paths = {d["path"] for d in res["dirs"]}
    assert dir_paths == {"sub/subsub"}
