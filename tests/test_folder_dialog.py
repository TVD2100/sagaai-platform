# -*- coding: utf-8 -*-
"""tests/test_folder_dialog.py - unit tests for the native folder picker.

The GUI child process is never launched here: the tests exercise the
verdict parsing, the graceful fallbacks and the availability probe that
the Streamlit page relies on. A real dialog is verified manually.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import folder_dialog  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a clean availability cache."""
    folder_dialog.reset_availability_cache()
    yield
    folder_dialog.reset_availability_cache()


def test_disabled_env_short_circuits(monkeypatch, tmp_path):
    """SAGAAI_DISABLE_FOLDER_DIALOG=1 forces None and reports unavailable."""
    monkeypatch.setenv(folder_dialog.DISABLE_ENV_VAR, "1")
    assert folder_dialog.native_dialog_available(force_recheck=True) is False

    called = []
    monkeypatch.setattr(folder_dialog, "_run_tk_dialog",
                        lambda *a, **k: called.append(1) or {"path": str(tmp_path)})
    assert folder_dialog.pick_folder() is None
    assert called == [], "the child dialog must not run when disabled"


def test_pick_folder_returns_resolved_dir(monkeypatch, tmp_path):
    """A successful child verdict yields the resolved absolute folder."""
    target = tmp_path / "a project with spaces"
    target.mkdir()

    def fake_run(out_path, initial_dir, title, timeout):
        Path(out_path).write_text(json.dumps({"path": str(target)}), encoding="utf-8")
        return folder_dialog._read_child_verdict(out_path)

    monkeypatch.setattr(folder_dialog, "_run_tk_dialog", fake_run)
    result = folder_dialog.pick_folder(title="Pick")
    assert result == str(target.resolve())


def test_pick_folder_cancel_and_bad_path(monkeypatch, tmp_path):
    """Cancel, a file path and a missing path all yield None."""
    monkeypatch.setattr(folder_dialog, "_run_tk_dialog",
                        lambda *a, **k: {"cancelled": True})
    assert folder_dialog.pick_folder() is None

    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    monkeypatch.setattr(folder_dialog, "_run_tk_dialog",
                        lambda *a, **k: {"path": str(a_file)})
    assert folder_dialog.pick_folder() is None

    monkeypatch.setattr(folder_dialog, "_run_tk_dialog",
                        lambda *a, **k: {"path": str(tmp_path / "nope")})
    assert folder_dialog.pick_folder() is None


def test_read_child_verdict_tolerates_broken_output(tmp_path):
    """A crashed child (no file / corrupt / non-dict) reports an error."""
    missing = tmp_path / "missing.json"
    assert "error" in folder_dialog._read_child_verdict(str(missing))

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not json", encoding="utf-8")
    assert "error" in folder_dialog._read_child_verdict(str(corrupt))

    list_json = tmp_path / "list.json"
    list_json.write_text("[1, 2]", encoding="utf-8")
    assert "error" in folder_dialog._read_child_verdict(str(list_json))

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"path": "/tmp"}), encoding="utf-8")
    assert folder_dialog._read_child_verdict(str(good)) == {"path": "/tmp"}


def test_macos_fallback_used_only_on_backend_error(monkeypatch, tmp_path):
    """A tkinter error falls back to osascript; a cancel does not reopen."""
    target = tmp_path / "mac_proj"
    target.mkdir()

    monkeypatch.setattr(folder_dialog, "_run_tk_dialog",
                        lambda *a, **k: {"error": "no tkinter"})
    monkeypatch.setattr(folder_dialog, "_osascript_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_run_osascript_dialog",
                        lambda *a, **k: {"path": str(target)})
    assert folder_dialog.pick_folder() == str(target.resolve())

    # A plain cancel must NOT trigger the fallback dialog.
    fallback_calls = []
    monkeypatch.setattr(folder_dialog, "_run_tk_dialog",
                        lambda *a, **k: {"cancelled": True})
    monkeypatch.setattr(folder_dialog, "_run_osascript_dialog",
                        lambda *a, **k: fallback_calls.append(1) or {"path": str(target)})
    assert folder_dialog.pick_folder() is None
    assert fallback_calls == []


def test_availability_is_cached(monkeypatch):
    """The probe runs once and the verdict is cached."""
    calls = []

    class _Probe:
        returncode = 0

    def fake_run(*a, **k):
        calls.append(1)
        return _Probe()

    monkeypatch.delenv(folder_dialog.DISABLE_ENV_VAR, raising=False)
    monkeypatch.setattr(folder_dialog.sys, "platform", "linux")
    monkeypatch.setattr(folder_dialog.subprocess, "run", fake_run)

    assert folder_dialog.native_dialog_available() is True
    assert folder_dialog.native_dialog_available() is True
    assert len(calls) == 1, "the probe must be cached across calls"

    folder_dialog.reset_availability_cache()
    assert folder_dialog.native_dialog_available() is True
    assert len(calls) == 2
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
