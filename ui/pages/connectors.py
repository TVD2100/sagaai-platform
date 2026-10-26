# -*- coding: utf-8 -*-
"""
ui/pages/connectors.py - external service connections page.

The page follows the assistants page pattern: a LIST view and full-page
form views that replace it, so nothing is written until the user submits.

  - LIST: three tabs (General services, MCP servers, Connections) rendered
    by a STATEFUL st.tabs widget (on_change="rerun" plus an explicit
    ``default`` label). Without on_change="rerun" the widget keeps no
    state at all - the key is only a CSS identity - so a rerun would fall
    back to the first tab; with it, the active label lives in
    ``st.session_state[key]`` and ``default`` reliably re-targets it;
  - General services: icon tiles (GitHub REST, SSH). A click only opens
    the create form for that service - no connection is created yet;
  - MCP servers: preset tiles (bundled secret-free templates) plus a
    "custom server" tile. A preset click opens the create form with the
    preset config; the credentials are typed right there;
  - CREATE: a full-page form (name, account, service fields or the preset
    credential fields) with Create / Cancel. The connection is persisted
    only on Create and already carries the entered credentials (an active
    connection, not a credential-less draft). Cancel returns to the list
    and writes nothing;
  - EDIT: the same full-page form for an existing connection; pending
    drafts created from the chat are completed here too;
  - Connections: a card per saved connection with the service icon,
    credential state, test / edit / delete actions.

All persistence goes through core.connectors (folder-based manifests in
DATA_DIR/connectors/<id>/). Credentials are always encrypted at rest and
never passed to the frontend in plain text.
"""
import streamlit as st

from core.i18n import t
from ui.components.flash import flash_error, flash_success
from core import connectors
from core import connector_presets
from core import personal_memory as pm
from core.connectors import (
    list_connections,
    list_services,
    create_connection,
    update_connection,
    delete_connection,
)


# Number of tiles per row on the general-services and MCP tabs.
_GENERAL_TILES_PER_ROW = 3
_MCP_TILES_PER_ROW = 3


# Internal ids of the list tabs; the label list order must match this tuple.
_TAB_GENERAL = "tab_general"
_TAB_MCP = "tab_mcp"
_TAB_CONNECTIONS = "tab_connections"
_TAB_IDS = (_TAB_GENERAL, _TAB_MCP, _TAB_CONNECTIONS)

# session_state key of the stateful st.tabs widget (its value is the active
# label) and the key carrying the tab the next pass must open on.
CONNECTORS_TABS_KEY = "connectors_active_tab"
_TARGET_TAB_KEY = "connectors_target_tab"

# Page views: the list of tabs, or one full-page form replacing it.
VIEW_KEY = "connectors_view"
VIEW_LIST = "list"
VIEW_CREATE = "create"
VIEW_EDIT = "edit"

# What the create form builds: a connector service plus an optional MCP
# preset id (empty means a raw custom MCP server).
_CREATE_SERVICE_KEY = "connectors_create_service"
_CREATE_PRESET_KEY = "connectors_create_preset"
_EDIT_ID_KEY = "connectors_edit_id"

# Widget-key prefixes owned by the create form; they are dropped when the
# form opens so a previous draft never leaks into a new one.
_CREATE_WIDGET_PREFIXES = ("conn_new_", "mcp_new_", "conn_create_")


def _conn_tab_labels(lang: str) -> list:
    """Return the translated tab labels in the fixed tab order."""
    return [
        t("connectors_tab_general", lang=lang),
        t("connectors_tab_mcp", lang=lang),
        t("connectors_tab_connections", lang=lang),
    ]


def _reset_create_widgets() -> None:
    """Drop the create-form widget state so a new form starts clean."""
    for key in [k for k in list(st.session_state.keys())
                if isinstance(k, str) and k.startswith(_CREATE_WIDGET_PREFIXES)]:
        st.session_state.pop(key, None)


