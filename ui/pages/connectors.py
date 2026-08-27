# -*- coding: utf-8 -*-
"""
ui/pages/connectors.py - external service connections page.

CRUD page for service connections (e.g. GitHub API tokens):
  - create a new connection (name, service, token, account);
  - list existing connections with token presence indicator;
  - test a connection against the service and refresh account info;
  - edit name/account and rotate the token;
  - delete a connection with inline confirmation.

All persistence goes through core.connectors (folder-based manifests in
DATA_DIR/connectors/<id>/). Tokens are always encrypted at rest and never
passed to the frontend in plain text.
"""
import streamlit as st

from core.i18n import t
from core import connectors
from core.connectors import (
    list_connections,
    list_services,
    create_connection,
    update_connection,
    delete_connection,
)


_IGNORE = (connectors,)  # keep the module reference for potential future use


def _service_options(lang: str):
    """Return [(service_id, display_label), ...] for the services registry."""
    options = []
    for svc in list_services():
        svc_id = str(svc.get("id") or "")
        label = t(f"connectors_service_{svc_id}", lang=lang) or svc.get("name") or svc_id
        options.append((svc_id, label))
    return options


def _test_connection(conn_id: str, lang: str) -> None:
    """Validate *conn_id* against its service and show the outcome."""
    try:
        from core.github_connector import test_connection
        result = test_connection(conn_id)
    except Exception as e:
        st.error(t("connectors_test_error", lang=lang, error=str(e)))
        return
    if result.get("ok"):
        login = str(result.get("login") or conn_id)
        st.success(t("connectors_test_ok", lang=lang, login=login))
    else:
        st.error(t("connectors_test_error", lang=lang,
                   error=str(result.get("error") or "")))


def _render_create_form(lang: str) -> None:
    """Render the create-connection form in an expander."""
    with st.expander(t("connectors_create_title", lang=lang), expanded=False):
        options = _service_options(lang)
        svc_ids = [o[0] for o in options]
        with st.form("connector_create_form"):
            name = st.text_input(
                t("connectors_name", lang=lang), key="conn_new_name",
            )
            if svc_ids:
                default_idx = svc_ids.index("github") if "github" in svc_ids else 0
                svc_id = st.selectbox(
                    t("connectors_service", lang=lang),
                    options=svc_ids,
                    index=default_idx,
                    format_func=lambda s: dict(options).get(s, s),
                    key="conn_new_service",
                )
            else:
                svc_id = ""
                st.warning(t("connectors_no_services", lang=lang))
            token = st.text_input(
                t("connectors_token", lang=lang),
                type="password",
                key="conn_new_token",
                help=t("connectors_token_help", lang=lang),
            )
            account = st.text_input(
                t("connectors_account", lang=lang),
                key="conn_new_account",
                help=t("connectors_account_help", lang=lang),
            )
            submitted = st.form_submit_button(
                t("connectors_create_btn", lang=lang), type="primary"
            )
    if submitted:
        try:
            create_connection(svc_id, name, token, account=account)
            st.success(t("connectors_created", lang=lang))
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _render_edit_form(conn: dict, lang: str) -> None:
    """Render the inline edit form for one connection."""
    conn_id = str(conn.get("id") or "")
    st.markdown(f"**{t('connectors_edit_title', lang=lang)}**")
    with st.form(f"conn_edit_form_{conn_id}"):
        name = st.text_input(
            t("connectors_name", lang=lang),
            value=str(conn.get("name") or ""),
            key=f"conn_edit_name_{conn_id}",
        )
        account = st.text_input(
            t("connectors_account", lang=lang),
            value=str(conn.get("account") or ""),
            key=f"conn_edit_account_{conn_id}",
        )
        token = st.text_input(
            t("connectors_token", lang=lang),
            type="password",
            key=f"conn_edit_token_{conn_id}",
            help=t("connectors_token_leave_empty", lang=lang),
        )
        save = st.form_submit_button(t("connectors_save", lang=lang), type="primary")
    if save:
        try:
            update_connection(conn_id, name=name, account=account, token=token)
            st.session_state[f"conn_edit_{conn_id}"] = False
            st.success(t("connectors_saved", lang=lang))
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _render_connection_card(conn: dict, lang: str) -> None:
    """Render one connection as an expander card with actions."""
    conn_id = str(conn.get("id") or "")
    name = str(conn.get("name") or conn_id)
    svc_id = str(conn.get("service") or "?")
    account = str(conn.get("account") or "")
    has_token = bool(conn.get("has_token"))
    svc_label = t(f"connectors_service_{svc_id}", lang=lang) or svc_id

    token_mark = (
        t("connectors_has_token", lang=lang)
        if has_token else
        t("connectors_no_token", lang=lang)
    )
    header = f"**{name}** · {svc_label} · {token_mark}"
    if account:
        header += f" · `{account}`"

    with st.expander(header, expanded=False):
        st.caption(f"id: `{conn_id}`")
        if conn.get("created_at"):
            st.caption(t("connectors_created_at", lang=lang,
                         date=str(conn.get("created_at"))))

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(t("connectors_test_btn", lang=lang),
                         key=f"conn_test_{conn_id}",
                         use_container_width=True):
                _test_connection(conn_id, lang)
        with c2:
            edit_key = f"conn_edit_{conn_id}"
            if st.button(t("connectors_edit_btn", lang=lang),
                         key=f"conn_edit_btn_{conn_id}",
                         use_container_width=True):
                st.session_state[edit_key] = True
                st.rerun()
        with c3:
            confirm_key = f"conn_confirm_del_{conn_id}"
            if not st.session_state.get(confirm_key):
                if st.button(t("btn_delete", lang=lang),
                             key=f"conn_del_{conn_id}",
                             use_container_width=True):
                    st.session_state[confirm_key] = conn_id
                    st.rerun()
            else:
                del_cols = st.columns(2)
                with del_cols[0]:
                    if st.button(t("btn_yes_delete", lang=lang),
                                 key=f"conn_del_yes_{conn_id}",
                                 use_container_width=True):
                        if delete_connection(conn_id):
                            st.session_state[confirm_key] = None
                            st.success(t("connectors_deleted", lang=lang))
                            st.rerun()
                        else:
                            st.error(t("connectors_delete_error", lang=lang))
                with del_cols[1]:
                    if st.button(t("btn_cancel", lang=lang),
                                 key=f"conn_del_no_{conn_id}",
                                 use_container_width=True):
                        st.session_state[confirm_key] = None
                        st.rerun()

        if st.session_state.get(f"conn_edit_{conn_id}"):
            _render_edit_form(conn, lang)


def page_connectors() -> None:
    """Connections management page (dispatched from ui.app)."""
    lang = st.session_state.get("ui_lang", "en")
    st.title(t("page_connectors_title", lang=lang))
    st.markdown(t("connectors_page_desc", lang=lang))

    _render_create_form(lang)

    connections = list_connections()
    if not connections:
        st.info(t("connectors_empty", lang=lang))
        return

    st.markdown("---")
    for conn in connections:
        _render_connection_card(conn, lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
