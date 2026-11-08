# -*- coding: utf-8 -*-
"""
core.naming_runtime - which interface language the data layer resolves names for.

``core.naming`` is pure and takes a language argument; something has to say
which language that is. This module is that single source, so the data layer,
the prompt builder and the marketplace all resolve an entity name for the same
language instead of each guessing.

The session language of the running interface wins when it exists. A background
worker, a transport API call and a test process have no session, and for them
the stored ``ui_lang`` setting is the answer - it is what the interface last
showed to its owner. Nothing here imports streamlit at module level: the import
is attempted inside the function and any failure falls through to the setting.
"""
from __future__ import annotations

from typing import Optional


def current_lang() -> str:
    """Return the interface language display name (e.g. ``"Русский"``).

    The order is: the running Streamlit session, then the stored ``ui_lang``
    setting, then the shipped default. Returns an empty string when none of the
    three is available, which the resolver treats as "no language preference"
    and answers in English.
    """
    session_lang = _session_lang()
    if session_lang:
        return session_lang
    stored = _stored_lang()
    if stored:
        return stored
    try:
        from core.config import get_default_ui_lang
        return str(get_default_ui_lang() or "")
    except Exception:
        return ""


def current_lang_code() -> str:
    """Return the canonical code (en / ru / zh) of the interface language.

    Empty when the language is unknown; ``core.naming.resolve_display_name``
    then falls back to English, which is the documented behaviour.
    """
    lang = current_lang()
    if not lang:
        return ""
    try:
        from core.i18n import lang_code
        return lang_code(lang) or ""
    except Exception:
        from core.naming import normalize_lang
        return normalize_lang(lang)


def _session_lang() -> Optional[str]:
    """Read ``st.session_state.ui_lang`` when a session is actually running.

    Outside a Streamlit script run the session object raises, and a background
    worker must not fail because of that - hence the broad guard.
    """
    try:
        import streamlit as st
        value = st.session_state.get("ui_lang")
        return str(value).strip() if value else ""
    except Exception:
        return ""


def _stored_lang() -> str:
    """Read the persisted ``ui_lang`` value from the platform settings."""
    try:
        from core.config import load_config
        value = load_config().get("ui_lang")
        return str(value).strip() if value else ""
    except Exception:
        return ""
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
