"""
ui.pages.settings - API key configuration with per-service forms.
Uses st.session_state.draft_cfg to retain edits across rerenders.
Each service gets its own st.form with test + save buttons side by side.
Now supports extra_fields (select/text) per service definition.

Environment-variable section - shows SAGAAI_<SVC>_KEY status and instructions.
If a key is set via env var, the input field is hidden and replaced with a message.
Such keys are not persisted to the database on save.

Note: DevAgent settings, economy mode and instructions have been moved
into the DevAgent / Orchestrator page (ui.pages.orchestrator).

Saving a provider form shows a success message directly inside that
provider's expanded block, under the Save / Test buttons, so the user sees
exactly which provider was saved.

A folders-first sync section at the bottom reloads assistants, orchestrators
and instructions from the DATA_DIR folders into the DB cache. Use it after
editing entity files directly on disk.

All user-facing strings go through t(key, lang=lang).
"""
import streamlit as st

from core.i18n import t
from core.config import load_config, save_config, has_key, list_env_keys, is_env_key_set_for_service
from core.services import get_services
from core.api_layer import test_connection


def _resolve_label(label, lang, fallback):
    """Return translated label string.
    label can be a plain string or a dict like {"en": "...", "ru": "..."}.
    """
    if isinstance(label, dict):
        return label.get(lang, label.get("en", fallback))
    return label or fallback


def _render_extra_fields(svc, svc_name, draft, lang):
    """Render extra_fields defined in the service JSON.
    Returns a dict of {field_key: widget_value} for the current form.
    """
    extra = svc.get("extra_fields", [])
    if not extra:
        return {}

    st.markdown("---")
    st.caption(t("settings_extra_params", lang=lang))

    values = {}
    for field in extra:
        fkey = field["key"]
        # Reasoning effort is configured per-assistant and per-orchestrator,
        # not as a global service setting - skip it here.
        if fkey == "reasoning_effort":
            continue
        cfg_key = f"{svc_name}_{fkey}"
        default = field.get("default", "")
        label = _resolve_label(field.get("label", fkey), lang, fkey)
        help_text = _resolve_label(field.get("tooltip", {}), lang, None)

        current_val = draft.get(cfg_key, default)

        ftype = field.get("type", "text")
        if ftype == "select":
            options = field.get("options", [])
            # ensure current_val is in options (fallback to first)
            if current_val not in options:
                current_val = options[0] if options else ""
            values[fkey] = st.selectbox(
                label,
                options=options,
                index=options.index(current_val) if current_val in options else 0,
                key=f"cfg_{cfg_key}",
                help=help_text,
            )
        else:  # text
            values[fkey] = st.text_input(
                label,
                value=str(current_val),
                key=f"cfg_{cfg_key}",
                help=help_text,
            )
    st.markdown("---")
    return values


def _render_env_variables_section(lang):
    """Render a help section about environment-variable API keys."""
    st.header(t("settings_env_header", lang=lang))
    st.markdown(t("settings_env_desc", lang=lang))

    st.caption(t("settings_env_macos_linux", lang=lang))
    st.code(
        'export SAGAAI_DEEPSEEK_KEY="sk-..."\n'
        'export SAGAAI_GIGACHAT_KEY="your-key"\n'
        'export SAGAAI_YANDEXAI_KEY="API-key"\n'
        'export SAGAAI_YANDEXAI_KEY2="folder-id"',
        language="bash",
    )
    st.caption(t("settings_env_windows", lang=lang))
    st.code(
        'setx SAGAAI_DEEPSEEK_KEY "sk-..."\n'
        'setx SAGAAI_GIGACHAT_KEY "your-key"\n'
        'setx SAGAAI_YANDEXAI_KEY "API-key"\n'
        'setx SAGAAI_YANDEXAI_KEY2 "folder-id"',
        language="shell",
    )
    st.markdown(t("settings_env_restart", lang=lang))

    if "env_keys_checked" not in st.session_state:
        st.session_state["env_keys_checked"] = False

    if st.button(t("settings_env_check_btn", lang=lang), key="check_env_keys"):
        st.session_state["env_keys_checked"] = True

    if st.session_state["env_keys_checked"]:
        info = list_env_keys()
        if not info:
            st.info(t("settings_env_no_services", lang=lang))
            return

        st.markdown("---")
        st.subheader(t("settings_env_status", lang=lang))
        for svc_name, data in info.items():
            status_parts = []
            for ev in data["env_keys"]:
                icon = "\u2705" if ev["set"] else "\u274c"
                status_parts.append(f"{icon} `{ev['var']}`")
            st.markdown(f"**{svc_name}**: {' \u00b7 '.join(status_parts)}")
            if data["env_wins"]:
                st.info(
                    t("settings_env_priority", lang=lang, service=svc_name)
                )
        st.caption(t("settings_env_priority_footer", lang=lang))


