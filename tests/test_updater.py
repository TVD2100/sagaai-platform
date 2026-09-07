# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""tests/test_updater.py - unit tests for the core.updater pipeline.

All tests run fully offline: the remote manifest and raw files are served
from tmp_path via a local HTTP server.
"""

import contextlib
import hashlib
import http.server
import json
import os
import subprocess
import threading

import pytest

import core.updater as updater


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_manifest() -> dict:
    """Build a synthetic manifest with one unit (two files) and one
    selectable file, plus metadata."""
    return {
        "schema": 1,
        "app_version": "1.2.3",
        "updated": "2026-01-01T00:00:00Z",
        "channel": "https://channel.test/base",
        "units": {
            "core": {
                "version": "1.0.0",
                "note": {"ru": "core", "en": "core"},
                "files": {
                    "core/a.py": {"version": "1.2.0", "sha256": _sha(b"core-a-new")},
                    "app.py": {"version": "1.2.0", "sha256": _sha(b"app-new")},
                },
            },
            "storage": {
                "version": "1.0.0",
                "note": {"ru": "storage", "en": "storage"},
                "files": {
                    "storage/db.py": {
                        "version": "2.0.0",
                        "sha256": _sha(b"db-new"),
                    }
                },
            },
        },
        "selectable": {
            "defaults/prompt.md": {"version": "3.7", "sha256": _sha(b"prompt-new")},
            "assets/logo.svg": {"version": "1.0.0", "sha256": _sha(b"logo-new")},
        },
    }


def _write(root, rel, data: bytes):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _read_bytes(root, rel) -> bytes:
    with open(os.path.join(root, rel), "rb") as f:
        return f.read()


def _read_text(root, rel) -> str:
    with open(os.path.join(root, rel), "r", encoding="utf-8") as f:
        return f.read()


@contextlib.contextmanager
def _local_channel(tmp_path, manifest, files):
    """Serve manifest + payload files over a local HTTP server; yield the
    channel URL (http://127.0.0.1:<port>)."""
    served = tmp_path / "served"
    os.makedirs(served, exist_ok=True)
    (served / "file_versions.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    for rel, data in files.items():
        path = served / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=str(served), **kwargs
    )
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_check_reports_new_and_updated_files(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "__version__", "1.0.0")
    root = str(tmp_path)
    manifest = _make_manifest()
    # same content -> up to date; different -> update; missing -> new
    _write(root, "core/a.py", b"core-a-new")
    _write(root, "app.py", b"app-old")
    _write(root, "storage/db.py", b"db-new")
    _write(root, "defaults/prompt.md", b"prompt-new")

    report = updater.check_updates(root, manifest=manifest, channel="https://channel.test/base")

    assert report["ok"]
    assert report["app_version"] == {
        "local": "1.0.0",
        "remote": "1.2.3",
        "newer_remote": True,
    }
    available = {item["path"]: item for item in report["available"]}
    assert set(available) == {"app.py", "assets/logo.svg"}
    assert available["app.py"]["action"] == "update"
    assert available["assets/logo.svg"]["action"] == "new"
    assert report["up_to_date"] is False
    assert report["source"] == "https://channel.test/base"


def test_check_uses_manifest_declared_channel_for_default(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "__version__", "1.0.0")
    manifest = _make_manifest()
    report = updater.check_updates(str(tmp_path), manifest=manifest)
    assert report["source"] == "https://channel.test/base"


def test_check_marks_out_of_scope_local_files_up_to_date(tmp_path):
    manifest = _make_manifest()
    _write(tmp_path, "docs/help.md", b"local doc")
    report = updater.check_updates(str(tmp_path), manifest=manifest)
    assert all(item["path"] != "docs/help.md" for item in report["available"])


def test_check_invalid_manifest(tmp_path):
    report = updater.check_updates(str(tmp_path), manifest={"units": []})
    assert not report["ok"]
    assert "manifest must contain" in report["error"]


def test_check_bad_entry_missing_sha(tmp_path):
    bad = _make_manifest()
    bad["selectable"]["defaults/prompt.md"].pop("sha256")
    report = updater.check_updates(str(tmp_path), manifest=bad)
    assert not report["ok"]
    assert "bad selectable entry" in report["error"]


def test_check_app_version_not_newer_remote(tmp_path):
    local = _make_manifest()
    local.pop("app_version", None)
    _write(tmp_path, "app.py", b"old")
    report = updater.check_updates(str(tmp_path), manifest=local)
    assert report["app_version"]["remote"] is None
    assert not report["app_version"]["newer_remote"]


def test_compare_versions():
    assert updater._compare_versions("1.0.0", "1.0.1") == -1
    assert updater._compare_versions("2.0.0", "1.9.9") == 1
    assert updater._compare_versions("1.0.0", "1.0.0") == 0
    assert updater._compare_versions("1.0.0-rc.1", "1.0.0") == -1
    assert updater._compare_versions("1.0.0", "1.0.0-rc.1") == 1
    assert updater._compare_versions("bad", "1.0.0") is None


def test_stage_merges_into_pending_json(tmp_path):
    manifest = _make_manifest()
    root = str(tmp_path)
    _write(root, "core/a.py", b"core-a-new")
    _write(root, "app.py", b"app-old")
    _write(root, "storage/db.py", b"db-new")
    _write(root, "defaults/prompt.md", b"prompt-new")
    payloads = {"app.py": b"app-new", "assets/logo.svg": b"logo-new"}

    # first stage: only the new asset + changed file, then a second stage
    # with default selection (= remaining available) merged into pending.
    with _local_channel(tmp_path, manifest, payloads) as channel:
        report = updater.stage_updates(
            root,
            selection=["assets/logo.svg", "app.py"],
            manifest=manifest,
            channel=channel,
        )
        assert report["ok"], report
        assert sorted(report["staged"]) == ["app.py", "assets/logo.svg"]
        report = updater.stage_updates(root, manifest=manifest, channel=channel)
        assert report["ok"], report
    pending = json.loads(_read_text(root, ".dev_agent/updates/pending.json"))
    assert set(pending["files"]) == {"app.py", "assets/logo.svg"}
    assert set(pending["selected"]) == {"app.py", "assets/logo.svg"}
    assert _read_bytes(root, ".dev_agent/updates/pending/app.py") == b"app-new"
    assert _read_bytes(root, ".dev_agent/updates/pending/assets/logo.svg") == b"logo-new"


def test_stage_selection_not_in_manifest_fails(tmp_path):
    root = str(tmp_path)
    report = updater.stage_updates(
        root,
        selection=["missing.txt"],
        manifest=_make_manifest(),
        channel="https://channel.test/base",
    )
    assert not report["ok"]
    assert "missing.txt is not in the manifest" in report["error"]


def test_stage_sha256_mismatch_fails(tmp_path):
    root = str(tmp_path)
    manifest = _make_manifest()
    manifest["selectable"]["defaults/prompt.md"]["sha256"] = _sha(b"other")
    _write(root, "defaults/prompt.md", b"local")
    payloads = {"defaults/prompt.md": b"evil-prompt"}
    with _local_channel(tmp_path, manifest, payloads) as channel:
        report = updater.stage_updates(
            root,
            selection=["defaults/prompt.md"],
            manifest=manifest,
            channel=channel,
        )
    assert not report["ok"]
    assert "defaults/prompt.md" in report["error"]
    # pending store must not contain the bad file
    assert not os.path.exists(
        os.path.join(root, ".dev_agent/updates/pending/defaults/prompt.md")
    )


def test_stage_download_success_via_local_http(tmp_path):
    """End-to-end stage over a local HTTP server serving manifest + files."""
    root = str(tmp_path)
    served = tmp_path / "served"
    os.makedirs(served, exist_ok=True)
    manifest = _make_manifest()
    (served / "file_versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    (served / "assets").mkdir(exist_ok=True)
    (served / "assets" / "logo.svg").write_bytes(b"logo-new")
    (served / "defaults").mkdir(exist_ok=True)
    (served / "defaults" / "prompt.md").write_bytes(b"prompt-new")

    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=str(served), **kwargs
    )
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        channel = "http://127.0.0.1:%d" % port
        report = updater.stage_updates(
            root,
            selection=["assets/logo.svg", "defaults/prompt.md"],
            channel=channel,
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)

    assert report["ok"], report
    assert sorted(report["staged"]) == ["assets/logo.svg", "defaults/prompt.md"]
    assert (
        _read_bytes(root, ".dev_agent/updates/pending/assets/logo.svg")
        == b"logo-new"
    )
    assert (
        _read_bytes(root, ".dev_agent/updates/pending/defaults/prompt.md")
        == b"prompt-new"
    )


def test_apply_refuses_when_app_running(tmp_path):
    root = str(tmp_path)
    updater._atomic_write_text(
        root,
        updater.RUNNING_FILE_REL,
        json.dumps({"pid": os.getpid(), "at": "2026-01-01T00:00:00Z"}),
    )
    report = updater.apply_updates(root)
    assert not report["ok"]
    assert "app is running" in report["error"]

    assert updater.apply_updates(root, force=True)["ok"] is True


def test_apply_without_pending_is_healthy(tmp_path):
    report = updater.apply_updates(str(tmp_path))
    assert report["ok"]
    assert report["applied"] == []


def test_apply_applies_staged_files(tmp_path):
    root = str(tmp_path)
    _write(root, "app.py", b"app-old")
    manifest = _make_manifest()
    payloads = {"app.py": b"app-new", "assets/logo.svg": b"logo-new"}
    with _local_channel(tmp_path, manifest, payloads) as channel:
        report = updater.stage_updates(
            root,
            selection=["app.py", "assets/logo.svg"],
            manifest=manifest,
            channel=channel,
        )
    assert report["ok"], report
    applied = updater.apply_updates(root)
    assert applied["ok"], applied
    assert applied["applied"] == ["app.py", "assets/logo.svg"]
    assert _read_bytes(root, "app.py") == b"app-new"
    assert _read_bytes(root, "assets/logo.svg") == b"logo-new"
    # pending manifest consumed
    assert not os.path.exists(os.path.join(root, ".dev_agent/updates/pending.json"))


def test_running_marker_detection(tmp_path):
    root = str(tmp_path)
    assert updater.is_app_running(root) is False
    # stale pid (nonexistent process) -> not running
    updater._atomic_write_text(
        root,
        updater.RUNNING_FILE_REL,
        json.dumps({"pid": 999999999, "at": "2026-01-01T00:00:00Z"}),
    )
    assert updater.is_app_running(root) is False
    # live pid -> running
    updater._atomic_write_text(
        root,
        updater.RUNNING_FILE_REL,
        json.dumps({"pid": os.getpid(), "at": "2026-01-01T00:00:00Z"}),
    )
    assert updater.is_app_running(root) is True


def test_stage_does_not_download_unlisted_file(tmp_path):
    """Selection default never includes unlisted files."""
    root = str(tmp_path)
    _write(root, "core/a.py", b"core-a-new")
    _write(root, "app.py", b"app-old")
    _write(root, "storage/db.py", b"db-new")
    _write(root, "defaults/prompt.md", b"prompt-new")
    manifest = _make_manifest()
    payloads = {"app.py": b"app-new", "assets/logo.svg": b"logo-new"}
    with _local_channel(tmp_path, manifest, payloads) as channel:
        report = updater.stage_updates(root, manifest=manifest, channel=channel)
    assert report["ok"], report
    assert set(report["staged"]) == {"app.py", "assets/logo.svg"}


def test_rollback_updates_no_store(tmp_path):
    report = updater.rollback_updates(str(tmp_path))
    assert not report["ok"]
    assert "no backup store" in report["error"]
def test_write_running_marker(tmp_path):
    root = str(tmp_path)
    updater.write_running_marker(root)
    marker = json.loads(_read_text(root, ".dev_agent/running.json"))
    assert marker["pid"] == os.getpid()
    assert "at" in marker
    assert updater.is_app_running(root) is True


def test_app_py_has_cold_start_hook():
    """Step 5 guard: app.py must call apply_updates + write_running_marker
    before importing streamlit."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = _read_text(project_root, "app.py")
    assert "apply_updates(_project_root)" in src
    assert "write_running_marker(_project_root)" in src
    assert src.index("apply_updates(_project_root)") < src.index("import streamlit as st")


def test_cold_start_hook_applies_then_marks(tmp_path):
    """Integration: stage a file, then run the app.py cold-start order
    (apply -> mark) in a subprocess and verify the file landed."""
    import sys

    root = str(tmp_path)
    _write(root, "target.json", b"old")
    manifest = _make_manifest()
    manifest["units"]["core"]["files"] = {
        "target.json": {"version": "1.0.0", "sha256": _sha(b"new")}
    }
    manifest["selectable"] = {}
    payloads = {"target.json": b"new"}
    with _local_channel(tmp_path, manifest, payloads) as channel:
        report = updater.stage_updates(
            root, selection=["target.json"], manifest=manifest, channel=channel
        )
    assert report["ok"], report
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from core.updater import apply_updates, write_running_marker; "
        "r = apply_updates(%r); write_running_marker(%r); "
        "print('OK' if r['ok'] else r)" % (project_root, root, root)
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert _read_bytes(root, "target.json") == b"new"
    marker = json.loads(_read_text(root, ".dev_agent/running.json"))
    assert isinstance(marker["pid"], int)