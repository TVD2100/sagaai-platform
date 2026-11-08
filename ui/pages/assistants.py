# -*- coding: utf-8 -*-
"""
ui/pages/assistants.py - Page to manage assistants (system prompts, config, files).

Session-state keys use the "assistant" naming; the legacy "skill" keys are
also read and mirrored when present. Pure value helpers live in
``ui.pages.assistants_helpers`` and are re-exported here, so callers keep
resolving them through this module.
"""
from datetime import datetime

import streamlit as st

from core.assistant_folders import (
    get_assistant_web_search_settings,
    set_assistant_rag_bases,
    set_assistant_web_search_settings,
)
from core.assistant_folders import (
    load_assistant_bundle as folder_load_bundle,
)
from core.assistants import (
    create_assistant,
    delete_assistant,
    delete_assistant_file,
    get_assistant_by_id,
    list_assistant_files,
    load_assistants_index,
    save_assistant_file,
    update_assistant,
)
from core.i18n import t
from core.prompt_improver import improve_prompt_with_main_model
from core.rag import list_bases as list_rag_bases
from core.services import (
    default_reasoning_effort,
    get_model_reasoning_effort_options,
    get_services,
    service_supports_reasoning_effort,
)
from core.tools_utils import (
    build_rag_chunks_tool,
    build_rag_search_tool,
    list_tool_definitions,
    service_supported_tools,
)
from ui.components.flash import flash_error, flash_success
from ui.components.name_fields import render_name_fields
from ui.pages.assistants_helpers import (  # noqa: F401  re-exported for callers
    _clamp_temperature,
    _finalize_tools,
    _format_tools_badge,
    _get_model_max_tokens_limit,
    _model_choice_lists,
    _temperature_bounds,
)
from ui.pages.assistants_nav import _render_back_to_chat  # noqa: F401  re-exported

# built-in assistant IDs that should never be editable
_BUILTIN_ASSISTANT_IDS = {"dev_agent", "skill_creator", "assistant_creator"}

# ─── Session-state helpers with legacy fallback ────────────────────────────

def _get_show_form() -> bool:
    """Return whether the create/edit form is open (legacy key included)."""
    return bool(
        st.session_state.get("show_assistant_form", False)
        or st.session_state.get("show_skill_form", False)
    )


def _set_show_form(value: bool) -> None:
    """Open or close the form, mirroring the legacy key."""
    st.session_state["show_assistant_form"] = value
    st.session_state["show_skill_form"] = value


def _get_edit_id():
    """Return the id of the assistant being edited, or None when creating."""
    return st.session_state.get("edit_assistant_id") or st.session_state.get("edit_skill_id")


def _set_edit_id(value) -> None:
    """Set the edited assistant id, mirroring the legacy key."""
    st.session_state["edit_assistant_id"] = value
    st.session_state["edit_skill_id"] = value


def page_assistants() -> None:
    """Assistants management page: the list, or the inline form when open.

    The page is two flows - a list of cards and one editing form - and each
    step of both lives in its own helper below. Every helper stays in THIS
    module: the tests replace ``st`` and the data functions on the module
    globals, so a helper moved elsewhere would read its own namespace and
    stop seeing a replacement installed here.
    """
    lang = st.session_state.get("ui_lang", "en")
    st.title(t("skills_title", lang=lang))
    _render_back_to_chat(lang)

    available_services = get_services()
    service_names = list(available_services.keys())
    tool_defs = list_tool_definitions()

    if _get_show_form():
        _render_assistant_form(lang, available_services, service_names, tool_defs)
        return
    _render_assistant_list(lang, available_services)