def _start_create(service: str, preset_id: str = "") -> None:
    """Open the create form for a service/preset without writing anything."""
    _reset_create_widgets()
    st.session_state[_CREATE_SERVICE_KEY] = service
    st.session_state[_CREATE_PRESET_KEY] = preset_id
    st.session_state[VIEW_KEY] = VIEW_CREATE
    st.rerun()


def _start_edit(conn_id: str) -> None:
    """Open the edit form of an existing connection."""
    st.session_state[_EDIT_ID_KEY] = str(conn_id)
    st.session_state[VIEW_KEY] = VIEW_EDIT
    st.rerun()


def _back_to_list(tab_id: str = _TAB_CONNECTIONS) -> None:
    """Return to the list view on *tab_id*, dropping the form state."""
    st.session_state[VIEW_KEY] = VIEW_LIST
    st.session_state[_TARGET_TAB_KEY] = _TAB_IDS.index(tab_id) if tab_id in _TAB_IDS else 0
    st.session_state.pop(_CREATE_SERVICE_KEY, None)
    st.session_state.pop(_CREATE_PRESET_KEY, None)
    st.session_state.pop(_EDIT_ID_KEY, None)
    _reset_create_widgets()
    st.rerun()


def _apply_target_tab(lang: str):
    """Consume the pending tab request and return the tabs ``default`` label.

    The stateful tabs widget reads its value from session_state, so a request
    is honoured by dropping the stored value and handing the wanted label as
    ``default`` - that happens BEFORE the widget is created, which is the only
    moment Streamlit allows a widget state to change.
    """
    labels = _conn_tab_labels(lang)
    target = st.session_state.pop(_TARGET_TAB_KEY, None)
    default_label = None
    if isinstance(target, int) and 0 <= target < len(labels):
        default_label = labels[target]
    elif isinstance(target, str) and target in labels:
        default_label = target
    if default_label:
        st.session_state.pop(CONNECTORS_TABS_KEY, None)
    elif st.session_state.get(CONNECTORS_TABS_KEY) not in labels:
        # A stored translated label goes stale when the UI language changes.
        st.session_state.pop(CONNECTORS_TABS_KEY, None)
    return default_label


def _general_service_tiles(lang: str) -> list:
    """Return [(svc_id, icon, label, description), ...] for the general tab

    (the mcp service lives in its own tab and is excluded here).
    """
    tiles = []
    for svc in list_services():
        svc_id = str(svc.get("id") or "")
        if svc_id == "mcp":
            continue
        icon = str(svc.get("icon") or "").strip()
        label = (t(f"connectors_service_{svc_id}", lang=lang)
                 or svc.get("name") or svc_id)
        description = (t(f"connectors_service_desc_{svc_id}", lang=lang)
                       or str(svc.get("description") or ""))
        tiles.append((svc_id, icon, label, description))
    return tiles


def _service_label(svc_id: str, lang: str) -> str:
    """Return the translated label of a connector service."""
    return t(f"connectors_service_{svc_id}", lang=lang) or svc_id


def _render_general_service_tiles(lang: str, tiles: list) -> None:
    """Render the icon tiles of the general services.

    A click opens the create form for that service; nothing is saved here.
    """
    for row_start in range(0, len(tiles), _GENERAL_TILES_PER_ROW):
        row = tiles[row_start:row_start + _GENERAL_TILES_PER_ROW]
        cols = st.columns(len(row))
        for col, (svc_id, icon, label, description) in zip(cols, row):
            with col:
                tile_label = f"{icon} {label}".strip() if icon else label
                if st.button(tile_label,
                             key=f"conn_service_tile_{svc_id}",
                             use_container_width=True):
                    _start_create(svc_id)
                if description:
                    st.caption(description)


_SERVICE_ICONS = None  # lazily built {service_id: icon} cache


def _service_icon(svc_id: str) -> str:
    """Return the icon of a connector service ('' when the id is unknown)."""
    global _SERVICE_ICONS
    if _SERVICE_ICONS is None:
        _SERVICE_ICONS = {}
        for svc in list_services():
            sid = str(svc.get("id") or "")
            if sid:
                _SERVICE_ICONS[sid] = str(svc.get("icon") or "").strip()
    return _SERVICE_ICONS.get(svc_id, "")


