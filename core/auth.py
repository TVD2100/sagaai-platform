"""
core.auth - Lightweight password-based authentication for Streamlit.

Access is controlled from the "Access" page (ui/pages/access.py) and stored
in the ConfigKV database:

  - auth_enabled     (bool)             - master switch, DISABLED by default.
                                          When disabled, no password is ever
                                          requested, even if one is configured.
  - auth_password    (str, encrypted)   - password entered via the UI form.
                                          Stored with Fernet (core.crypto),
                                          decrypted on load.
  - auth_reask_hours (float)            - how often the password is requested
                                          (in hours). Default: 12. After a
                                          successful login the password is not
                                          requested again until this interval
                                          expires; after it expires the user is
                                          asked once and a new interval starts.

Password resolution (the form wins over the environment):
  1. auth_password stored in the DB via the "Access" page (canonical).
  2. SAGAAI_AUTH_PASSWORD environment variable (fallback when the form
     value is empty).

While ANY orchestrator is running a task (its agent loop is active), the UI
never asks for authentication, even when the re-ask interval has expired.

This is NOT a full multi-user system - it protects against casual
unauthorized access when the app is exposed on a network. For
production use, deploy behind a reverse proxy with proper auth.
"""
import os
import hashlib
import secrets
import time

import streamlit as st

from core.i18n import t
from core.config import load_config, load_stored_config, save_config

_ENV_VAR = "SAGAAI_AUTH_PASSWORD"

# ConfigKV keys for access settings.
AUTH_ENABLED_KEY = "auth_enabled"
AUTH_PASSWORD_KEY = "auth_password"
AUTH_REASK_HOURS_KEY = "auth_reask_hours"

# Default re-ask interval (hours).
DEFAULT_REASK_HOURS = 12.0

# Session-state keys (namespaced to avoid collisions).
_AUTHENTICATED_KEY = "_sagaai_auth_authenticated"
_AUTH_FAILED_KEY = "_sagaai_auth_failed"
_AUTH_SUCCESS_TS_KEY = "_sagaai_auth_last_success_ts"


def _coerce_bool(value) -> bool:
    """Coerce a stored config value to bool (DB values may be strings)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def load_auth_settings() -> dict:
    """Return access settings from the DB form values (no env fallback).

    Returns:
        {"enabled": bool, "password": str, "reask_hours": float}
    """
    try:
        cfg = load_stored_config()
    except Exception:
        cfg = {}
    enabled = _coerce_bool(cfg.get(AUTH_ENABLED_KEY, False))
    password = cfg.get(AUTH_PASSWORD_KEY, "")
    if not isinstance(password, str):
        password = ""
    try:
        hours = float(cfg.get(AUTH_REASK_HOURS_KEY, DEFAULT_REASK_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_REASK_HOURS
    if hours <= 0:
        hours = DEFAULT_REASK_HOURS
    return {"enabled": enabled, "password": password.strip(), "reask_hours": hours}


def save_auth_settings(enabled: bool, password=None,
                       reask_hours: float = DEFAULT_REASK_HOURS) -> bool:
    """Persist access settings into ConfigKV.

    ``password=None`` keeps the existing stored password; any other value
    (including "") overwrites it. The password is encrypted with Fernet by
    ``save_config`` because ``auth_password`` is registered as a secret key.
    """
    cfg = load_config()
    cfg[AUTH_ENABLED_KEY] = bool(enabled)
    try:
        cfg[AUTH_REASK_HOURS_KEY] = float(reask_hours)
    except (TypeError, ValueError):
        cfg[AUTH_REASK_HOURS_KEY] = DEFAULT_REASK_HOURS
    if password is not None:
        cfg[AUTH_PASSWORD_KEY] = password
    return save_config(cfg)


def resolve_auth_password() -> str:
    """Return the effective access password (the form wins over the env var)."""
    form_pwd = load_auth_settings()["password"]
    if form_pwd:
        return form_pwd
    return os.environ.get(_ENV_VAR, "").strip()


def _get_configured_password_hash() -> str | None:
    """Return the SHA-256 hex digest of the configured password, or None
    if authentication is not configured."""
    pwd = resolve_auth_password()
    if not pwd:
        return None
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()


def is_auth_enabled() -> bool:
    """Return True if password authentication is switched ON and a password
    is configured (via the form or the environment).

    When the master switch is OFF, this returns False even if a password is
    set - the user explicitly asked not to be prompted.
    """
    settings = load_auth_settings()
    if not settings["enabled"]:
        return False
    return bool(resolve_auth_password())


def any_orchestrator_active(session_state=None) -> bool:
    """Return True if ANY orchestrator is currently running a task.

    An orchestrator is considered active while its agent-loop state
    (``orch_<slug>_loop_state``) exists and is not in a terminal phase
    ("done", "error"). This mirrors the ``agent_is_active`` flag used by
    ui.pages.orchestrator.
    """
    if session_state is None:
        session_state = st.session_state
    try:
        items = session_state.items()
    except Exception:
        return False
    for key, value in items:
        if not (key.startswith("orch_") and key.endswith("_loop_state")):
            continue
        if value is None:
            continue
        phase = getattr(value, "phase", None)
        if phase is not None and phase not in ("done", "error"):
            return True
    return False


def _auth_grace_active() -> bool:
    """Return True while the re-ask interval has not expired yet.

    The interval starts at the last successful login and lasts for
    ``auth_reask_hours`` hours.
    """
    ts = st.session_state.get(_AUTH_SUCCESS_TS_KEY, 0)
    if not ts:
        return False
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return False
    hours = load_auth_settings()["reask_hours"]
    return (time.time() - ts) < (hours * 3600.0)


def is_authenticated() -> bool:
    """Return True if the current session is authenticated.

    Always returns True if auth is disabled OR an orchestrator is running a
    task. A successful login is valid for ``auth_reask_hours`` hours; when
    that interval expires the session becomes unauthenticated again and the
    next successful login starts a fresh interval.
    """
    if not is_auth_enabled():
        return True
    if any_orchestrator_active():
        return True
    if not st.session_state.get(_AUTHENTICATED_KEY, False):
        return False
    if not _auth_grace_active():
        st.session_state[_AUTHENTICATED_KEY] = False
        return False
    return True


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
                    st.session_state[_AUTH_SUCCESS_TS_KEY] = time.time()
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
    If auth is disabled, or an orchestrator is actively running a task,
    this is a no-op.
    If auth is enabled and the user is not authenticated (or the re-ask
    interval has expired), renders the login form and stops the page.
    """
    if not is_auth_enabled():
        return

    if any_orchestrator_active():
        return

    if is_authenticated():
        return

    _render_login_form()
    st.stop()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
