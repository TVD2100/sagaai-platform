# -*- coding: utf-8 -*-
"""
Unit tests for ui.components.flash - notifications that survive a rerun.

A queued message must be painted exactly once, on the NEXT render pass, so a
page can call ``flash_success(...)`` and then ``st.rerun()`` without losing
the confirmation.
"""
from __future__ import annotations

import sys

import pytest

from tests._st_mock import install_streamlit_mock


@pytest.fixture
def st_env():
    """Install the Streamlit mock and import a fresh ui.components.flash."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as mock:
        yield mock


def _flash_module():
    """Import ui.components.flash under the currently installed mock."""
    import ui.components.flash as mod
    return mod


def _painted(mock, kind):
    """Return the messages painted with the given Streamlit call name."""
    return [
        args[0] if args else ""
        for name, args, _kwargs in mock.calls
        if name == kind
    ]


def test_queued_message_is_painted_once(st_env):
    """Given a queued confirmation, when rendered, then it is shown once."""
    mod = _flash_module()
    mod.flash_success("Saved.")

    assert _painted(st_env, "success") == []

    mod.render_flash()
    assert _painted(st_env, "success") == ["Saved."]

    # A second render pass must not repeat the message.
    mod.render_flash()
    assert _painted(st_env, "success") == ["Saved."]


def test_kind_selects_the_painter(st_env):
    """Each queued kind must be painted by the matching Streamlit call."""
    mod = _flash_module()
    mod.flash_error("Failed.")
    mod.flash_warning("Careful.")
    mod.flash_info("Heads up.")
    mod.render_flash()

    assert _painted(st_env, "error") == ["Failed."]
    assert _painted(st_env, "warning") == ["Careful."]
    assert _painted(st_env, "info") == ["Heads up."]
    assert _painted(st_env, "success") == []


def test_unknown_kind_falls_back_to_success(st_env):
    """An unrecognised kind must not crash the render pass."""
    mod = _flash_module()
    mod.flash("Done.", "nonsense")
    mod.render_flash()

    assert _painted(st_env, "success") == ["Done."]


def test_empty_and_malformed_entries_are_skipped(st_env):
    """Empty texts and non-dict queue entries are ignored silently."""
    mod = _flash_module()
    mod.flash_success("")
    assert st_env.session_state.get(mod.FLASH_KEY) in ([], None)

    st_env.session_state[mod.FLASH_KEY] = ["junk", {"kind": "success", "text": ""}]
    mod.render_flash()
    assert _painted(st_env, "success") == []
    assert st_env.session_state[mod.FLASH_KEY] == []


def test_queue_is_capped(st_env):
    """The pending queue keeps only the newest MAX_PENDING messages."""
    mod = _flash_module()
    for i in range(mod.MAX_PENDING + 5):
        mod.flash_success(f"msg-{i}")

    queued = st_env.session_state[mod.FLASH_KEY]
    assert len(queued) == mod.MAX_PENDING
    assert queued[-1]["text"] == f"msg-{mod.MAX_PENDING + 4}"


def test_render_without_queue_is_a_noop(st_env):
    """Rendering an empty queue must not paint anything or raise."""
    mod = _flash_module()
    mod.render_flash()
    assert _painted(st_env, "success") == []


def test_malformed_queue_value_is_reset(st_env):
    """A corrupted queue value is replaced by an empty list."""
    mod = _flash_module()
    st_env.session_state[mod.FLASH_KEY] = "broken"
    mod.render_flash()
    assert st_env.session_state[mod.FLASH_KEY] == []


# ─── anchored notifications ────────────────────────────────────────────────


def test_an_orphaned_message_falls_back_to_the_page(st_env):
    """Given a message whose button never rendered, the page still shows it.

    The anchored widget paints its own messages on the pass after the save.
    If that widget is not on screen at all, the page-level pass is the last
    chance to reach the user - a confirmation lost in the queue is worse
    than one shown at the bottom of the page.
    """
    mod = _flash_module()
    mod.flash_success("Saved.", anchor="save-btn")

    mod.render_flash()

    assert _painted(st_env, "success") == ["Saved."]


def test_the_anchor_paints_on_the_next_pass_and_leaves_others(st_env):
    """Given two anchored messages, when one widget renders, only it paints."""
    mod = _flash_module()
    mod.flash_success("First.", anchor="btn-a")
    mod.flash_error("Second.", anchor="btn-b")

    mod.render_flash_after("btn-a")

    assert _painted(st_env, "success") == ["First."]
    assert _painted(st_env, "error") == []
    mod.render_flash_after("btn-b")
    assert _painted(st_env, "error") == ["Second."]


def test_a_queued_message_follows_the_button_being_handled(st_env):
    """A plain flash inside a button handler lands under that button."""
    mod = _flash_module()
    mod.set_flash_anchor("save-btn")
    mod.flash_success("Saved.")

    mod.render_flash_after("save-btn")

    assert _painted(st_env, "success") == ["Saved."]


def test_an_explicit_empty_anchor_stays_page_level(st_env):
    """An explicit "" keeps a message page-level even inside a handler."""
    mod = _flash_module()
    mod.set_flash_anchor("save-btn")
    mod.flash_success("General.", anchor="")

    mod.render_flash_after("save-btn")
    assert _painted(st_env, "success") == []

    mod.render_flash()
    assert _painted(st_env, "success") == ["General."]


def test_the_anchor_is_forgotten_once_the_button_rendered(st_env):
    """A handled anchor must not capture a later handler's messages."""
    mod = _flash_module()
    mod.set_flash_anchor("save-btn")

    mod.render_flash_after("save-btn")

    assert st_env.session_state.get(mod.ANCHOR_KEY) == ""
    mod.flash_success("Later.")
    mod.render_flash()
    assert _painted(st_env, "success") == ["Later."]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
