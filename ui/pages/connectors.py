# -*- coding: utf-8 -*-
"""
ui/pages/connectors.py - external service connections page.

CRUD page for service connections (GitHub API tokens, SSH servers):
  - create a new connection (service-specific fields: token/account for
    GitHub REST; host/port/username + password or private key for SSH);
  - list existing connections with credential presence indicator;
  - test a connection against the service and refresh account info;
  - edit display fields and rotate credentials;
  - delete a connection with inline confirmation.

All persistence goes through core.connectors (folder-based manifests in
DATA_DIR/connectors/<id>/). Credentials are always encrypted at rest and
never passed to the frontend in plain text.
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
        conn = connectors.get_connection(conn_id)
        service = str((conn or {}).get("service") or "")
        if service == "ssh":
            from core import ssh_connector
            result = ssh_connector.test_connection(conn_id)
            label = f"{result.get('username')}@{result.get('host')}"
        elif service == "github_rest":
            from core.github_connector_rest import test_connection
            result = test_connection(conn_id)
            label = str(result.get("login") or conn_id)
        else:
            st.error(t("connectors_test_error", lang=lang,
                       error=f"Unsupported service: {service}"))
            return
    except Exception as e:
        st.error(t("connectors_test_error", lang=lang, error=str(e)))
        return
    if result.get("ok"):
        st.success(t("connectors_test_ok", lang=lang, login=label))
    else:
        st.error(t("connectors_test_error", lang=lang,
                   error=str(result.get("error") or "")))


def _ssh_stored_fields(conn: dict) -> set:
    """Return secret field names already stored on a connection (masked view)."""
    masked = (conn or {}).get("secrets_masked")
    if isinstance(masked, dict):
        return {str(k) for k in masked}
    return set()


def _render_ssh_fields(lang: str, prefix: str, existing: dict = None):
    """Render SSH config + secret widgets; return (config, secrets).

    ``config`` collects host/port/username. ``secrets`` collects only the
    non-empty password / private_key / key_passphrase inputs, so empty fields
    keep the stored secrets on edit.
    """
    cfg = (existing or {}).get("config")
    cfg = cfg if isinstance(cfg, dict) else {}
    host = st.text_input(
        t("connectors_host", lang=lang),
        value=str(cfg.get("host") or ""),
        key=f"{prefix}_host",
    )
    port = st.number_input(
        t("connectors_port", lang=lang),
        min_value=1, max_value=65535, step=1,
        value=int(cfg.get("port") or 22),
        key=f"{prefix}_port",
    )
    username = st.text_input(
        t("connectors_username", lang=lang),
        value=str(cfg.get("username") or ""),
        key=f"{prefix}_username",
    )
    st.caption(t("connectors_ssh_auth_hint", lang=lang))
    stored = _ssh_stored_fields(existing or {})
    keep_help = t("connectors_secret_leave_empty", lang=lang)
    password = st.text_input(
        t("connectors_password", lang=lang),
        type="password",
        key=f"{prefix}_password",
        help=(keep_help if "password" in stored else None),
    )
    private_key = st.text_area(
        t("connectors_private_key", lang=lang),
        key=f"{prefix}_private_key",
        help=(keep_help if "private_key" in stored else None),
    )
    passphrase = st.text_input(
        t("connectors_key_passphrase", lang=lang),
        type="password",
        key=f"{prefix}_key_passphrase",
        help=(keep_help if "key_passphrase" in stored else None),
    )
    config = {"host": host, "port": port, "username": username}
    secrets = {}
    if str(password or "").strip():
        secrets["password"] = password
    if str(private_key or "").strip():
        secrets["private_key"] = private_key
    if str(passphrase or "").strip():
        secrets["key_passphrase"] = passphrase
    return config, secrets


def _render_create_form(lang: str) -> None:
    """Render the create-connection form in an expander.

    The service selector sits outside the form so switching services
    re-renders the service-specific fields on the next script run.
    """
    with st.expander(t("connectors_create_title", lang=lang), expanded=False):
        options = _service_options(lang)
        svc_ids = [o[0] for o in options]
        if not svc_ids:
            st.warning(t("connectors_no_services", lang=lang))
            return
        svc_id = st.selectbox(
            t("connectors_service", lang=lang),
            options=svc_ids,
            index=0,
            format_func=lambda s: dict(options).get(s, s),
            key="conn_new_service",
        )
        with st.form("connector_create_form"):
            name = st.text_input(
                t("connectors_name", lang=lang), key="conn_new_name",
            )
            if svc_id == "ssh":
                config, secrets = _render_ssh_fields(lang, prefix="conn_new")
                token = ""
            else:
                config, secrets = None, None
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
                t("connectors_create_btn", lang=lang), type="primary",
                key="conn_new_submit",
            )
    if submitted:
        try:
            create_connection(svc_id, name, token, account=account,
                              config=config, secrets=secrets)
            st.success(t("connectors_created", lang=lang))
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _render_edit_form(conn: dict, lang: str) -> None:
    """Render the inline edit form for one connection."""
    conn_id = str(conn.get("id") or "")
    service = str(conn.get("service") or "")
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
        token = ""
        config, secrets = None, None
        if service == "ssh":
            config, secrets = _render_ssh_fields(
                lang, prefix=f"conn_edit_{conn_id}", existing=conn
            )
        else:
            token = st.text_input(
                t("connectors_token", lang=lang),
                type="password",
                key=f"conn_edit_token_{conn_id}",
                help=t("connectors_token_leave_empty", lang=lang),
            )
        save = st.form_submit_button(t("connectors_save", lang=lang), type="primary")
    if save:
        try:
            update_connection(conn_id, name=name, account=account, token=token,
                              config=config, secrets=secrets)
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

    masked = conn.get("secrets_masked")
    if conn.get("has_secrets") and isinstance(masked, dict):
        fields = ", ".join(sorted(str(k) for k in masked))
        cred_mark = t("connectors_has_secrets", lang=lang, fields=fields)
    else:
        cred_mark = (
            t("connectors_has_token", lang=lang)
            if has_token else
            t("connectors_no_token", lang=lang)
        )
    header = f"**{name}** · {svc_label} · {cred_mark}"
    if account:
        header += f" · `{account}`"

    with st.expander(header, expanded=False):
        st.caption(f"id: `{conn_id}`")
        cfg = conn.get("config")
        if isinstance(cfg, dict) and cfg:
            host = str(cfg.get("host") or "")
            user = str(cfg.get("username") or "")
            target = f"{user}@{host}" if (user or host) else ""
            if target and cfg.get("port"):
                target += f":{cfg.get('port')}"
            if target:
                st.caption(target)
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