def _prepare_prompt_revision(edit_id) -> tuple:
    """Return (revision, widget key), dropping drafts of other targets.

    Opening the form for a different assistant discards the drafts left by a
    previous one, so an improved prompt never leaks into another card.
    """
    if st.session_state.get("assistant_prompt_revision_for") != edit_id:
        for _k in [k for k in list(st.session_state.keys())
                   if k.startswith("assistant_prompt_text_")]:
            st.session_state.pop(_k, None)
        st.session_state["assistant_prompt_revision"] = 0
        st.session_state["assistant_prompt_revision_for"] = edit_id
    revision = st.session_state.get("assistant_prompt_revision", 0)
    return revision, f"assistant_prompt_text_{revision}"


def _form_defaults(lang: str, edit_id, editing: bool, service_names: list) -> tuple:
    """Return (defaults, existing) for the form of the given target.

    ``existing`` is None while creating; ``defaults`` carries the initial
    value of every widget.
    """
    if not editing:
        st.subheader(t("skills_form_title_create", lang=lang))
        return {
            "name": "", "name_en": "", "name_ru": "", "name_zh": "",
            "service": service_names[0] if service_names else "",
            "model": "", "temp": None, "desc": "", "text": "", "tools": [],
            "max_calls": None, "max_tokens": None, "reasoning": None,
            "ws_settings": {},
        }, None
    existing = get_assistant_by_id(edit_id)
    if not existing:
        flash_error(t("skill_not_found", lang=lang))
        _set_show_form(False)
        _set_edit_id(None)
        st.rerun()
        # A mock st returns from rerun instead of unwinding: keep None away.
        return _form_defaults(lang, edit_id, False, service_names)
    st.subheader(t("skills_form_title_edit", lang=lang))
    ws_settings = {}
    try:
        if existing.get("slug"):
            ws_settings = get_assistant_web_search_settings(existing["slug"]) or {}
    except Exception:
        ws_settings = {}
    return {
        # The user-defined slot is what an edit form writes back; the resolved
        # ``name`` is only a display value and must not become a stored name.
        "name": existing.get("name_user") or existing.get("name", ""),
        "name_en": existing.get("name_en", ""),
        "name_ru": existing.get("name_ru", ""),
        "name_zh": existing.get("name_zh", ""),
        "service": existing.get("service", ""),
        "model": existing.get("model", ""),
        "temp": existing.get("temperature", 0.7),
        "desc": existing.get("description", ""),
        "text": existing.get("text", ""),
        "tools": existing.get("tools", []),
        "max_calls": existing.get("max_tool_calls"),
        "max_tokens": existing.get("max_tokens"),
        "reasoning": existing.get("reasoning_effort"),
        "ws_settings": ws_settings,
    }, existing


def _render_assistant_form(lang: str, available_services: dict,
                           service_names: list, tool_defs: list) -> None:
    """Render the create/edit form and handle its save button."""
    edit_id = _get_edit_id()
    editing = bool(edit_id)
    prompt_revision, prompt_key = _prepare_prompt_revision(edit_id)
    defaults, existing = _form_defaults(lang, edit_id, editing, service_names)

    if not service_names:
        st.error(t("no_services", lang=lang))
        return

    name_fields = render_name_fields(lang, edit_id or "new", defaults,
                                    st=st, user_key="assistant_name_input")
    desc = st.text_area(
        t("skills_form_desc", lang=lang), value=defaults["desc"], key="assistant_desc",
        help=t("skills_form_desc_help", lang=lang))
    prompt_text = st.text_area(
        t("prompt_text", lang=lang), value=defaults["text"], height=300,
        key=prompt_key, help=t("prompt_text_help", lang=lang))
    if st.button(t("improve_prompt", lang=lang), key="assistant_improve"):
        _improve_prompt(prompt_text, prompt_revision, lang)
    st.markdown("---")

    if editing and existing:
        _warn_saved_model(lang, existing, available_services, service_names)

    model_state = _render_model_fields(lang, defaults, available_services,
                                       service_names, tool_defs)
    bases = _render_rag_bases_section(lang, editing, existing)
    web = _render_web_search_section(lang, model_state["svc_info"], defaults["ws_settings"])
    _render_files_section(lang, editing, edit_id)

    form = dict(model_state)
    form.update({
        "edit_id": edit_id, "editing": editing, "existing": existing,
        "name": name_fields["name"], "desc": desc, "prompt_text": prompt_text,
        "name_en": name_fields["name_en"], "name_ru": name_fields["name_ru"],
        "name_zh": name_fields["name_zh"],
        "base_options": bases["base_options"], "bases_selected": bases["bases_selected"],
        "web_context": web["context"], "web_domains": web["domains"],
    })
    c1, c2 = st.columns([1, 4])
    with c1:
        if st.button(t("btn_save", lang=lang), type="primary", key="assistant_save_btn"):
            _submit_assistant_form(lang, form)
    with c2:
        if st.button(t("btn_cancel", lang=lang), key="assistant_cancel_btn"):
            _set_show_form(False)
            _set_edit_id(None)
            st.rerun()