def _test_connection(conn_id: str, lang: str, connection=None) -> None:
    """Validate *conn_id* against its service and show the outcome."""
    try:
        conn = connection if connection is not None else connectors.get_connection(conn_id)
        service = str((conn or {}).get("service") or "")
        if service == "ssh":
            from core import ssh_connector
            result = ssh_connector.test_connection(conn_id)
            label = f"{result.get('username')}@{result.get('host')}"
        elif service == "github_rest":
            from core.github_connector_rest import test_connection
            result = test_connection(conn_id)
            label = str(result.get("login") or conn_id)
        elif service == "mcp":
            from core import mcp_tools
            result = mcp_tools.test_connection(conn_id)
            label = str(result.get("server_name") or conn_id)
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


def _mcp_stored_env_json(conn: dict) -> bool:
    """Return True when the connection already stores the env_json secret."""
    masked = (conn or {}).get("secrets_masked")
    if isinstance(masked, dict):
        return "env_json" in masked
    return False


def _mcp_stored_env_names(conn: dict) -> set:
    """Return the env var names already stored on a connection (no values)."""
    conn_id = str((conn or {}).get("id") or "")
    if not conn_id:
        return set()
    try:
        return set(connectors.get_mcp_env_var_names(conn_id))
    except Exception:
        return set()


def _mcp_preset_options() -> list:
    """Return the bundled MCP presets (public views, no secrets)."""
    try:
        return list(connector_presets.list_presets("mcp"))
    except Exception:
        return []


def _mcp_preset_by_id(preset_id: str):
    """Resolve a preset public view by id (None when unknown).

    The catalogue scan comes first so a connection created from a preset
    keeps the preset form while the preset is visible. Presets hidden from
    the catalogue (``hidden: true``) are still resolved by id, so an
    existing connection keeps its preset fields even after the preset was
    taken off the tile list.
    """
    preset_id = str(preset_id or "")
    if not preset_id:
        return None
    for preset in _mcp_preset_options():
        if str(preset.get("id") or "") == preset_id:
            return preset
    try:
        return connector_presets.resolve_preset(preset_id, "mcp")
    except Exception:
        return None


def _mcp_preset_for(existing: dict):
    """Resolve the stored preset id of a connection or None (custom server)."""
    cfg = (existing or {}).get("config")
    cfg = cfg if isinstance(cfg, dict) else {}
    return _mcp_preset_by_id(str(cfg.get("preset") or ""))


def _mcp_field_help(meta: dict, stored: bool, lang: str) -> str:
    """Build the help text of one preset credential field."""
    parts = [str(meta.get("help") or "")]
    if stored:
        parts.append(t("connectors_secret_leave_empty", lang=lang))
    return " ".join(part for part in parts if part)


def _render_mcp_preset_credential_fields(lang: str, prefix: str, preset: dict,
                                         existing: dict = None):
    """Render the credential fields of one MCP preset; return (config, secrets).

    The config comes entirely from the preset (plus its id); ``secrets``
    hold the non-empty credential inputs as env_pairs, empty inputs keep
    the stored values on edit.
    """
    if preset.get("description"):
        st.caption(str(preset["description"]))
    config = dict(preset.get("config") or {})
    config["preset"] = str(preset.get("id") or "")
    fields = config.get("secret_env_fields") or {}
    if isinstance(fields, dict) and fields:
        st.caption(t("connectors_mcp_credential_help", lang=lang))
    stored_names = _mcp_stored_env_names(existing or {})
    env_pairs = {}
    for field_id, meta in fields.items():
        if not isinstance(meta, dict):
            continue
        env_name = str(meta.get("env") or "").strip()
        if not env_name:
            continue
        value = st.text_input(
            str(meta.get("label") or env_name),
            type="password" if meta.get("password", True) else "default",
            key=f"{prefix}_mcp_secret_{field_id}",
            help=_mcp_field_help(meta, env_name in stored_names, lang),
        )
        if str(value or "").strip():
            env_pairs[env_name] = value
    secrets = {"env_pairs": env_pairs} if env_pairs else {}
    return config, secrets


