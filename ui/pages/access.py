# -*- coding: utf-8 -*-
"""
ui.pages.access - "Access" settings page (Доступы).

Renders the "Password access" block:
  - master checkbox "Password access" (DISABLED by default; when disabled
    the password is never requested, even if one is configured);
  - password + confirmation fields, stored encrypted with Fernet via
    core.auth.save_auth_settings();
  - "How often to ask (hours)" numeric input (default 12).

The form value has priority over the SAGAAI_AUTH_PASSWORD environment
variable (see core.auth.resolve_auth_password). While an orchestrator is
running a task, the UI never asks for authentication even after the
re-ask interval expires.

All user-facing strings go through t(key, lang=lang).
"""
import streamlit as st

from core.i18n import t
from core.auth import (
    load_auth_settings,
    save_auth_settings,
)


def page_access() -> None:
    """Render the Access page (Доступы)."""
    lang = st.session_state.get("ui_lang")
    settings = load_auth_settings()

    st.title(f"\U0001f510 {t('access_page_title', lang=lang)}")
    st.markdown("---")

    st.markdown(f"## {t('access_section_password', lang=lang)}")

    enabled = st.checkbox(
        t("access_enabled_label", lang=lang),
        value=bool(settings["enabled"]),
        key="access_enabled",
        help=t("access_enabled_help", lang=lang),
    )

    with st.form(key="access_password_form", clear_on_submit=False):
        password = st.text_input(
            t("access_password_label", lang=lang),
            type="password",
            key="access_password_input",
            help=t("access_password_help", lang=lang),
        )
        confirm = st.text_input(
            t("access_password_confirm_label", lang=lang),
            type="password",
            key="access_password_confirm_input",
        )
        reask_hours = st.number_input(
            t("access_reask_hours_label", lang=lang),
            min_value=1.0,
            max_value=8760.0,
            value=float(settings["reask_hours"]),
            step=1.0,
            key="access_reask_hours_input",
            help=t("access_reask_hours_help", lang=lang),
        )
        submitted = st.form_submit_button(
            t("access_save_btn", lang=lang),
            type="primary",
            use_container_width=True,
        )

        if submitted:
            if password and password != confirm:
                st.error(t("access_password_mismatch", lang=lang))
            elif enabled and not password and not settings["password"]:
                st.error(t("access_password_empty", lang=lang))
            else:
                save_auth_settings(
                    enabled=bool(enabled),
                    password=password if password else None,
                    reask_hours=float(reask_hours),
                )
                st.success(t("access_saved", lang=lang))

    st.caption(t("access_env_password_hint", lang=lang))
    st.caption(t("access_orch_active_hint", lang=lang))

    # Refresh settings after a save to show the current status.
    settings = load_auth_settings()
    if settings["enabled"] and settings["password"]:
        st.info(t("access_status_enabled", lang=lang))
    else:
        st.info(t("access_status_disabled", lang=lang))