def _render_models_table(svc: dict, lang: str) -> None:
    """Render a compact table of available models with context window and max_tokens."""
    models = svc.get("models", [])
    if not models:
        return

    # Build rows: model ID, context_window, max_tokens
    rows = []
    for m in models:
        if isinstance(m, dict):
            mid = m.get("id", "?")
            ctx = m.get("context_window")
            mt = m.get("max_tokens")
        else:
            mid = str(m)
            ctx = None
            mt = None

        ctx_str = f"{ctx:,}" if ctx else "-"
        mt_str = f"{mt:,}" if mt else "-"
        rows.append((mid, ctx_str, mt_str))

    if not rows:
        return

    # Compact single-line captions per model
    label_cw = t("settings_table_ctx", lang=lang)
    label_mt = t("settings_table_max_tok", lang=lang)
    header = f"| Model | {label_cw} | {label_mt} |"
    sep    = "|---|---|---|"
    lines = [header, sep]
    for mid, ctx_str, mt_str in rows:
        lines.append(f"| `{mid}` | {ctx_str} | {mt_str} |")
    st.markdown("\n".join(lines))


def _render_api_keys(lang):
    """Render the API keys configuration."""
    # Use a session-state draft so edits survive intermediate rerenders
    if "draft_cfg" not in st.session_state:
        st.session_state.draft_cfg = load_config()
    draft = st.session_state.draft_cfg

    services = get_services()
    if not services:
        st.info(t("settings_no_services", lang=lang))
        return

    # --- Environment variables section --------------------------------------
    _render_env_variables_section(lang)
    st.markdown("---")

    for svc_name, svc in services.items():
        key1_label = svc.get("key_label", "API Key")
        key2_label = svc.get("key2_label", "")
        key1_field = svc.get("config_key", "")
        key2_field = svc.get("config_key2", "")
        key1_help = _resolve_label(svc.get("key_help"), lang, None)
        key2_help = _resolve_label(svc.get("key2_help"), lang, None)

        # Check if keys are set via environment variables
        key1_from_env = bool(key1_field) and is_env_key_set_for_service(svc_name, "config_key")
        key2_from_env = bool(key2_field) and is_env_key_set_for_service(svc_name, "config_key2")

        with st.expander(f"\U0001f527 {svc_name}", expanded=not has_key(svc)):
            # Per-service form to isolate widget state
            with st.form(key=f"settings_form_{svc_name}", clear_on_submit=False):
                # --- Key 1 -------------------------------------------------------
                if key1_from_env:
                    st.markdown(f"{t('settings_key_from_env', lang=lang)}")
                    val1 = draft.get(key1_field, "")
                else:
                    val1 = st.text_input(
                        key1_label,
                        value=draft.get(key1_field, ""),
                        type="password",
                        key=f"cfg_{key1_field}",
                        help=key1_help,
                    )

                # --- Key 2 (optional) --------------------------------------------
                val2 = ""
                if key2_field and key2_label:
                    if key2_from_env:
                        st.markdown(f"{t('settings_key_from_env', lang=lang)}")
                        val2 = draft.get(key2_field, "")
                    else:
                        val2 = st.text_input(
                            key2_label,
                            value=draft.get(key2_field, ""),
                            key=f"cfg_{key2_field}",
                            help=key2_help,
                        )

                # --- Models table (context window + max_tokens) ------------------
                _render_models_table(svc, lang)

                # Render extra_fields (e.g., deepseek thinking/reasoning)
                extra_vals = _render_extra_fields(svc, svc_name, draft, lang)

                # Two buttons side by side: Test Connection | Save
                col_test, col_save = st.columns([2, 2])

                with col_test:
                    test_clicked = st.form_submit_button(
                        t("btn_test_conn", lang=lang),
                        use_container_width=True,
                    )
                with col_save:
                    save_clicked = st.form_submit_button(
                        t("btn_save_settings", lang=lang),
                        type="primary",
                        use_container_width=True,
                        key=f"settings_save_{svc_name}",
                    )

                if save_clicked:
                    # Persist the current form values into draft
                    # Only save key1/key2 if they are NOT set via environment variable
                    if not key1_from_env:
                        draft[key1_field] = val1
                    if key2_field and key2_label and not key2_from_env:
                        draft[key2_field] = val2
                    # Save extra_fields (they are never set via env vars)
                    for fkey, fval in extra_vals.items():
                        draft[f"{svc_name}_{fkey}"] = fval
                    if save_config(dict(draft)):
                        st.session_state.draft_cfg = dict(draft)
                        # Show confirmation directly under this provider's buttons.
                        st.success(t("settings_saved", lang=lang))

                if test_clicked:
                    # Build a config dict from the form values (may not be saved yet)
                    test_cfg = dict(draft)
                    test_cfg[key1_field] = val1
                    if key2_field and key2_label:
                        test_cfg[key2_field] = val2
                    # Include extra_fields in test config
                    for fkey, fval in extra_vals.items():
                        test_cfg[f"{svc_name}_{fkey}"] = fval
                    with st.spinner(t("testing_conn", lang=lang)):
                        ok, msg = test_connection(svc_name, test_cfg)
                    if ok:
                        st.success(t("settings_tested_ok", lang=lang, msg=msg))
                    else:
                        st.error(t("settings_tested_fail", lang=lang, msg=msg))