def _improve_prompt(prompt_text: str, prompt_revision: int, lang: str) -> None:
    """Run the prompt improver and open its result as a fresh draft."""
    if not prompt_text.strip():
        st.warning(t("err_prompt_required", lang=lang))
        return
    try:
        with st.spinner(t("improve_prompt_running", lang=lang)):
            improved = improve_prompt_with_main_model(prompt_text, lang=lang)
    except Exception as exc:
        st.error(t("improve_prompt_error", lang=lang, error=str(exc)))
        return
    new_rev = prompt_revision + 1
    st.session_state[f"assistant_prompt_text_{new_rev}"] = improved
    for _k in [k for k in list(st.session_state.keys())
               if k.startswith("assistant_prompt_text_")
               and k != f"assistant_prompt_text_{new_rev}"]:
        st.session_state.pop(_k, None)
    st.session_state["assistant_prompt_revision"] = new_rev
    flash_success(t("improve_prompt_ok", lang=lang))
    st.rerun()


def _warn_saved_model(lang: str, existing: dict, available_services: dict,
                      service_names: list) -> None:
    """Warn when the stored model pair of an edited assistant is gone."""
    saved_svc = existing.get("service", "")
    if saved_svc and saved_svc not in service_names:
        st.warning(t("assistant_service_unavailable", lang=lang, service=saved_svc))
        return
    if saved_svc in service_names:
        saved_models = [m.get("id") if isinstance(m, dict) else m
                        for m in available_services.get(saved_svc, {}).get("models", [])]
        if existing.get("model") and existing["model"] not in saved_models:
            st.warning(t("assistant_model_unavailable", lang=lang,
                         model=existing["model"], service=saved_svc))


