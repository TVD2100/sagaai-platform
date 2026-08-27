"""Tests for backup_manager and safe_writer, incl. PROTECTED_FILES enforcement."""

import pytest
from pathlib import Path

from dev_agent import config
from dev_agent.backup_manager import BackupManager
from dev_agent.safe_writer import SafeWriter, ProtectedFileError


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect DevAgent's project root + dirs into a temp sandbox."""
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    # Activate protected-files list so that tests for Inviolable Core work.
    monkeypatch.setattr(config, "PROTECTED_FILES", ("universal.py", "dev_agent/dev_agent.py"))
    monkeypatch.setattr(config, "WORKING_ON_INSTALL", True)
    # Ensure runtime dirs are recreated in the sandbox
    for d in (config.BACKUPS_DIR, config.WORKSPACE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # A normal, editable source file.
    target = root / "core" / "skills.py"
    target.write_text("def a():\n    return 1\n", encoding="utf-8")
    return root


def test_backup_creates_versions(sandbox):
    bm = BackupManager()
    f = sandbox / "core" / "skills.py"
    e1 = bm.create_backup(f, note="first")
    f.write_text("def a():\n    return 2\n", encoding="utf-8")
    e2 = bm.create_backup(f, note="second")
    assert e1.version == 1 and e2.version == 2
    hist = bm.history_summary(f)
    assert hist["total_versions"] == 2


def test_restore_backup(sandbox):
    bm = BackupManager()
    f = sandbox / "core" / "skills.py"
    bm.create_backup(f, note="v1 original")
    f.write_text("BROKEN", encoding="utf-8")
    bm.restore_backup(f, version=1)
    assert "def a()" in f.read_text(encoding="utf-8")


def test_protected_file_blocks_write(sandbox):
    sw = SafeWriter()
    # universal.py is protected; staging a full draft must fail.
    res = sw.stage_draft_full("universal.py", "malicious")
    assert not res.ok
    assert any("PROTECTED" in e for e in res.errors)


def test_protected_check_raises(sandbox):
    sw = SafeWriter()
    with pytest.raises(ProtectedFileError):
        sw.check_protected("dev_agent/dev_agent.py")


def test_stage_and_apply_full_rewrite(sandbox):
    sw = SafeWriter()
    # Full-file rewrite is the only edit path now — no ops/patch fragments.
    new_text = "def a():\n    return 42\n"
    draft = sw.stage_draft_full("core/skills.py", new_text)
    assert draft.ok, draft.errors
    assert "return 42" in draft.new_text
    # Source not yet changed.
    assert "return 1" in (sandbox / "core" / "skills.py").read_text(encoding="utf-8")
    # Apply.
    applied = sw.apply_draft("core/skills.py", note="bump return value")
    assert applied.ok
    assert applied.backup_version == 1
    assert "return 42" in (sandbox / "core" / "skills.py").read_text(encoding="utf-8")
    # Changelog written.
    assert config.CHANGELOG_FILE.exists()
    assert "core/skills.py" in config.CHANGELOG_FILE.read_text(encoding="utf-8")


def test_apply_without_draft_fails(sandbox):
    sw = SafeWriter()
    res = sw.apply_draft("core/skills.py")
    assert not res.ok


def test_path_traversal_blocked(sandbox):
    # Attempt to escape the project root.
    assert config.is_protected("../../etc/passwd") is True