def _render_custom_mcp_transport(lang: str, prefix: str,
                                 existing: dict = None, boxed: bool = True):
    """Render raw stdio/http transport fields; return (config, secrets).

    ``config`` collects command/args (stdio) or url/auth_header (http);
    ``secrets`` collect an optional one-line env_json secret, an empty
    input keeps the stored value on edit. ``boxed`` wraps the fields in an
    expanded "advanced mode" panel; the create form renders them flat.
    """
    cfg = (existing or {}).get("config")
    cfg = cfg if isinstance(cfg, dict) else {}
    if boxed:
        with st.expander(t("connectors_mcp_advanced", lang=lang), expanded=True):
            return _render_custom_mcp_transport_body(lang, prefix, cfg, existing)
    st.caption(t("connectors_mcp_advanced_help", lang=lang))
    return _render_custom_mcp_transport_body(lang, prefix, cfg, existing)


def _render_custom_mcp_transport_body(lang: str, prefix: str, cfg: dict,
                                      existing: dict):
    """Render the transport widgets themselves; return (config, secrets)."""
    transport = st.selectbox(
        t("connectors_mcp_transport", lang=lang),
        options=["stdio", "http"],
        index=0 if str(cfg.get("transport") or "stdio") != "http" else 1,
        key=f"{prefix}_mcp_transport",
    )
    if transport == "http":
        url = st.text_input(
            t("connectors_mcp_http_url", lang=lang),
            value=str(cfg.get("url") or ""),
            key=f"{prefix}_mcp_url",
            help=t("connectors_mcp_endpoint_help", lang=lang),
        )
        auth_header = st.text_input(
            t("connectors_mcp_auth_header", lang=lang),
            value=str(cfg.get("auth_header") or ""),
            key=f"{prefix}_mcp_auth_header",
            help=t("connectors_mcp_auth_header_help", lang=lang),
        )
        config = {"transport": "http", "url": url,
                  "auth_header": auth_header}
    else:
        command = st.text_input(
            t("connectors_mcp_command", lang=lang),
            value=str(cfg.get("command") or ""),
            key=f"{prefix}_mcp_command",
            help=t("connectors_mcp_command_help", lang=lang),
        )
        args_existing = cfg.get("args")
        args_lines = [str(a) for a in args_existing] if isinstance(
            args_existing, list) else []
        args_text = st.text_area(
            t("connectors_mcp_args", lang=lang),
            value="\n".join(args_lines),
            key=f"{prefix}_mcp_args",
            help=t("connectors_mcp_args_help", lang=lang),
        )
        config = {
            "transport": "stdio",
            "command": command,
            "args": [line.strip() for line in args_text.splitlines()
                     if line.strip()],
        }
    stored_env = _mcp_stored_env_json(existing or {})
    env_help = t("connectors_mcp_env_json_help", lang=lang)
    if stored_env:
        env_help += " " + t("connectors_secret_leave_empty", lang=lang)
    env_json = st.text_input(
        t("connectors_mcp_env_json", lang=lang),
        key=f"{prefix}_mcp_env_json",
        help=env_help,
    )
    secrets = {}
    if str(env_json or "").strip():
        secrets["env_json"] = env_json
    return config, secrets


# ─── full-page form views ────────────────────────────────────────────────────

def _form_header(lang: str, title: str, subtitle: str, back_tab: str,
                 back_key: str) -> None:
    """Render the back link, the form title and its subtitle."""
    if st.button("\u2190 " + t("connectors_back_btn", lang=lang),
                 key=back_key, type="secondary"):
        _back_to_list(back_tab)
    st.subheader(title)
    if subtitle:
        st.caption(subtitle)