def _render_model_fields(lang: str, defaults: dict, available_services: dict,
                         service_names: list, tool_defs: list) -> dict:
    """Render the model, sampling and tool knobs of the form."""
    svc_idx = 0
    if defaults["service"] in service_names:
        svc_idx = service_names.index(defaults["service"])
    service = st.selectbox(
        t("service", lang=lang), service_names, index=svc_idx,
        key="assistant_service", help=t("service_help", lang=lang))

    svc_info = available_services.get(service, {})
    model_list, model_ids, model_display = _model_choice_lists(svc_info)
    default_model_idx = 0
    if defaults["model"] and defaults["model"] in model_ids:
        default_model_idx = model_ids.index(defaults["model"])
    model_sel = st.selectbox(
        t("model", lang=lang), range(len(model_list)),
        format_func=lambda i: model_display[i] if i < len(model_display) else "-",
        index=default_model_idx, key="assistant_model", help=t("model_help", lang=lang))
    model = model_ids[model_sel] if model_list else ""

    t_min, t_max, t_step = _temperature_bounds(svc_info)
    default_temp = defaults["temp"]
    if default_temp is None:
        default_temp = float(svc_info.get("temp_default", 0.7))
    temperature = _clamp_temperature(
        st.slider(t("temperature", lang=lang), t_min, t_max,
                  value=float(default_temp), step=t_step,
                  key="assistant_temp", help=t("temperature_help", lang=lang)),
        svc_info)

    reasoning_effort = ""
    if service_supports_reasoning_effort(svc_info):
        reasoning_options = get_model_reasoning_effort_options(svc_info, model)
        default_reasoning = defaults["reasoning"]
        if not default_reasoning:
            default_reasoning = default_reasoning_effort(svc_info, strong=False, model=model) or "high"
        if default_reasoning not in reasoning_options and reasoning_options:
            default_reasoning = reasoning_options[0]
        reasoning_effort = st.selectbox(
            t("reasoning_effort", lang=lang), options=reasoning_options,
            index=reasoning_options.index(default_reasoning) if default_reasoning in reasoning_options else 0,
            key="assistant_reasoning_effort", help=t("reasoning_effort_help", lang=lang))

    allowed_tools = service_supported_tools(svc_info, tool_defs)
    if allowed_tools:
        tools_selected = st.multiselect(
            t("tools", lang=lang), options=allowed_tools,
            default=[n for n in (defaults["tools"] or []) if n in allowed_tools],
            key=f"assistant_tools_{service}", help=t("tools_help", lang=lang))
    else:
        tools_selected = []
        st.caption(t("tools_not_supported", lang=lang))
    max_tool_calls = st.number_input(
        t("skill_max_tool_calls_label", lang=lang), min_value=1, max_value=50, step=1,
        value=defaults["max_calls"] or 3, key="assistant_max_calls",
        help=t("assistant_max_tool_calls_help", lang=lang))

    limit = _get_model_max_tokens_limit(svc_info, model)
    cur = defaults["max_tokens"] if defaults["max_tokens"] is not None else 4096
    if cur > limit:
        cur = limit
    max_tokens = st.number_input(
        t("max_tokens", lang=lang), min_value=1, max_value=limit, step=1,
        value=cur, key="assistant_max_tokens", help=t("max_tokens_help", lang=lang))

    return {"service": service, "model": model, "temperature": temperature,
            "reasoning_effort": reasoning_effort, "tools_selected": tools_selected,
            "max_tool_calls": max_tool_calls, "max_tokens": max_tokens,
            "svc_info": svc_info}


def _render_rag_bases_section(lang: str, editing: bool, existing) -> dict:
    """Render the knowledge-base picker of the form."""
    st.markdown("### " + t("assistant_rag_bases", lang=lang))
    rag_bases = list_rag_bases(with_stats=False)
    base_options = [b.get("slug", "") for b in rag_bases]
    base_labels = [
        f"{b.get('name') or b.get('slug')} ({b.get('status') or 'draft'})"
        for b in rag_bases
    ]
    if not base_options:
        st.caption(t("assistant_rag_bases_empty", lang=lang))
        return {"base_options": [], "bases_selected": []}
    default_bases = []
    if editing:
        try:
            bundle = folder_load_bundle((existing or {}).get("slug") or "") or {}
            default_bases = [str(x).strip().lower()
                             for x in (bundle.get("rag_bases") or []) if str(x).strip()]
        except Exception:
            default_bases = []
    bases_selected = st.multiselect(
        t("assistant_rag_bases", lang=lang), options=range(len(base_options)),
        default=[i for i, s in enumerate(base_options) if s in default_bases],
        format_func=lambda i: base_labels[i] if i < len(base_labels) else "-",
        key="assistant_rag_bases_sel", help=t("assistant_rag_bases_help", lang=lang))
    return {"base_options": base_options, "bases_selected": bases_selected}


