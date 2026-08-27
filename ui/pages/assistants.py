"""
ui/pages/assistants.py - Page to manage assistants (system prompts, config, files).
Now includes max_tokens field with dynamic limits from service definitions.

Session-state keys use the "assistant" naming. For backward compatibility the
legacy "skill" keys (show_skill_form / edit_skill_id) are also read and
mirrored when present.
"""
import streamlit as st
from datetime import datetime
from core.assistants import (
    load_assistants_index, get_assistant_by_id,
    create_assistant, update_assistant, delete_assistant,
    list_assistant_files, save_assistant_file, delete_assistant_file,
    load_assistant_prompt_text,
)
from core.prompt_improver import improve_prompt_with_weak_model
from core.i18n import t
from core.services import get_services, service_supports_reasoning_effort, default_reasoning_effort, get_model_reasoning_effort_options
from core.tools_utils import (
    list_tool_definitions, service_supported_tools, build_rag_search_tool,
)
from core.rag import list_bases as list_rag_bases
from core.assistant_folders import (
    load_assistant_bundle as folder_load_bundle,
    set_assistant_rag_bases,
    set_assistant_web_search_settings,
    get_assistant_web_search_settings,
)

# built-in assistant IDs that should never be editable
_BUILTIN_ASSISTANT_IDS = {"dev_agent", "skill_creator", "assistant_creator"}

# ─── Session-state helpers with legacy fallback ────────────────────────────

def _get_show_form() -> bool:
    return bool(
        st.session_state.get("show_assistant_form", False)
        or st.session_state.get("show_skill_form", False)
    )


def _set_show_form(value: bool) -> None:
    st.session_state["show_assistant_form"] = value
    st.session_state["show_skill_form"] = value


def _get_edit_id():
    return st.session_state.get("edit_assistant_id") or st.session_state.get("edit_skill_id")


def _set_edit_id(value) -> None:
    st.session_state["edit_assistant_id"] = value
    st.session_state["edit_skill_id"] = value


def _format_tools_badge(tools) -> str:
    """Human-readable list of an assistant's tools.

    Handles both plain string tool names (e.g. ``web_search``) and dict
    tool definitions (e.g. Responses function tools) - a dict is shown
    as its ``name`` (function tools) or its ``type`` otherwise.
    """
    if not tools:
        return ""
    parts = []
    for t in tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("type") or "tool"
            parts.append(str(name))
        else:
            parts.append(str(t))
    return ", ".join(parts)


def _get_model_max_tokens_limit(svc_def: dict, model_id: str) -> int:
    """Return the max_tokens limit for a given service definition and model.

    Priority:
      1. Model's "max_tokens" field.
      2. Service-level "max_tokens_default".
      3. Fallback: 200000 (previous hardcoded value).
    """
    if svc_def:
        for m in svc_def.get("models", []):
            if isinstance(m, dict) and m.get("id") == model_id:
                mt = m.get("max_tokens")
                if mt:
                    return int(mt)
        svc_default = svc_def.get("max_tokens_default")
        if svc_default:
            return int(svc_default)
    return 200000


def _temperature_bounds(svc_def: dict) -> tuple:
    """Return (min, max, step) for the temperature slider from a service definition.

    Falls back to 0.0..2.0 (step 0.05) for services without explicit bounds,
    preserving the old hardcoded behaviour.
    """
    t_min = float(svc_def.get("temp_min", 0.0))
    t_max = float(svc_def.get("temp_max", 2.0))
    t_step = float(svc_def.get("temp_step", 0.05))
    if t_min >= t_max:
        t_min, t_max = 0.0, 2.0
    return t_min, t_max, t_step


def _clamp_temperature(value: float, svc_def: dict) -> float:
    """Clamp a temperature value into the service definition's valid range."""
    t_min, t_max, _ = _temperature_bounds(svc_def)
    return max(t_min, min(t_max, float(value)))