def _create_target(lang: str, service: str, preset) -> tuple:
    """Return (icon, title, subtitle, back_tab) of the create form target."""
    if preset is not None:
        icon = str(preset.get("icon") or "").strip() or "\U0001f50c"
        name = str(preset.get("name") or preset.get("id") or "")
        return icon, name, t("connectors_mcp_preset_form_hint", lang=lang), _TAB_MCP
    if service == "mcp":
        return ("\U0001f50c", t("connectors_mcp_custom_title", lang=lang),
                t("connectors_mcp_custom_tile_help", lang=lang), _TAB_MCP)
    return (_service_icon(service), _service_label(service, lang),
            t("connectors_create_form_hint", lang=lang), _TAB_GENERAL)


def _render_create_view(lang: str) -> None:
    """Render the full-page create form (nothing is saved until Create)."""
    service = str(st.session_state.get(_CREATE_SERVICE_KEY) or "github_rest")
    preset = _mcp_preset_by_id(
        str(st.session_state.get(_CREATE_PRESET_KEY) or ""))
    icon, target, subtitle, back_tab = _create_target(lang, service, preset)
    title = f"{icon} {t('connectors_new_title', lang=lang)}".strip()
    _form_header(lang, title, f"{target} · {subtitle}".strip(" ·"),
                 back_tab, "connectors_create_back")

    default_name = str((preset or {}).get("name") or "")
    name = st.text_input(
        t("connectors_name", lang=lang), value=default_name,
        key="conn_new_name",
    )
    account = st.text_input(
        t("connectors_account", lang=lang), key="conn_new_account",
        help=t("connectors_account_help", lang=lang),
    )

    token = ""
    config, secrets = None, None
    if preset is not None:
        config, secrets = _render_mcp_preset_credential_fields(
            lang, "conn_new", preset)
    elif service == "ssh":
        config, secrets = _render_ssh_fields(lang, prefix="conn_new")
    elif service == "mcp":
        config, secrets = _render_custom_mcp_transport(
            lang, prefix="conn_new", boxed=False)
    else:
        token = st.text_input(
            t("connectors_token", lang=lang), type="password",
            key="conn_new_token", help=t("connectors_token_help", lang=lang),
        )

    st.markdown("---")
    c1, c2 = st.columns([1, 4])
    with c1:
        submitted = st.button(t("connectors_create_btn", lang=lang),
                              type="primary", key="conn_new_submit")
    with c2:
        if st.button(t("btn_cancel", lang=lang), key="conn_new_cancel"):
            _back_to_list(back_tab)

    if not submitted:
        return
    try:
        if preset is not None:
            # The preset supplies the config; the credentials typed above are
            # submitted together, so the connection is active from the start.
            connector_presets.instantiate_preset(
                str(preset.get("id") or ""), "mcp", name=name,
                account=account, secrets=secrets, pending=False,
            )
        else:
            create_connection(service, name, token, account=account,
                              config=config, secrets=secrets)
    except Exception as e:
        st.error(str(e))
        return
    flash_success(t("connectors_created", lang=lang))
    _back_to_list(_TAB_CONNECTIONS)


def _render_edit_view(lang: str) -> None:
    """Render the full-page edit form of one connection."""
    conn_id = str(st.session_state.get(_EDIT_ID_KEY) or "")
    conn = connectors.get_connection(conn_id) if conn_id else None
    if not conn:
        flash_error(t("connectors_not_found", lang=lang))
        _back_to_list(_TAB_CONNECTIONS)
        return
    service = str(conn.get("service") or "")
    icon = _service_icon(service)
    title = f"{icon} {t('connectors_edit_title', lang=lang)}".strip()
    _form_header(lang, title,
                 f"{str(conn.get('name') or conn_id)} · "
                 f"{_service_label(service, lang)}",
                 _TAB_CONNECTIONS, "connectors_edit_back")

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
            lang, prefix=f"conn_edit_{conn_id}", existing=conn)
    elif service == "mcp":
        preset = _mcp_preset_for(conn)
        if preset is not None:
            config, secrets = _render_mcp_preset_credential_fields(
                lang, f"conn_edit_{conn_id}", preset, existing=conn)
        else:
            config, secrets = _render_custom_mcp_transport(
                lang, prefix=f"conn_edit_{conn_id}", existing=conn)
    else:
        token = st.text_input(
            t("connectors_token", lang=lang), type="password",
            key=f"conn_edit_token_{conn_id}",
            help=t("connectors_token_leave_empty", lang=lang),
        )

    pending = str(conn.get("credentials_status") or "") == "pending"
    if pending:
        st.warning(t("connectors_needs_credentials", lang=lang))

    st.markdown("---")
    c1, c2 = st.columns([1, 4])
    with c1:
        saved = st.button(t("connectors_save", lang=lang), type="primary",
                          key=f"conn_edit_submit_{conn_id}")
    with c2:
        if st.button(t("btn_cancel", lang=lang), key=f"conn_edit_cancel_{conn_id}"):
            _back_to_list(_TAB_CONNECTIONS)

    if not saved:
        return
    try:
        update_connection(conn_id, name=name, account=account, token=token,
                          config=config, secrets=secrets)
    except Exception as e:
        st.error(str(e))
        return
    flash_success(t("connectors_saved", lang=lang))
    _back_to_list(_TAB_CONNECTIONS)