def _render_web_search_section(lang: str, svc_info: dict, ws_settings: dict) -> dict:
    """Render the per-assistant web-search overrides, when the service has them."""
    if svc_info.get("auth_type") != "yandex_iam":
        return {"context": "", "domains": ""}
    st.markdown("### " + t("assistant_web_search_section", lang=lang))
    ws_ctx_cur = ws_settings.get("context_size") or ""
    ctx_options = ["", "low", "medium", "high"]
    if ws_ctx_cur not in ctx_options:
        ws_ctx_cur = ""
    web_search_context = st.selectbox(
        t("assistant_web_search_context", lang=lang), options=ctx_options,
        index=ctx_options.index(ws_ctx_cur),
        format_func=lambda v: (t("web_search_context_auto", lang=lang)
                               if v == "" else v.capitalize()),
        key="assistant_ws_context", help=t("assistant_web_search_context_help", lang=lang))
    default_ws_domains = ", ".join(
        str(d) for d in (ws_settings.get("allowed_domains") or []))
    web_search_domains = st.text_input(
        t("assistant_web_search_domains", lang=lang), value=default_ws_domains,
        key="assistant_ws_domains",
        placeholder=t("assistant_web_search_domains_placeholder", lang=lang),
        help=t("assistant_web_search_domains_help", lang=lang))
    return {"context": web_search_context, "domains": web_search_domains}


def _render_files_section(lang: str, editing: bool, edit_id) -> None:
    """Render the attached-files list, its delete buttons and the uploader."""
    st.markdown("### " + t("skill_files", lang=lang))
    if not editing:
        st.info(t("save_skill_first", lang=lang))
        return
    files = list_assistant_files(edit_id)
    if not files:
        st.caption(t("no_files", lang=lang))
    for fname in files or []:
        c1, c2 = st.columns([6, 1])
        with c1:
            st.write(fname)
        with c2:
            if st.button(t("delete_file", lang=lang), key=f"del_assistant_file_{fname}"):
                delete_assistant_file(edit_id, fname)
                flash_success(t("deleted_ok", lang=lang))
                st.rerun()
    uploaded_files = st.file_uploader(
        t("add_files", lang=lang), accept_multiple_files=True,
        key="assistant_file_uploader", help=t("add_files_help", lang=lang))
    if uploaded_files and st.button(t("btn_add_files", lang=lang), key="assistant_add_files"):
        _save_uploaded_files(lang, edit_id, uploaded_files)


def _save_uploaded_files(lang: str, edit_id, uploaded_files) -> None:
    """Store the uploaded files and report both outcomes.

    The rerun discards a plain st.* call, so the confirmations and the per-file
    failures are queued through the flash helpers and painted on the next
    render pass; when nothing was added the failures are shown right away.
    """
    added = 0
    failures = []
    for uf in uploaded_files:
        try:
            content = uf.getvalue().decode("utf-8")
            save_assistant_file(edit_id, uf.name, content)
            added += 1
        except Exception as e:
            failures.append(t("skill_file_error", lang=lang, name=uf.name, error=str(e)))
    if added:
        for msg in failures:
            flash_error(msg)
        flash_success(t("files_added", lang=lang, count=added))
        st.rerun()
    for msg in failures:
        st.error(msg)


