# -*- coding: utf-8 -*-
"""tests/test_verify_manifest.py - targeted tests for scripts/verify_manifest.py.

Covers the update-manifest bootstrap, validation and maintenance workflow:
--init builds file_versions.json from git-tracked files, plain verify stays
clean, --add registers newly added files, --fix-hashes repairs stale sha256
values after content edits, and a bumped app version is detected as an error.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys

import pytest


SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for this test"
)


def _git(cwd, *args):
    p = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=60,
    )
    assert p.returncode == 0, p.stderr
    return p


def _run_verifier(cwd, *args):
    p = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "verify_manifest.py"), *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=120,
    )
    return p.returncode, p.stdout, p.stderr


def _build_repo(tmp_path):
    """Create a minimal git repo resembling the SagaAI layout."""
    def write(rel, content):
        fp = tmp_path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    write("core/version.py", '__version__ = "1.2.3-test.1"\n')
    write("app.py", "print('hello')\n")
    write("core/app_logic.py", "def run():\n    return 1\n")
    write(
        "dev_agent/system_prompt.md",
        "# DevAgent - System Prompt (v7.7)\n\nbody\n",
    )
    write("langs/ru.json", '{"a": "b"}\n')
    write(".gitignore", "__pycache__/\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _assert_hashes(root, entries):
    for rel, entry in entries.items():
        data = (root / rel).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest(), rel


def test_init_and_verify_roundtrip(tmp_path):
    repo = _build_repo(tmp_path)
    rc, out, err = _run_verifier(repo, "--init")
    assert rc == 0, (out, err)
    manifest = json.loads((repo / "file_versions.json").read_text(encoding="utf-8"))

    # schema and app version
    assert manifest["schema"] == 1
    assert manifest["app_version"] == "1.2.3-test.1"
    assert manifest["channel"].startswith("https://")
    assert set(manifest["release_note"]) >= {"ru", "en"}

    # classification: code -> units.core, content -> selectable
    core = manifest["units"]["core"]
    assert core["version"] == "1.0.0"
    assert set(core["files"]) == {"core/app_logic.py", "core/version.py", "app.py"}
    assert core["files"]["app.py"]["version"] == "1.0.0"

    sel = manifest["selectable"]
    assert set(sel) == {"dev_agent/system_prompt.md", "langs/ru.json"}
    # prompt version comes from the file header
    assert sel["dev_agent/system_prompt.md"]["version"] == "7.7"
    assert {"ru", "en"} <= set(sel["langs/ru.json"]["note"])

    # .gitignore and the manifest itself stay out of the file lists
    for section in (core["files"], sel):
        assert ".gitignore" not in section
        assert "file_versions.json" not in section

    _assert_hashes(repo, core["files"])
    _assert_hashes(repo, sel)

    # plain verify stays green
    rc, out, err = _run_verifier(repo)
    assert rc == 0, (out, err)


def test_fix_hashes_repairs_tampered_file(tmp_path):
    repo = _build_repo(tmp_path)
    assert _run_verifier(repo, "--init")[0] == 0

    (repo / "langs/ru.json").write_text('{"changed": true}\n', encoding="utf-8")
    rc, out, err = _run_verifier(repo)
    assert rc == 1
    assert "sha256 mismatch" in (out + err)

    rc, out, err = _run_verifier(repo, "--fix-hashes")
    assert rc == 0, (out, err)
    rc, out, err = _run_verifier(repo)
    assert rc == 0, (out, err)
    manifest = json.loads((repo / "file_versions.json").read_text(encoding="utf-8"))
    assert manifest["selectable"]["langs/ru.json"]["version"] == "1.0.0"


def test_add_new_file_then_verify(tmp_path):
    repo = _build_repo(tmp_path)
    assert _run_verifier(repo, "--init")[0] == 0

    (repo / "langs" / "de.json").write_text('{"x": "y"}\n', encoding="utf-8")
    _git(repo, "add", "langs/de.json")
    _git(repo, "commit", "-q", "-m", "add de")

    # tracked file missing from the manifest -> coverage error
    rc, out, err = _run_verifier(repo)
    assert rc == 1
    assert "tracked file not in manifest" in (out + err)

    rc, out, err = _run_verifier(repo, "--add", "langs/de.json")
    assert rc == 0, (out, err)
    manifest = json.loads((repo / "file_versions.json").read_text(encoding="utf-8"))
    assert "langs/de.json" in manifest["selectable"]

    rc, out, err = _run_verifier(repo)
    assert rc == 0, (out, err)


def test_app_version_change_is_detected(tmp_path):
    repo = _build_repo(tmp_path)
    assert _run_verifier(repo, "--init")[0] == 0

    (repo / "core" / "version.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    rc, out, err = _run_verifier(repo)
    assert rc == 1
    assert "app_version" in (out + err)