def page_assistants() -> None:
    """Assistants management page."""
    lang = st.session_state.get("ui_lang", "en")
    st.title(t("skills_title", lang=lang))

    # Back to chat button (mirrors orchestrator settings page)
    _, right_col = st.columns([4, 1])
    with right_col:
        if st.button("← " + t("orch_tab_chat", lang=lang),
                     key="assistant_back_chat",
                     use_container_width=True,
                     type="secondary"):
            aid = (
                st.session_state.get("edit_assistant_id")
                or st.session_state.get("edit_skill_id")
                or st.session_state.get("last_active_entity_id")
                or st.session_state.get("selected_assistant_id")
                or st.session_state.get("selected_skill_id")
            )
            if aid:
                st.session_state["last_active_entity_type"] = "assistant"
                st.session_state["last_active_entity_id"] = aid
                st.session_state["selected_assistant_id"] = aid
                st.session_state["selected_skill_id"] = aid
            st.session_state["current_page"] = "run"
            st.rerun()

    available_services = get_services()
    service_names = list(available_services.keys())
    tool_defs = list_tool_definitions()

    # ─── Form inline ─────────────────────────────────────────────────────────
    if _get_show_form():
        edit_id = _get_edit_id()
        editing = bool(edit_id)

        # Reset prompt revision when the form is opened for a different target.
        # This avoids stale session-state values from a previous assistant/form.
        if st.session_state.get("assistant_prompt_revision_for") != edit_id:
            # Drop stale draft keys from a previous assistant/form so they
            # do not accumulate in the session forever.
            for _k in [k for k in list(st.session_state.keys())
                       if k.startswith("assistant_prompt_text_")]:
                st.session_state.pop(_k, None)
            st.session_state["assistant_prompt_revision"] = 0
            st.session_state["assistant_prompt_revision_for"] = edit_id
        prompt_revision = st.session_state.get("assistant_prompt_revision", 0)
        prompt_key = f"assistant_prompt_text_{prompt_revision}"

        if editing:
            existing = get_assistant_by_id(edit_id)
            if existing:
                st.subheader(t("skills_form_title_edit", lang=lang))
                default_name        = existing.get("name", "")
                default_service     = existing.get("service", "")
                default_model       = existing.get("model", "")
                default_temp        = existing.get("temperature", 0.7)
                default_desc        = existing.get("description", "")
                default_text        = existing.get("text", "")
                default_tools       = existing.get("tools", [])
                default_max_calls   = existing.get("max_tool_calls")
                default_max_tokens  = existing.get("max_tokens")
                default_reasoning   = existing.get("reasoning_effort")
                default_ws_settings = {}
                try:
                    if existing.get("slug"):
                        default_ws_settings = (
                            get_assistant_web_search_settings(existing["slug"]) or {}
                        )
                except Exception:
                    default_ws_settings = {}
            else:
                st.error(t("skill_not_found", lang=lang))
                _set_show_form(False)
                _set_edit_id(None)
                st.rerun()
        else:
            st.subheader(t("skills_form_title_create", lang=lang))
            default_name        = ""
            default_service     = service_names[0] if service_names else ""
            default_model       = ""
            default_temp        = None
            default_desc        = ""
            default_text        = ""
            default_tools       = []
            default_max_calls   = None
            default_max_tokens  = None
            default_reasoning   = None
            default_ws_settings = {}

        if not service_names:
            st.error(t("no_services", lang=lang))
            return

        # --- Basic fields (name, description, prompt first) -------------------
        name = st.text_input(
            t("skills_form_name", lang=lang), value=default_name, key="assistant_name_input",
            help=t("skills_form_name_help", lang=lang),
        )
        desc = st.text_area(
            t("skills_form_desc", lang=lang), value=default_desc, key="assistant_desc",
            help=t("skills_form_desc_help", lang=lang),
        )

        # --- Prompt text ---
        prompt_text = st.text_area(
            t("prompt_text", lang=lang),
            value=default_text,
            height=300,
            key=prompt_key,
            help=t("prompt_text_help", lang=lang),
        )
        if st.button(t("improve_prompt", lang=lang), key="assistant_improve"):
            if not prompt_text.strip():
                st.warning(t("err_prompt_required", lang=lang))
            else:
                try:
                    with st.spinner(t("improve_prompt_running", lang=lang)):
                        improved = improve_prompt_with_weak_model(
                            prompt_text, lang=lang
                        )
                except Exception as exc:
                    st.error(t("improve_prompt_error", lang=lang, error=str(exc)))
                else:
                    new_rev = prompt_revision + 1
                    st.session_state[f"assistant_prompt_text_{new_rev}"] = improved
                    # Keep only the newest draft to avoid session leaks.
                    for _k in [k for k in list(st.session_state.keys())
                               if k.startswith("assistant_prompt_text_")
                               and k != f"assistant_prompt_text_{new_rev}"]:
                        st.session_state.pop(_k, None)
                    st.session_state["assistant_prompt_revision"] = new_rev
                    st.success(t("improve_prompt_ok", lang=lang))
                    st.rerun()

        st.markdown("---")

        # --- Saved model availability warning -------------------------------
        if editing and existing:
            saved_svc = existing.get("service", "")
            if saved_svc and saved_svc not in service_names:
                st.warning(
                    t("assistant_service_unavailable", lang=lang, service=saved_svc)
                )
            elif saved_svc in service_names:
                saved_models = [
                    m.get("id") if isinstance(m, dict) else m
                    for m in available_services.get(saved_svc, {}).get("models", [])
                ]
                if existing.get("model") and existing["model"] not in saved_models:
                    st.warning(
                        t("assistant_model_unavailable", lang=lang,
                          model=existing["model"], service=saved_svc)
                    )

        # --- Service / Model ---
        svc_idx = 0
        if default_service in service_names:
            svc_idx = service_names.index(default_service)
        service = st.selectbox(
            t("service", lang=lang), service_names, index=svc_idx,
            key="assistant_service",
            help=t("service_help", lang=lang),
        )

        svc_slug = service
        svc_info = available_services.get(svc_slug, {})
        model_list = []
        for m in svc_info.get("models", []):
            m_id = m["id"] if isinstance(m, dict) else m
            m_name = m.get("name", m_id) if isinstance(m, dict) else m
            model_list.append((m_id, m_name))
        model_ids = [m_id for m_id, _ in model_list]
        default_model_idx = 0
        if default_model and default_model in model_ids:
            default_model_idx = model_ids.index(default_model)
        model_display = [f"{name} ({m_id})" for m_id, name in model_list]
        model_sel = st.selectbox(
            t("model", lang=lang),
            range(len(model_list)),
            format_func=lambda i: model_display[i] if i < len(model_display) else "-",
            index=default_model_idx,
            key="assistant_model",
            help=t("model_help", lang=lang),
        )
        model = model_ids[model_sel] if model_list else ""

        t_min, t_max, t_step = _temperature_bounds(svc_info)
        if default_temp is None:
            default_temp = float(svc_info.get("temp_default", 0.7))
        temperature = _clamp_temperature(
            st.slider(t("temperature", lang=lang), t_min, t_max,
                      value=float(default_temp), step=t_step,
                      key="assistant_temp",
                      help=t("temperature_help", lang=lang)),
            svc_info,
        )

        # --- Reasoning effort (only for services that support it) ------------
        reasoning_effort = ""
        if service_supports_reasoning_effort(svc_info):
            reasoning_options = get_model_reasoning_effort_options(svc_info, model)
            if not default_reasoning:
                default_reasoning = default_reasoning_effort(svc_info, strong=False, model=model) or "high"
            if default_reasoning not in reasoning_options and reasoning_options:
                default_reasoning = reasoning_options[0]
            reasoning_effort = st.selectbox(
                t("reasoning_effort", lang=lang),
                options=reasoning_options,
                index=reasoning_options.index(default_reasoning) if default_reasoning in reasoning_options else 0,
                key="assistant_reasoning_effort",
                help=t("reasoning_effort_help", lang=lang),
            )

        # --- Tools (filtered by provider capabilities from services/*.json) ---
        allowed_tools = service_supported_tools(svc_info, tool_defs)
        if allowed_tools:
            tools_selected = st.multiselect(
                t("tools", lang=lang),
                options=allowed_tools,
                default=[t_name for t_name in default_tools if t_name in allowed_tools],
                key=f"assistant_tools_{service}",
                help=t("tools_help", lang=lang),
            )
        else:
            tools_selected = []
            st.caption(t("tools_not_supported", lang=lang))
        max_tool_calls = st.number_input(
            t("skill_max_tool_calls_label", lang=lang),
            min_value=1, max_value=50, step=1,
            value=default_max_calls or 3,
            key="assistant_max_calls",
            help=t("assistant_max_tool_calls_help", lang=lang),
        )

        # --- max_tokens (dynamic limit from service) ---
        _max_tokens_limit = _get_model_max_tokens_limit(svc_info, model)
        _cur_max_tokens = default_max_tokens if default_max_tokens is not None else 4096
        if _cur_max_tokens > _max_tokens_limit:
            _cur_max_tokens = _max_tokens_limit
        max_tokens = st.number_input(
            t("max_tokens", lang=lang),
            min_value=1, max_value=_max_tokens_limit, step=1,
            value=_cur_max_tokens,
            key="assistant_max_tokens",
            help=t("max_tokens_help", lang=lang),
        )

        # --- Knowledge bases (auto-RAG) ---
        st.markdown("### " + t("assistant_rag_bases", lang=lang))
        rag_bases = list_rag_bases()
        base_options = [b.get("slug", "") for b in rag_bases]
        base_labels = [
            f"{b.get('name') or b.get('slug')} ({b.get('status') or 'draft'})"
            for b in rag_bases
        ]
        if base_options:
            default_bases = []
            if editing:
                try:
                    bundle = folder_load_bundle(existing.get("slug") or "") or {}
                    default_bases = [
                        str(x).strip().lower()
                        for x in (bundle.get("rag_bases") or [])
                        if str(x).strip()
                    ]
                except Exception:
                    default_bases = []
            sel_indices = [
                i for i, s in enumerate(base_options)
                if s in default_bases
            ]
            bases_selected = st.multiselect(
                t("assistant_rag_bases", lang=lang),
                options=range(len(base_options)),
                default=sel_indices,
                format_func=lambda i: base_labels[i] if i < len(base_labels) else "-",
                key="assistant_rag_bases_sel",
                help=t("assistant_rag_bases_help", lang=lang),
            )
        else:
            bases_selected = []
            st.caption(t("assistant_rag_bases_empty", lang=lang))

        # --- Web-search settings (per-assistant overrides) --------------------
        if svc_info.get("auth_type") == "yandex_iam":
            st.markdown("### " + t("assistant_web_search_section", lang=lang))
            ws_ctx_cur = default_ws_settings.get("context_size") or ""
            ctx_options = ["", "low", "medium", "high"]
            if ws_ctx_cur not in ctx_options:
                ws_ctx_cur = ""
            web_search_context = st.selectbox(
                t("assistant_web_search_context", lang=lang),
                options=ctx_options,
                index=ctx_options.index(ws_ctx_cur),
                format_func=lambda v: (
                    t("web_search_context_auto", lang=lang)
                    if v == "" else v.capitalize()
                ),
                key="assistant_ws_context",
                help=t("assistant_web_search_context_help", lang=lang),
            )
            default_ws_domains = ", ".join(
                str(d) for d in (default_ws_settings.get("allowed_domains") or [])
            )
            web_search_domains = st.text_input(
                t("assistant_web_search_domains", lang=lang),
                value=default_ws_domains,
                key="assistant_ws_domains",
                placeholder=t("assistant_web_search_domains_placeholder", lang=lang),
                help=t("assistant_web_search_domains_help", lang=lang),
            )
        else:
            web_search_context = ""
            web_search_domains = ""

        # --- Files ---
        st.markdown("### " + t("skill_files", lang=lang))
        if editing:
            files = list_assistant_files(edit_id)
            if files:
                for fname in files:
                    c1, c2 = st.columns([6, 1])
                    with c1:
                        st.write(fname)
                    with c2:
                        if st.button(t("delete_file", lang=lang), key=f"del_assistant_file_{fname}"):
                            delete_assistant_file(edit_id, fname)
                            st.success(t("deleted_ok", lang=lang))
                            st.rerun()
            else:
                st.caption(t("no_files", lang=lang))
            uploaded_files = st.file_uploader(
                t("add_files", lang=lang),
                accept_multiple_files=True,
                key="assistant_file_uploader",
                help=t("add_files_help", lang=lang),
            )
            if uploaded_files:
                if st.button(t("btn_add_files", lang=lang), key="assistant_add_files"):
                    added = 0
                    for uf in uploaded_files:
                        try:
                            content = uf.getvalue().decode("utf-8")
                            save_assistant_file(edit_id, uf.name, content)
                            added += 1
                        except Exception as e:
                            st.error(t("skill_file_error", lang=lang, name=uf.name, error=str(e)))
                    if added:
                        st.success(t("files_added", lang=lang, count=added))
                        st.rerun()
        else:
            st.info(t("save_skill_first", lang=lang))

        # --- Buttons ---
        c1, c2 = st.columns([1, 4])
        with c1:
            if st.button(t("btn_save", lang=lang), type="primary", key="assistant_save_btn"):
                if not name.strip():
                    st.error(t("err_name_required", lang=lang))
                elif not prompt_text.strip():
                    st.error(t("err_prompt_required", lang=lang))
                else:
                    effort_value = reasoning_effort or None
                    rag_ids = [
                        base_options[i] for i in bases_selected
                        if 0 <= i < len(base_options)
                    ]
                    # Auto-attach the native rag_search function tool for
                    # yandex_iam assistants with bound bases; remove it when
                    # no bases remain selected.
                    final_tools = list(tools_selected or [])
                    if svc_info.get("auth_type") == "yandex_iam":
                        has_rag_tool = any(
                            isinstance(t, dict)
                            and t.get("type") == "function"
                            and t.get("name") == "rag_search"
                            for t in final_tools
                        )
                        if rag_ids and not has_rag_tool:
                            final_tools.append(build_rag_search_tool(rag_ids))
                        elif not rag_ids:
                            final_tools = [
                                t for t in final_tools
                                if not (
                                    isinstance(t, dict)
                                    and t.get("type") == "function"
                                    and t.get("name") == "rag_search"
                                )
                            ]
                    if editing:
                        update_assistant(edit_id, name, service, model,
                                         temperature, prompt_text, desc,
                                         tools=final_tools,
                                         max_tool_calls=max_tool_calls,
                                         max_tokens=max_tokens if max_tokens else None,
                                         reasoning_effort=effort_value)
                        slug = (existing or {}).get("slug")
                        if slug:
                            set_assistant_rag_bases(slug, rag_ids)
                            set_assistant_web_search_settings(
                                slug,
                                context_size=web_search_context or None,
                                allowed_domains=web_search_domains,
                            )
                    else:
                        new_id = create_assistant(name, service, model, temperature,
                                                  prompt_text, desc,
                                                  tools=final_tools,
                                                  max_tool_calls=max_tool_calls,
                                                  max_tokens=max_tokens if max_tokens else None,
                                                  reasoning_effort=effort_value)
                        if new_id:
                            new_full = get_assistant_by_id(new_id) or {}
                            new_slug = new_full.get("slug")
                            if new_slug:
                                set_assistant_rag_bases(new_slug, rag_ids)
                                set_assistant_web_search_settings(
                                    new_slug,
                                    context_size=web_search_context or None,
                                    allowed_domains=web_search_domains,
                                )
                    _set_show_form(False)
                    _set_edit_id(None)
                    st.success(t("skills_saved", lang=lang))
                    st.rerun()
        with c2:
            if st.button(t("btn_cancel", lang=lang), key="assistant_cancel_btn"):
                _set_show_form(False)
                _set_edit_id(None)
                st.rerun()
        return

    # ─── Main list ────────────────────────────────────────────────────────────
    if st.button(t("skills_create_btn", lang=lang), type="primary", key="assistant_create_btn"):
        if not available_services:
            st.warning(t("no_services_short", lang=lang))
        else:
            _set_show_form(True)
            _set_edit_id(None)
            st.rerun()

    assistants = [s for s in load_assistants_index() if s.get("id") not in _BUILTIN_ASSISTANT_IDS]
    if not assistants:
        st.info(t("skills_empty", lang=lang))
        return

    for p in assistants:
        with st.container(border=True):
            c_info, c_edit, c_del = st.columns([7, 1, 1])
            with c_info:
                created  = datetime.fromisoformat(p["created_at"]).strftime("%d.%m.%Y")
                sf_count = len(list_assistant_files(p["id"]))
                badge    = t("skill_files_badge", lang=lang, count=sf_count) if sf_count else ""
                st.markdown(f"**{p['name']}**{badge}")
                if p.get("description"):
                    st.caption(f"📝 {p['description']}")
                tools_str = ""
                if p.get("tools"):
                    tools_str = t("skill_tools_badge", lang=lang,
                                   tools=_format_tools_badge(p["tools"]))
                    if p.get("max_tool_calls") is not None:
                        tools_str += t("skill_max_calls_badge", lang=lang, count=p['max_tool_calls'])
                max_tokens_str = ""
                if p.get("max_tokens") is not None:
                    max_tokens_str = t("skill_max_tokens_badge", lang=lang, count=p['max_tokens'])
                reasoning_str = ""
                if p.get("reasoning_effort"):
                    reasoning_str = f" | RE={p['reasoning_effort']}"
                st.caption(
                    f"{p.get('service','?')} > {p['model']} | "
                    f"T={p['temperature']} | {created}{tools_str}{max_tokens_str}{reasoning_str}"
                )
            with c_edit:
                if st.button(t("btn_edit", lang=lang), key=f"edit_{p['id']}"):
                    _set_show_form(True)
                    _set_edit_id(p["id"])
                    st.rerun()
            with c_del:
                if st.button(t("btn_delete", lang=lang), key=f"del_{p['id']}",
                             use_container_width=True):
                    st.session_state["assistant_confirm_delete"] = p["id"]
                    st.rerun()

        # Inline delete confirmation (destructive action gate).
        if st.session_state.get("assistant_confirm_delete") == p["id"]:
            with st.container(border=True):
                st.warning(t("confirm_delete", lang=lang, name=p["name"]))
                c_yes, c_no = st.columns(2)
                with c_yes:
                    if st.button(t("btn_yes_delete", lang=lang),
                                 key=f"del_yes_{p['id']}",
                                 type="primary", use_container_width=True):
                        delete_assistant(p["id"])
                        st.session_state["assistant_confirm_delete"] = None
                        st.success(t("skills_deleted", lang=lang))
                        st.rerun()
                with c_no:
                    if st.button(t("btn_cancel", lang=lang),
                                 key=f"del_no_{p['id']}",
                                 use_container_width=True):
                        st.session_state["assistant_confirm_delete"] = None
                        st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