def _submit_assistant_form(lang: str, form: dict) -> None:
    """Validate the form, persist the assistant and leave the form."""
    # An entity installed from the marketplace carries no user-defined name:
    # it shows the translation of the interface language, so an empty name
    # field is valid as long as at least one translation exists.
    if not form["name"].strip() and not any(
        str(form.get(field) or "").strip()
        for field in ("name_en", "name_ru", "name_zh")
    ):
        st.error(t("err_name_required", lang=lang))
        return
    if not form["prompt_text"].strip():
        st.error(t("err_prompt_required", lang=lang))
        return
    effort_value = form["reasoning_effort"] or None
    rag_ids = [form["base_options"][i] for i in form["bases_selected"]
               if 0 <= i < len(form["base_options"])]
    final_tools = _finalize_tools(form["tools_selected"], rag_ids, form["svc_info"],
                                  build_rag_search_tool, build_rag_chunks_tool)
    kwargs = {"tools": final_tools, "max_tool_calls": form["max_tool_calls"],
              "max_tokens": form["max_tokens"] if form["max_tokens"] else None,
              "reasoning_effort": effort_value,
              "name_en": form.get("name_en") or None,
              "name_ru": form.get("name_ru") or None,
              "name_zh": form.get("name_zh") or None}
    if form["editing"]:
        update_assistant(form["edit_id"], form["name"], form["service"], form["model"],
                         form["temperature"], form["prompt_text"], form["desc"], **kwargs)
        slug = (form["existing"] or {}).get("slug")
    else:
        new_id = create_assistant(form["name"], form["service"], form["model"],
                                  form["temperature"], form["prompt_text"],
                                  form["desc"], **kwargs)
        slug = (get_assistant_by_id(new_id) or {}).get("slug") if new_id else None
    if slug:
        set_assistant_rag_bases(slug, rag_ids)
        set_assistant_web_search_settings(
            slug, context_size=form["web_context"] or None,
            allowed_domains=form["web_domains"])
    _set_show_form(False)
    _set_edit_id(None)
    flash_success(t("skills_saved", lang=lang))
    st.rerun()


def _render_assistant_list(lang: str, available_services: dict) -> None:
    """Render the create button and the cards of all user assistants."""
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
        _render_assistant_card(lang, p)
        _render_delete_confirmation(lang, p)


def _render_assistant_card(lang: str, p: dict) -> None:
    """Render one assistant card with its edit and delete buttons."""
    with st.container(border=True):
        c_info, c_edit, c_del = st.columns([7, 1, 1])
        with c_info:
            _render_assistant_card_info(lang, p)
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


def _render_assistant_card_info(lang: str, p: dict) -> None:
    """Render the name, description and model badges of one card."""
    created = datetime.fromisoformat(p["created_at"]).strftime("%d.%m.%Y")
    sf_count = len(list_assistant_files(p["id"]))
    badge = t("skill_files_badge", lang=lang, count=sf_count) if sf_count else ""
    st.markdown(f"**{p['name']}**{badge}")
    if p.get("description"):
        st.caption(f"📝 {p['description']}")
    tools_str = ""
    if p.get("tools"):
        tools_str = t("skill_tools_badge", lang=lang, tools=_format_tools_badge(p["tools"]))
        if p.get("max_tool_calls") is not None:
            tools_str += t("skill_max_calls_badge", lang=lang, count=p['max_tool_calls'])
    max_tokens_str = ""
    if p.get("max_tokens") is not None:
        max_tokens_str = t("skill_max_tokens_badge", lang=lang, count=p['max_tokens'])
    reasoning_str = f" | RE={p['reasoning_effort']}" if p.get("reasoning_effort") else ""
    st.caption(
        f"{p.get('service','?')} > {p['model']} | "
        f"T={p['temperature']} | {created}{tools_str}{max_tokens_str}{reasoning_str}"
    )


def _render_delete_confirmation(lang: str, p: dict) -> None:
    """Render the inline delete confirmation of one assistant.

    Deleting an assistant also removes its folder, so the destructive action
    stays behind an explicit yes/no pair instead of a single click.
    """
    if st.session_state.get("assistant_confirm_delete") != p["id"]:
        return
    with st.container(border=True):
        st.warning(t("confirm_delete", lang=lang, name=p["name"]))
        c_yes, c_no = st.columns(2)
        with c_yes:
            if st.button(t("btn_yes_delete", lang=lang), key=f"del_yes_{p['id']}",
                         type="primary", use_container_width=True):
                delete_assistant(p["id"])
                st.session_state["assistant_confirm_delete"] = None
                flash_success(t("skills_deleted", lang=lang))
                st.rerun()
        with c_no:
            if st.button(t("btn_cancel", lang=lang), key=f"del_no_{p['id']}",
                         use_container_width=True):
                st.session_state["assistant_confirm_delete"] = None
                st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
