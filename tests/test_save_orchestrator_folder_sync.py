# -*- coding: utf-8 -*-
"""tests/test_save_orchestrator_folder_sync.py - a folder mirror must not eat a save.

``save_orchestrator`` writes the record to the database and then mirrors it
into the orchestrator's folder bundle. The mirror is a convenience copy: a
read-only data volume, a permission problem or a stale bundle must not turn
a successful database update into an exception, otherwise the caller never
reaches its confirmation and the user sees "nothing happened".
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import orchestrators as orch_mod

SLUG = "myorch"
STORED = {
    "id": 7,
    "slug": SLUG,
    "name": "MyOrch",
    "prompt_text": "STORED",
    "config": {},
    "is_builtin": False,
}


def test_successful_update_survives_a_failing_folder_mirror():
    """Given the bundle write fails, when saving, then the save still succeeds."""
    update = lambda *a, **k: True  # noqa: E731
    with patch.object(orch_mod, "repo_get_orchestrator_with_text", return_value=dict(STORED)), \
         patch.object(orch_mod, "repo_update_orchestrator", side_effect=update) as db_write, \
         patch.object(orch_mod, "_sync_orchestrator_folder",
                      side_effect=OSError("read-only file system")):
        assert orch_mod.save_orchestrator(SLUG, prompt_text="NEW") is True

    # The database write still happened - the mirror is not the source of truth.
    db_write.assert_called_once()
    assert db_write.call_args.kwargs.get("prompt_text") == "NEW"


def test_failed_update_still_reports_failure():
    """Given the database write fails, when saving, then False is returned."""
    with patch.object(orch_mod, "repo_get_orchestrator_with_text", return_value=dict(STORED)), \
         patch.object(orch_mod, "repo_update_orchestrator", return_value=False), \
         patch.object(orch_mod, "_sync_orchestrator_folder") as mirror:
        assert orch_mod.save_orchestrator(SLUG, prompt_text="NEW") is False

    mirror.assert_not_called()


def test_missing_orchestrator_reports_failure():
    """Given an unknown slug, when saving, then nothing is written."""
    with patch.object(orch_mod, "repo_get_orchestrator_with_text", return_value=None), \
         patch.object(orch_mod, "repo_update_orchestrator") as db_write:
        assert orch_mod.save_orchestrator("ghost", prompt_text="NEW") is False

    db_write.assert_not_called()


def test_folder_mirror_runs_on_success():
    """Given a clean save, then the folder bundle is refreshed."""
    with patch.object(orch_mod, "repo_get_orchestrator_with_text", return_value=dict(STORED)), \
         patch.object(orch_mod, "repo_update_orchestrator", return_value=True), \
         patch.object(orch_mod, "_sync_orchestrator_folder") as mirror:
        assert orch_mod.save_orchestrator(SLUG, name="Renamed") is True

    mirror.assert_called_once_with(SLUG)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
