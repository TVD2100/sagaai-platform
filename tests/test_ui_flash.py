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
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
