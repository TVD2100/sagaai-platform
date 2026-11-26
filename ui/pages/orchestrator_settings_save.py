# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings_save - save plumbing of the settings tabs.

A settings save queues its confirmation and calls ``st.rerun()``, which throws
the render pass away: an ``st.success()`` right before it is never seen. The
helpers here keep that confirmation, put it under the button that saved - the
anchor mechanism of :mod:`ui.components.flash` - and return the user to the
section they were editing.

The arguments follow ``st.button``: a caller states the label and the key, and
where the confirmation appears follows from them.

Every name the tests may replace through the page facade is resolved late, at
call time, so a patch installed on ``ui.pages.orchestrator`` reaches the code
here.
"""
from typing import Any

from ui.components.flash import flash_error, render_flash_after, set_flash_anchor
from ui.pages.orchestrator_shared import SETTINGS_SECTIONS, late, st_proxy

st = st_proxy()
pin_settings_section = late("pin_settings_section")
t = late("t")


def pin_settings_section_impl(slug: str, section: str) -> None:
    """Ask the settings page to reopen *section* on the next render pass.

    A save handler calls this before ``st.rerun()``: the page reads the
    request before it creates the tabs and activates the matching one, so a
    save keeps the user in the section they were editing instead of dropping
    them on the first tab.
    """
    if section in SETTINGS_SECTIONS:
        from ui.pages.orchestrator_text import settings_section_key
        st.session_state[settings_section_key(slug)] = section


def settings_save_button_impl(label: str, key: str, **kwargs: Any) -> bool:
    """Render a settings save button that reports its result beneath itself.

    On the click pass this helper records the button as the current anchor, so
    the messages the handler queues belong to this button; on the next pass the
    same button renders and paints them right below itself. A drop-in
    replacement for a save button: the arguments are the ones ``st.button``
    accepts, and where the confirmation goes follows from the key.

    Returns:
        True when the button was pressed in this pass.
    """
    pressed = st.button(label, key=key, **kwargs)
    if pressed:
        set_flash_anchor(key)
    else:
        render_flash_after(key)
    return pressed


def settings_save_failed_impl(slug: str, section: str, message_key: str,
                              lang: str) -> None:
    """Report a failed settings save and keep the user in *section*.

    The message is queued and the page is rerun: the next pass paints it below
    the section's own content, so the error is visible together with the very
    settings whose save failed.
    """
    pin_settings_section(slug, section)
    flash_error(t(message_key, lang=lang))
    st.rerun()


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
