"""
core.auth - Lightweight password-based authentication for Streamlit.

By default auth is DISABLED (no password required).
To enable, set the SAGAAI_AUTH_PASSWORD environment variable.
When enabled, the user must enter the correct password before any
page content is rendered.

This is NOT a full multi-user system - it protects against casual
unauthorized access when the app is exposed on a network. For
production use, deploy behind a reverse proxy with proper auth.
"""
import os
import hashlib
import secrets

import streamlit as st

from core.i18n import t
from core.config import load_config

_ENV_VAR = "SAGAAI_AUTH_PASSWORD"

# Session-state keys (namespaced to avoid collisions)
_AUTHENTICATED_KEY = "_sagaai_auth_authenticated"
_AUTH_FAILED_KEY = "_sagaai_auth_failed"


def _get_configured_password_hash() -> str | None:
    """Return the SHA-256 hex digest of the configured password, or None
    if authentication is not configured."""
    pwd = os.environ.get(_ENV_VAR, "").strip()
    if not pwd:
        return None
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()


def is_auth_enabled() -> bool:
    """Return True if password authentication is configured."""
    return _get_configured_password_hash() is not None


def is_authenticated() -> bool:
    """Return True if the current session is authenticated.

    Always returns True if auth is disabled.
    """
    if not is_auth_enabled():
        return True
    return st.session_state.get(_AUTHENTICATED_KEY, False)


def _resolve_auth_lang() -> str:
    """Determine the UI language for the auth form.

    Since auth is rendered before st.session_state.ui_lang is initialised,
    we read from the database config as a fallback.
    """
    # Try session state first (if auth is re-triggered after init)
    lang = st.session_state.get("ui_lang", "")
    if lang:
        return lang
    # Fallback: read from config DB
    try:
        cfg = load_config()
        saved = cfg.get("ui_lang", "")
        if saved:
            return saved
    except Exception:
        pass
    return ""


def _render_login_form():
    """Render the password form and handle authentication."""
    expected_hash = _get_configured_password_hash()
    lang = _resolve_auth_lang()

    # Hide the sidebar and default Streamlit UI during login
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] { display: none; }
        [data-testid="stToolbar"] { display: none; }
        header { display: none; }
        footer { display: none; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.title(f"\U0001f916 {t('app_title', lang=lang)}")
        st.caption(t("auth_required", lang=lang))

        with st.form(key="sagaai_auth_form", clear_on_submit=True):
            password = st.text_input(
                t("auth_password_label", lang=lang),
                type="password",
                placeholder=t("auth_password_placeholder", lang=lang),
                key="_sagaai_auth_input",
            )
            submitted = st.form_submit_button(
                t("auth_sign_in", lang=lang),
                type="primary",
                use_container_width=True,
            )

            if submitted:
                input_hash = hashlib.sha256(
                    (password or "").encode("utf-8")
                ).hexdigest()

                if secrets.compare_digest(input_hash, expected_hash):
                    st.session_state[_AUTHENTICATED_KEY] = True
                    st.session_state[_AUTH_FAILED_KEY] = False
                    st.rerun()
                else:
                    st.session_state[_AUTH_FAILED_KEY] = True

        if st.session_state.get(_AUTH_FAILED_KEY):
            st.error(t("auth_incorrect_password", lang=lang))


def require_auth():
    """Ensure the user is authenticated before proceeding.

    Call this at the very beginning of `main()` in ui/app.py,
    before any page content or sidebar is rendered.
    If auth is disabled, this is a no-op.
    If auth is enabled and the user is not yet authenticated,
    renders the login form and stops the page.
    """
    if not is_auth_enabled():
        return

    if is_authenticated():
        return

    _render_login_form()
    st.stop()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