def _render_folder_sync(lang):
    """Render the folders-to-DB sync section (folders-first model).

    Reloads assistants, orchestrators and instructions from the DATA_DIR
    folders into the DB cache. Useful after editing entity files directly
    on disk (e.g. with DevAgent or an external editor).
    """
    from core.entity_sync import ensure_entity_folders_sync

    st.markdown("---")
    st.subheader(t("sync_folders_btn", lang=lang))
    st.caption(t("sync_folders_desc", lang=lang))
    if st.button(t("sync_folders_btn", lang=lang), key="sync_folders_button"):
        try:
            result = ensure_entity_folders_sync()
            assistants = result.get("assistants", {})
            orchestrators = result.get("orchestrators", {})
            summary_parts = []
            if assistants:
                summary_parts.append(f"assistants: {len(assistants)}")
            if orchestrators:
                summary_parts.append(f"orchestrators: {len(orchestrators)}")
            st.success(
                t("sync_folders_done", lang=lang,
                  summary=", ".join(summary_parts) if summary_parts else "ok")
            )
        except Exception as exc:
            st.error(t("sync_folders_error", lang=lang, error=str(exc)))


def page_settings():
    """Settings page - API keys configuration + folders-first sync.

    DevAgent/Orchestrator settings (models, prompt, economy, instructions,
    export/import) have been moved into the orchestrator page itself
    (ui.pages.orchestrator). The folders sync section reloads entity
    content from DATA_DIR folders into the DB.
    """
    lang = st.session_state.get("ui_lang")
    st.title(f"\u2699\ufe0f {t('nav_settings', lang=lang)}")
    _render_api_keys(lang)
    _render_folder_sync(lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