# ─── list view pieces ───────────────────────────────────────────────────────

def _render_connection_card(conn: dict, lang: str) -> None:
    """Render one connection as an expander card with actions."""
    conn_id = str(conn.get("id") or "")
    name = str(conn.get("name") or conn_id)
    svc_id = str(conn.get("service") or "?")
    account = str(conn.get("account") or "")
    has_token = bool(conn.get("has_token"))
    pending = str(conn.get("credentials_status") or "") == "pending"
    svc_label = _service_label(svc_id, lang)

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
    icon = _service_icon(svc_id)
    header = f"{icon} {name} · {svc_label} · {cred_mark}".strip()
    if account:
        header += f" · `{account}`"

    with st.expander(header, expanded=False):
        st.caption(f"id: `{conn_id}`")
        if pending:
            st.warning(t("connectors_needs_credentials", lang=lang))
        cfg = conn.get("config")
        if isinstance(cfg, dict) and cfg:
            host = str(cfg.get("host") or "")
            user = str(cfg.get("username") or "")
            target = f"{user}@{host}" if (user or host) else ""
            if target and cfg.get("port"):
                target += f":{cfg.get('port')}"
            if target:
                st.caption(target)
            elif cfg.get("url"):
                st.caption(str(cfg["url"]))
            elif cfg.get("command"):
                mcp_target = " ".join(
                    [str(cfg.get("command") or "")]
                    + [str(a) for a in (cfg.get("args") or [])]
                )
                st.caption(mcp_target)
        if conn.get("created_at"):
            st.caption(t("connectors_created_at", lang=lang,
                         date=str(conn.get("created_at"))))

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(t("connectors_test_btn", lang=lang),
                         key=f"conn_test_{conn_id}",
                         use_container_width=True):
                _test_connection(conn_id, lang, connection=conn)
        with c2:
            edit_label = (t("connectors_fill_credentials_btn", lang=lang)
                          if pending else t("connectors_edit_btn", lang=lang))
            if st.button(edit_label, key=f"conn_edit_btn_{conn_id}",
                         use_container_width=True):
                _start_edit(conn_id)
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
                            flash_success(t("connectors_deleted", lang=lang))
                            _back_to_list(_TAB_CONNECTIONS)
                        else:
                            st.error(t("connectors_delete_error", lang=lang))
                with del_cols[1]:
                    if st.button(t("btn_cancel", lang=lang),
                                 key=f"conn_del_no_{conn_id}",
                                 use_container_width=True):
                        st.session_state[confirm_key] = None
                        st.rerun()

        # Working notes of this connection: limits discovered in practice,
        # paths and rules. Written through the personal-memory layer so the
        # text is screened for credentials before the connector API stores it.
        st.markdown("---")
        st.markdown(f"**{t('connectors_memory_title', lang=lang)}**")
        st.caption(t("connectors_memory_desc", lang=lang))
        mem_scope = pm.format_scope(pm.SCOPE_CONNECTOR, conn_id)
        try:
            mem = pm.read_memory(mem_scope)
        except Exception:
            mem = {"exists": False, "body": ""}
        mem_text = st.text_area(
            t("connectors_memory_title", lang=lang),
            value=str(mem.get("body") or ""),
            height=160,
            key=f"conn_memory_{conn_id}",
            label_visibility="collapsed",
        )
        if st.button(t("connectors_memory_save_btn", lang=lang),
                     key=f"conn_memory_save_{conn_id}"):
            try:
                pm.write_memory(mem_scope, mem_text, updated_by="ui")
            except Exception as exc:
                st.error(f"{t('connectors_memory_save_error', lang=lang)} {exc}")
            else:
                flash_success(t("connectors_memory_saved", lang=lang))
                st.rerun()


