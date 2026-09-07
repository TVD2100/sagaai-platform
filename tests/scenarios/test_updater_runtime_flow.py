# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""tests/scenarios/test_updater_runtime_flow.py - updater runtime-flow scenarios.

Simulates the three user journeys fixed by the pending-updates task, walking
through the real Updates page (ui.pages.updates.page_updates) against the
REAL update store (no mocks on apply/rollback), so the scenarios cover the
whole pipeline end to end.

  Scenario 1 - force apply from the live UI:
      staged files may be applied right from the Updates page while the app
      is running, after an explicit confirmation; the Apply button is no
      longer disabled.

  Scenario 2 - force rollback from the live UI:
      with a live running marker, a confirmed Rollback restores the previous
      files through the real backup store.

  Scenario 3 - refusal is visible and the restart applies:
      while the app is running, an unforced apply refuses and the failure is
      reported on the Updates page (health.json); after the running marker
      disappears (user restart), the same staged update applies.

Each scenario is written in given -> when -> then form and goes through the
public page entry point, doubling as a tier-3 regression test.
"""

import hashlib
import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

import core.updater as updater  # noqa: E402
from core.updater_apply import (  # noqa: E402
    list_backup_runs as real_list_backup_runs,
    load_health as real_load_health,
    read_pending as real_read_pending,
)


# ─── helpers ────────────────────────────────────────────────────────────────

def _fresh_page():
    """Import the updates page under the installed Streamlit mock.

    ui.* modules are dropped from sys.modules first so the page picks up the
    current mock; call BEFORE patching its attributes.
    """
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    import ui.pages.updates as mod
    return mod


def _enter_page(root, extra_patches):
    """Patch the page namespace for one render pass; return a ready ExitStack.

    Blocks network/disk access to the real project root and simulates a live
    application (running marker). Tests override individual helpers via
    *extra_patches* (entered after the defaults, so they win).
    """
    stack = ExitStack()
    try:
        stack.enter_context(
            patch("ui.pages.updates.default_root", return_value=str(root))
        )
        stack.enter_context(patch("ui.pages.updates.load_health", return_value=None))
        stack.enter_context(
            patch("ui.pages.updates.read_pending", return_value=(None, []))
        )
        stack.enter_context(
            patch(
                "ui.pages.updates.load_state",
                return_value={"entries": {}, "last_run": {}},
            )
        )
        stack.enter_context(patch("ui.pages.updates.list_backup_runs", return_value=[]))
        stack.enter_context(patch("ui.pages.updates.is_app_running", return_value=True))
        for extra_patch in extra_patches:
            stack.enter_context(extra_patch)
    except Exception:
        stack.close()
        raise
    return stack


def _render(st, page, **session):
    """Render the updates page once, eating the expected rerun signal."""
    st.session_state.update(dict(ui_lang="English", **session))
    try:
        page.page_updates()
    except StopRerun:
        pass


def _button_call(st, key):
    """(args, kwargs) of the last rendered button with the given key."""
    for name, args, kwargs in reversed(st.calls):
        if name == "button" and kwargs.get("key") == key:
            return args, kwargs
    return None


def _success_messages(st):
    """All success message texts rendered by the page."""
    return [
        args[0]
        for name, args, _kwargs in st.calls
        if name == "success" and args
    ]


def _stage_files(root, payloads):
    """Write *payloads* {rel: bytes} into the real pending update store."""
    files = {}
    for rel, data in payloads.items():
        sha = hashlib.sha256(data).hexdigest()
        files[rel] = {"version": "1.0.0", "sha256": sha}
        updater._atomic_write_text(
            root,
            ".dev_agent/updates/pending/" + rel,
            data.decode("utf-8"),
        )
    updater._atomic_write_text(
        root,
        ".dev_agent/updates/pending.json",
        json.dumps({"files": files, "selected": sorted(files)}, indent=2) + "\n",
    )


def _read_text_file(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


# ─── Scenario 1: force apply from the live UI -------------------------------

def test_force_apply_from_live_ui(tmp_path):
    """
    Given a staged update and the running marker present,
    when the user opens the Updates page, ticks the confirmation and clicks
    Apply,
    then the real apply_updates runs in force mode: the file is replaced,
    pending.json is consumed and an applied-success message is shown.
    """
    root = str(tmp_path)
    _stage_files(root, {"defaults/prompt.md": b"prompt-new"})
    updater._atomic_write_text(root, "defaults/prompt.md", "prompt-old")

    with install_streamlit_mock() as st:
        page = _fresh_page()
        with _enter_page(
            tmp_path,
            [patch("ui.pages.updates.read_pending", new=real_read_pending)],
        ):
            _render(st, page)

            hit = _button_call(st, "updates_apply")
            assert hit is not None, "Apply button was not rendered"
            assert hit[1].get("disabled") is not True, \
                "Apply button must stay active while the app is running"
            listed = [
                args[0]
                for name, args, _kw in st.calls
                if name == "markdown"
                and args
                and args[0].startswith("- `defaults/prompt.md`")
            ]
            assert listed, "staged file is not listed on the page"

            st.reset_clicks()
            st.click("updates_apply")
            _render(st, page, updates_apply_confirm=True)

    assert _read_text_file(root, "defaults/prompt.md") == "prompt-new"
    assert not os.path.isfile(os.path.join(root, ".dev_agent/updates/pending.json"))
    assert st.errors == [], "page rendered errors: %r" % st.errors
    assert st.warnings == [], "force apply showed warnings: %r" % st.warnings
    assert any("Applied 1 file(s)." in msg for msg in _success_messages(st))


# ─── Scenario 2: force rollback from the live UI ----------------------------

def test_force_rollback_from_live_ui(tmp_path):
    """
    Given an applied update run (one replaced file and one created file) and
    a live running marker,
    when the user confirms and clicks Rollback on the Updates page,
    then the real rollback restores the replaced file and removes the created
    one through the backup store.
    """
    root = str(tmp_path)
    _stage_files(
        root,
        {
            "defaults/prompt.md": b"prompt-new",
            "defaults/newfile.md": b"brand-new-file",
        },
    )
    updater._atomic_write_text(root, "defaults/prompt.md", "prompt-old")

    applied = updater.apply_updates(root)
    assert applied["ok"], applied
    assert sorted(applied["applied"]) == ["defaults/newfile.md", "defaults/prompt.md"]
    assert _read_text_file(root, "defaults/prompt.md") == "prompt-new"
    assert os.path.isfile(os.path.join(root, "defaults/newfile.md"))

    with install_streamlit_mock() as st:
        page = _fresh_page()
        with _enter_page(
            tmp_path,
            [
                patch("ui.pages.updates.read_pending", new=real_read_pending),
                patch("ui.pages.updates.list_backup_runs", new=real_list_backup_runs),
            ],
        ):
            _render(st, page)
            hit = _button_call(st, "updates_rollback")
            assert hit is not None, "Rollback button was not rendered"
            assert hit[1].get("disabled") is not True, \
                "Rollback button must stay active while the app is running"

            st.reset_clicks()
            st.click("updates_rollback")
            _render(st, page, updates_rollback_confirm=True)

    assert _read_text_file(root, "defaults/prompt.md") == "prompt-old"
    assert not os.path.isfile(os.path.join(root, "defaults/newfile.md"))
    assert st.warnings == [], "force rollback showed warnings: %r" % st.warnings
    assert any("Restored 2 file(s)." in msg for msg in _success_messages(st))


# ─── Scenario 3: refusal visible, restart applies ---------------------------

def test_refusal_is_visible_and_restart_applies(tmp_path):
    """
    Given a staged update and a live running marker,
    when an unforced apply tries to run while the app is live,
    then it refuses, writes a failed health record that the Updates page
    shows as a warning, and after the marker disappears (user restart) the
    same staged update applies successfully.
    """
    root = str(tmp_path)
    _stage_files(root, {"defaults/prompt.md": b"prompt-new"})
    updater._atomic_write_text(root, "defaults/prompt.md", "prompt-old")
    updater._atomic_write_text(
        root,
        updater.RUNNING_FILE_REL,
        json.dumps(
            {"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat()}
        ),
    )

    with patch("core.updater.is_app_running", return_value=True):
        refusal = updater.apply_updates(root)
    assert not refusal["ok"]
    assert "app is running" in refusal["error"]
    health = json.loads(_read_text_file(root, ".dev_agent/updates/health.json"))
    assert health["ok"] is False
    assert "app is running" in health["error"]

    with install_streamlit_mock() as st:
        page = _fresh_page()
        with _enter_page(
            tmp_path,
            [
                patch("ui.pages.updates.load_health", new=real_load_health),
                patch("ui.pages.updates.read_pending", new=real_read_pending),
            ],
        ):
            _render(st, page)
    assert any("app is running" in msg for msg in st.warnings), (
        "refusal was not made visible: %r" % st.warnings
    )

    os.remove(os.path.join(root, updater.RUNNING_FILE_REL))
    restart = updater.apply_updates(root)
    assert restart["ok"], restart
    assert restart["applied"] == ["defaults/prompt.md"]
    assert _read_text_file(root, "defaults/prompt.md") == "prompt-new"
    assert not os.path.isfile(os.path.join(root, ".dev_agent/updates/pending.json"))
    health_after = json.loads(
        _read_text_file(root, ".dev_agent/updates/health.json")
    )
    assert health_after["ok"] is True
