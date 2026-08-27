"""Tests for the propose_file + verify_file workflow.

These cover the user-facing change: full-file rewrites (and creations)
instead of fragile anchor-based patches, plus a post-apply verification
tool. ``propose_file`` is the single edit mechanism — it both rewrites an
existing file and creates a new one, chosen automatically via the ``is_new``
flag in its result.

Since the default is now ``auto_apply=True``, propose_file immediately
applies the change to disk. Manual-mode staging is tested via
auto_apply=False.
"""

import json
import pytest

from dev_agent import config
from dev_agent.tool_executor import ToolExecutor


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect DevAgent state into a temp sandbox."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(config, "PROTECTED_FILES", ())
    config.ensure_runtime_dirs()
    target = root / "src" / "module.py"
    target.write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    return root


def test_propose_file_auto_applies_full_content(sandbox):
    """propose_file with auto_apply=True (default) immediately writes to disk."""
    te = ToolExecutor()
    new_content = (
        "def add(a, b):\n"
        "    # multi-region change with no anchors\n"
        "    return a + b + 0\n"
        "\n\n"
        "def sub(a, b):\n"
        "    # second region\n"
        "    return a - b\n"
        "\n\n"
        "def mul(a, b):\n"
        "    return a * b\n"
    )
    res = te.propose_file(path="src/module.py", content=new_content)
    assert res["ok"], res
    assert res["path"] == "src/module.py"
    assert res["is_new"] is False
    assert "diff" in res and res["diff"], "expected a unified diff"
    assert res["applied"] is True
    assert res["verified"] is True
    assert res["backup_version"] is not None
    # The file on disk HAS been changed immediately.
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "mul" in on_disk
    assert on_disk == new_content


def test_propose_file_creates_new_file_on_disk(sandbox):
    """propose_file creates a brand-new file on disk immediately."""
    te = ToolExecutor()
    res = te.propose_file(path="src/new_module.py", content="x = 1\n")
    assert res["ok"] is True, res
    assert res["is_new"] is True
    assert res["applied"] is True
    assert res["verified"] is True
    # File exists on disk now.
    assert (sandbox / "src" / "new_module.py").exists()
    assert (sandbox / "src" / "new_module.py").read_text(encoding="utf-8") == "x = 1\n"


def test_apply_edit_after_manual_staging(sandbox):
    """apply_edit reports verified=True after writing a manually-staged draft."""
    te = ToolExecutor()
    new_content = "def add(a, b):\n    return a + b + 100\n"
    # Manual mode: stage only, do not auto-apply.
    stage = te.propose_file(path="src/module.py", content=new_content, auto_apply=False)
    assert stage["ok"]
    assert "applied" not in stage  # not applied yet
    # File on disk should still be the original.
    assert "100" not in (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    # Now apply.
    apply_res = te.apply_edit(path="src/module.py")
    assert apply_res["ok"] is True
    assert apply_res["verified"] is True
    assert apply_res["bytes_written"] > 0
    # And the file on disk now actually contains the new content.
    assert (sandbox / "src" / "module.py").read_text(encoding="utf-8") == new_content


def test_verify_file_finds_expected_and_unexpected(sandbox):
    """verify_file reports missing expected + present unexpected substrings."""
    te = ToolExecutor()
    new_content = (
        "def add(a, b):\n    return a + b + 100\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    te.propose_file(path="src/module.py", content=new_content)
    # file is already applied — no separate apply_edit needed

    # Happy path: present + absent both correct.
    ok = te.verify_file(
        path="src/module.py",
        expected_substrings=["def mul", "+ 100"],
        unexpected_substrings=["return a - b"],  # we removed sub()
    )
    assert ok["ok"] is True
    assert ok["missing_expected"] == []
    assert ok["present_unexpected"] == []

    # Failure path: claim something should be there that isn't.
    fail = te.verify_file(
        path="src/module.py",
        expected_substrings=["def NOT_THERE"],
    )
    assert fail["ok"] is False
    assert fail["missing_expected"] == ["def NOT_THERE"]


def test_verify_file_unknown_path(sandbox):
    te = ToolExecutor()
    res = te.verify_file(path="does/not/exist.py", expected_substrings=["x"])
    assert res["ok"] is False
    assert "not found" in res["error"].lower()


def test_propose_file_in_tool_catalog():
    """propose_file is exposed in the LLM-facing TOOL_CATALOG."""
    from dev_agent.tool_executor import TOOL_CATALOG
    names = {t["name"] for t in TOOL_CATALOG}
    assert "propose_file" in names
    assert "verify_file" in names


def test_dispatch_routes_propose_file(sandbox):
    """ToolExecutor.dispatch routes propose_file by name."""
    te = ToolExecutor()
    res = te.dispatch("propose_file", {
        "path": "src/module.py",
        "content": "def add(a, b):\n    return 999\n",
    })
    assert res["ok"]
    assert "diff" in res
    assert res["applied"] is True
    assert res["verified"] is True