def _render_connections(lang: str, connections: list) -> None:
    """Render the connection cards of the list."""
    st.markdown("---")
    for conn in connections:
        _render_connection_card(conn, lang)


def _render_preset_tile(lang: str, preset: dict) -> None:
    """Render one MCP preset tile; a click opens the create form."""
    preset_id = str(preset.get("id") or "")
    icon = str(preset.get("icon") or "").strip() or "\U0001f50c"
    label = f"{icon} {preset.get('name') or preset_id}".strip()
    if st.button(label, key=f"mcp_preset_tile_{preset_id}",
                 use_container_width=True):
        _start_create("mcp", preset_id)
    if preset.get("description"):
        st.caption(str(preset["description"]))


def _render_custom_tile(lang: str) -> None:
    """Render the custom-server tile; a click opens the raw create form."""
    if st.button(t("connectors_mcp_custom_tile", lang=lang),
                 key="mcp_custom_tile", use_container_width=True):
        _start_create("mcp")
    st.caption(t("connectors_mcp_custom_tile_help", lang=lang))


def _render_mcp_tab(lang: str) -> None:
    """Render the MCP tab: preset tiles and the custom-server tile."""
    tiles = [dict(p) for p in _mcp_preset_options()] + [None]
    for row_start in range(0, len(tiles), _MCP_TILES_PER_ROW):
        row = tiles[row_start:row_start + _MCP_TILES_PER_ROW]
        cols = st.columns(len(row))
        for col, tile in zip(cols, row):
            with col:
                if tile is not None:
                    _render_preset_tile(lang, tile)
                else:
                    _render_custom_tile(lang)


def _render_list_view(lang: str) -> None:
    """Render the LIST view: three stateful tabs."""
    labels = _conn_tab_labels(lang)
    default_label = _apply_target_tab(lang)
    tabs_kwargs = {"key": CONNECTORS_TABS_KEY, "on_change": "rerun"}
    if default_label:
        tabs_kwargs["default"] = default_label
    tab_general, tab_mcp, tab_connections = st.tabs(labels, **tabs_kwargs)

    with tab_general:
        tiles = _general_service_tiles(lang)
        if tiles:
            _render_general_service_tiles(lang, tiles)
        else:
            st.warning(t("connectors_no_services", lang=lang))
    with tab_mcp:
        _render_mcp_tab(lang)
    with tab_connections:
        connections = list_connections()
        if not connections:
            st.info(t("connectors_empty", lang=lang))
        else:
            _render_connections(lang, connections)


def page_connectors() -> None:
    """Connections management page (dispatched from ui.app)."""
    lang = st.session_state.get("ui_lang", "en")
    view = str(st.session_state.get(VIEW_KEY) or VIEW_LIST)
    st.title(t("page_connectors_title", lang=lang))
    if view == VIEW_LIST:
        st.markdown(t("connectors_page_desc", lang=lang))
        _render_list_view(lang)
    elif view == VIEW_CREATE:
        _render_create_view(lang)
    elif view == VIEW_EDIT:
        _render_edit_view(lang)
    else:
        # An unknown view (stale session) falls back to the list.
        st.session_state[VIEW_KEY] = VIEW_LIST
        st.markdown(t("connectors_page_desc", lang=lang))
        _render_list_view(lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
