# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings_orch - Settings tab: functions, instructions, skills, RAG, connections, memory.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""

from core.connectors import list_connections
from core.orchestrators import (
    DEVAGENT_SLUG,
    get_enabled_connections,
    get_enabled_skills,
    get_orchestrator_rag_bases,
    orch_delete_function,
    orch_delete_instruction,
    orch_save_function,
    orch_save_instruction,
    set_enabled_connections,
    set_enabled_skills,
    set_orchestrator_rag_bases,
)
from ui.components.flash import flash_error
from ui.pages.orchestrator_shared import late, st_proxy

st = st_proxy()
_set_ss = late("_set_ss")
_ss = late("_ss")
_thread_folder = late("_thread_folder")
flash_success = late("flash_success")
get_disabled_tools = late("get_disabled_tools")
get_orchestrator = late("get_orchestrator")
list_bases_with_activity = late("list_bases_with_activity")
list_library_skills = late("list_library_skills")
list_system_tools = late("list_system_tools")
orch_get_function = late("orch_get_function")
orch_get_instruction = late("orch_get_instruction")
orch_list_functions = late("orch_list_functions")
orch_list_instructions = late("orch_list_instructions")
pin_settings_section = late("pin_settings_section")
save_orchestrator = late("save_orchestrator")
set_disabled_tools = late("set_disabled_tools")
settings_save_failed = late("settings_save_failed")
t = late("t")


def _render_orch_system_tools(slug: str, lang: str) -> None:
    """Manage enabled/disabled system tools (blacklist via disabled_tools)."""
    st.markdown(t("orch_sysfunc_section_title", lang=lang))
    st.markdown(t("orch_sysfunc_section_desc", lang=lang))

    try:
        system_tools = list_system_tools(slug)
    except Exception:
        system_tools = []

    if not system_tools:
        st.info(t("orch_sysfunc_empty", lang=lang))
        return

    disabled = set(get_disabled_tools(slug))
    disabled_selected = []
    for tool in system_tools:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        desc = str(tool.get("desc") or "").strip()
        checked = st.checkbox(
            f"**{name}**",
            value=(name not in disabled),
            key=f"orch_sysfunc_{slug}_{name}",
        )
        if desc:
            st.caption(f"\U0001f4dd {desc}")
        if not checked:
            disabled_selected.append(name)

    st.markdown("---")
    if st.button(t("orch_sysfunc_save_btn", lang=lang), key=f"orch_save_sysfunc_{slug}",
                 type="primary"):
        if set_disabled_tools(slug, disabled_selected):
            pin_settings_section(slug, "functions")
            flash_success(t("orch_sysfunc_saved", lang=lang))
            st.rerun()
        else:
            settings_save_failed(slug, "functions", "orch_sysfunc_save_error", lang)


def _render_orchestrator_functions_settings(slug: str, lang: str) -> None:
    """Manage custom Python functions of this orchestrator (stored in folder)."""
    st.markdown(t("orch_func_section_title", lang=lang))
    st.markdown(t("orch_func_section_desc", lang=lang))

    _render_orch_system_tools(slug, lang)

    show_form = _ss(slug, "show_func_form") or False
    edit_name = _ss(slug, "edit_func") or None

    if show_form:
        _render_function_form(slug, lang, edit_name)
        return

    if st.button(t("orch_func_create_btn", lang=lang), key=f"orch_func_create_{slug}", type="primary"):
        _set_ss(slug, "show_func_form", True)
        _set_ss(slug, "edit_func", None)
        st.rerun()

    _render_function_cards(slug, lang)


def _render_function_cards(slug: str, lang: str) -> None:
    """List the saved custom functions with edit and delete controls."""
    functions = orch_list_functions(slug)
    if not functions:
        st.info(t("orch_func_empty", lang=lang))
        return

    for fn in functions:
        with st.container(border=True):
            c_info, c_edit, c_del = st.columns([7, 1, 1])
            with c_info:
                st.markdown(f"**{fn.get('name', '?')}**")
                st.caption(f"📄 {fn.get('path', '')} · {fn.get('size_bytes', 0)} B")
            with c_edit:
                if st.button("✏️", key=f"orch_func_edit_{slug}_{fn['name']}"):
                    _set_ss(slug, "show_func_form", True)
                    _set_ss(slug, "edit_func", fn["name"])
                    st.rerun()
            with c_del:
                if st.button("🗑", key=f"orch_func_del_{slug}_{fn['name']}"):
                    orch_delete_function(slug, fn["name"])
                    pin_settings_section(slug, "functions")
                    flash_success(t("orch_func_deleted", lang=lang))
                    st.rerun()


def _render_function_form(slug: str, lang: str, edit_name) -> None:
    """Render the create/edit form of one custom function."""
    edit_data = orch_get_function(slug, edit_name) if edit_name else None
    with st.container(border=True):
        st.subheader(t("orch_func_edit_title", lang=lang) if edit_data else t("orch_func_create_title", lang=lang))
        name = st.text_input(
            t("orch_func_name_label", lang=lang),
            value=edit_data.get("name", "") if edit_data else "",
            disabled=bool(edit_data),
            key=f"orch_func_name_{slug}",
            help=t("orch_func_name_help", lang=lang),
        )
        code = st.text_area(
            t("orch_func_code_label", lang=lang),
            value=edit_data.get("code", "") if edit_data else "",
            height=250,
            key=f"orch_func_code_{slug}",
            help=t("orch_func_code_help", lang=lang),
        )
        st.caption(t("orch_func_code_hint", lang=lang))
        c1, c2 = st.columns([1, 4])
        with c1:
            if st.button(t("orch_instr_save_btn", lang=lang), type="primary",
                         key=f"orch_func_save_{slug}"):
                if not name.strip() or not code.strip():
                    st.error(t("orch_func_required", lang=lang))
                else:
                    ok = orch_save_function(slug, name.strip(), code)
                    if ok:
                        _set_ss(slug, "show_func_form", False)
                        _set_ss(slug, "edit_func", None)
                        pin_settings_section(slug, "functions")
                        flash_success(t("orch_func_saved", lang=lang))
                        st.rerun()
                    else:
                        settings_save_failed(slug, "functions", "orch_func_save_error", lang)
        with c2:
            if st.button(t("orch_instr_cancel_btn", lang=lang), key=f"orch_func_cancel_{slug}"):
                _set_ss(slug, "show_func_form", False)
                _set_ss(slug, "edit_func", None)
                st.rerun()


def _render_orchestrator_instructions_settings(slug: str, lang: str) -> None:
    """Manage orchestrator-specific instructions (stored in folder)."""
    st.markdown(t("orch_oinstr_section_title", lang=lang))
    st.markdown(t("orch_oinstr_section_desc", lang=lang))

    show_form = _ss(slug, "show_oinstr_form") or False
    edit_id = _ss(slug, "edit_oinstr") or None

    if show_form:
        _render_instruction_form(slug, lang, edit_id)
        return

    if st.button(t("orch_instr_create_btn", lang=lang), key=f"orch_oinstr_create_{slug}", type="primary"):
        _set_ss(slug, "show_oinstr_form", True)
        _set_ss(slug, "edit_oinstr", None)
        st.rerun()

    _render_instruction_cards(slug, lang)


def _render_instruction_cards(slug: str, lang: str) -> None:
    """List the saved instructions with edit and delete controls."""
    instructions = orch_list_instructions(slug)
    if not instructions:
        st.info(t("orch_oinstr_empty", lang=lang))
        return

    for inst in instructions:
        with st.container(border=True):
            c_info, c_edit, c_del = st.columns([7, 1, 1])
            with c_info:
                st.markdown(f"**{inst.get('name', '?')}**")
                if inst.get("description"):
                    st.caption(f"📝 {inst['description']}")
            with c_edit:
                if st.button("✏️", key=f"orch_oinstr_edit_{slug}_{inst['id']}"):
                    _set_ss(slug, "show_oinstr_form", True)
                    _set_ss(slug, "edit_oinstr", inst["id"])
                    st.rerun()
            with c_del:
                if st.button("🗑", key=f"orch_oinstr_del_{slug}_{inst['id']}"):
                    orch_delete_instruction(slug, inst["id"])
                    pin_settings_section(slug, "instructions")
                    flash_success(t("orch_instr_deleted", lang=lang))
                    st.rerun()


def _render_instruction_form(slug: str, lang: str, edit_id) -> None:
    """Render the create/edit form of one instruction."""
    edit_data = orch_get_instruction(slug, edit_id) if edit_id else None
    with st.container(border=True):
        st.subheader(t("orch_edit_instr_title", lang=lang) if edit_data else t("orch_create_instr_title", lang=lang))
        name = st.text_input(t("orch_instr_name_label", lang=lang), value=edit_data.get("name", "") if edit_data else "",
                             key=f"orch_oinstr_name_{slug}",
                             help=t("orch_instr_name_help", lang=lang))
        description = st.text_input(t("orch_instr_desc_label", lang=lang), value=edit_data.get("description", "") if edit_data else "",
                                    max_chars=200, key=f"orch_oinstr_desc_{slug}",
                                    help=t("orch_instr_desc_help", lang=lang))
        prompt_text = st.text_area(t("orch_instr_prompt_label", lang=lang),
                                   value=edit_data.get("text", "") if edit_data else "",
                                   height=250, key=f"orch_oinstr_prompt_{slug}",
                                   help=t("orch_instr_prompt_help", lang=lang))
        c1, c2 = st.columns([1, 4])
        with c1:
            if st.button(t("orch_instr_save_btn", lang=lang), type="primary", key=f"orch_oinstr_save_{slug}"):
                if not name.strip() or not prompt_text.strip():
                    st.error(t("orch_instr_required", lang=lang))
                else:
                    ok = orch_save_instruction(
                        slug, instruction_id=edit_id or "", name=name.strip(),
                        description=description.strip(), prompt_text=prompt_text,
                    )
                    if ok:
                        _set_ss(slug, "show_oinstr_form", False)
                        _set_ss(slug, "edit_oinstr", None)
                        pin_settings_section(slug, "instructions")
                        flash_success(t("orch_instr_saved", lang=lang))
                        st.rerun()
                    else:
                        settings_save_failed(slug, "instructions", "orch_instr_save_error", lang)
        with c2:
            if st.button(t("orch_instr_cancel_btn", lang=lang), key=f"orch_oinstr_cancel_{slug}"):
                _set_ss(slug, "show_oinstr_form", False)
                _set_ss(slug, "edit_oinstr", None)
                st.rerun()


def _render_orch_skills_settings(slug: str, lang: str) -> None:
    """Manage enabled standardized skills (Skills library) for this orchestrator."""
    st.markdown(t("orch_skills_section_title", lang=lang))
    st.markdown(t("orch_skills_section_desc", lang=lang))

    try:
        # Only skills adapted for SagaAI can be assigned to an orchestrator;
        # non-adapted third-party skills remain visible in the Skills library
        # page where they can be adapted.
        all_skills = list_library_skills(adapted_only=True)
    except Exception:
        all_skills = []

    if not all_skills:
        st.info(t("orch_skills_empty", lang=lang))
        st.markdown(t("orch_skills_empty_hint", lang=lang))
        return

    enabled = set(get_enabled_skills(slug))
    selected = []
    for skill_item in all_skills:
        skill_id = skill_item["id"]
        checked = st.checkbox(
            f"**{skill_item['name']}** `{skill_id}`",
            value=(skill_id in enabled),
            key=f"orch_skill_{slug}_{skill_id}",
        )
        if skill_item.get("description"):
            st.caption(f"📝 {skill_item['description']}")
        st.caption(f"📁 {skill_item.get('folder', '')}")
        if checked:
            selected.append(skill_id)

    st.markdown("---")
    if st.button(t("orch_skills_save_btn", lang=lang), key=f"orch_save_skills_{slug}",
                 type="primary"):
        if set_enabled_skills(slug, selected):
            pin_settings_section(slug, "skills")
            flash_success(t("orch_skills_saved", lang=lang))
            st.rerun()
        else:
            settings_save_failed(slug, "skills", "orch_skills_save_error", lang)


def _render_orch_rag_bases(slug: str, lang: str) -> None:
    """Assign RAG knowledge bases this orchestrator may use via rag_search."""
    st.markdown(t("orch_rag_bases_section_title", lang=lang))
    st.markdown(t("orch_rag_bases_section_desc", lang=lang))

    try:
        bases = list_bases_with_activity(with_stats=False)
    except Exception:
        bases = []

    if not bases:
        st.info(t("orch_rag_bases_empty", lang=lang))
        return

    if slug == DEVAGENT_SLUG:
        st.info(t("orch_rag_bases_all_hint", lang=lang))
        for b in bases:
            bslug = str(b.get("slug") or "")
            if not bslug:
                continue
            st.markdown(f"**{b.get('name') or bslug}** `{bslug}` "
                        f"({str(b.get('status') or 'draft')})")
            if not bool(b.get("active", True)):
                st.caption("⚠ " + t("orch_rag_bases_inactive_hint", lang=lang))
        return

    assigned = set(get_orchestrator_rag_bases(slug))
    if not assigned:
        st.info(t("orch_rag_bases_none_allowed", lang=lang))
    selected = []
    for b in bases:
        bslug = str(b.get("slug") or "")
        if not bslug:
            continue
        checked = st.checkbox(
            f"**{b.get('name') or bslug}** `{bslug}`",
            value=(bslug in assigned),
            key=f"orch_rag_base_{slug}_{bslug}",
        )
        if not bool(b.get("active", True)):
            st.caption("⚠ " + t("orch_rag_bases_inactive_hint", lang=lang))
        if checked:
            selected.append(bslug)

    st.markdown("---")
    if st.button(t("orch_rag_bases_save_btn", lang=lang), key=f"orch_save_rag_bases_{slug}",
                 type="primary"):
        if set_orchestrator_rag_bases(slug, selected):
            pin_settings_section(slug, "rag_bases")
            flash_success(t("orch_rag_bases_saved", lang=lang))
            st.rerun()
        else:
            settings_save_failed(slug, "rag_bases", "orch_rag_bases_save_error", lang)


def _render_orch_connections(slug: str, lang: str) -> None:
    """Manage enabled external-service connections for this orchestrator."""
    st.markdown(t("orch_connections_section_title", lang=lang))
    st.markdown(t("orch_connections_section_desc", lang=lang))

    try:
        all_conns = list_connections()
    except Exception:
        all_conns = []

    if not all_conns:
        st.info(t("orch_connections_empty", lang=lang))
        return

    enabled = set(get_enabled_connections(slug))
    selected = []
    for conn in all_conns:
        conn_id = str(conn.get("id") or "")
        if not conn_id:
            continue
        name = str(conn.get("name") or conn_id)
        svc = str(conn.get("service") or "?")
        account = str(conn.get("account") or "")
        label = f"**{name}** `{conn_id}` ({svc})"
        if account:
            label += f" · {account}"
        checked = st.checkbox(
            label,
            value=(conn_id in enabled),
            key=f"orch_conn_{slug}_{conn_id}",
        )
        if checked:
            selected.append(conn_id)

    st.markdown("---")
    if st.button(t("orch_connections_save_btn", lang=lang),
                 key=f"orch_save_connections_{slug}",
                 type="primary"):
        if set_enabled_connections(slug, selected):
            pin_settings_section(slug, "connections")
            flash_success(t("orch_connections_saved", lang=lang))
            st.rerun()
        else:
            settings_save_failed(slug, "connections", "orch_connections_save_error", lang)


def _render_orch_memory(slug: str, lang: str) -> None:
    """Manage the employee's working folder and its personal memory.

    The working folder holds the role's domain material inside the project;
    the personal memory is a note to self that lives in the installation.
    """
    from core import personal_memory as pm
    from core.orchestrator_folders import default_data_dir_name, resolve_data_dir

    st.markdown(t("orch_memory_section_title", lang=lang))
    st.markdown(t("orch_memory_section_desc", lang=lang))

    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return
    cfg = orch.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
    default_name = default_data_dir_name(slug)

    # The workspace of THIS dialog, when it has one: the resolver needs it to
    # turn a relative folder into an absolute path and to spot conflicts.
    try:
        workspace = _thread_folder(slug)
    except Exception:
        workspace = ""
    resolved = resolve_data_dir(
        slug, workspace,
        prompt_text=str(orch.get("prompt_text") or ""),
        config=cfg,
    )

    _render_memory_dir_section(slug, lang, cfg, resolved, default_name)

    st.markdown("---")
    scope = pm.format_scope(pm.SCOPE_ORCHESTRATOR, slug)
    _render_memory_editor(slug, lang, pm, scope)


def _render_memory_dir_section(slug: str, lang: str, cfg: dict, resolved: dict,
                               default_name: str) -> None:
    """Render the working-folder field, its resolution caption and the save button."""
    new_dir = st.text_input(
        t("orch_memory_dir_label", lang=lang),
        value=str(cfg.get("memory_dir") or ""),
        key=f"orch_memory_dir_{slug}",
        help=t("orch_memory_dir_help", lang=lang, default=default_name),
    )
    if resolved.get("path"):
        st.caption(f"`{resolved['path']}` · {resolved.get('source') or 'default'}")
    else:
        st.caption(t("orch_memory_dir_help", lang=lang, default=default_name))
    for conflict in resolved.get("conflicts") or []:
        st.warning(t("orch_memory_conflict", lang=lang,
                     path=str(conflict.get("path") or ""),
                     files=int(conflict.get("files") or 0)))

    if st.button(t("orch_memory_dir_save_btn", lang=lang),
                 key=f"orch_save_memory_dir_{slug}", type="primary"):
        if save_orchestrator(slug, memory_dir=new_dir):
            pin_settings_section(slug, "memory")
            shown = new_dir.strip() or default_name
            flash_success(t("orch_memory_dir_saved", lang=lang, path=shown))
            st.rerun()
        else:
            settings_save_failed(slug, "memory", "orch_memory_dir_save_error", lang)


def _render_memory_editor(slug: str, lang: str, pm, scope: str) -> None:
    """Render the personal-memory body editor and its save button."""
    try:
        data = pm.read_memory(scope)
    except Exception as exc:
        st.error(t("orch_memory_save_error", lang=lang, error=str(exc)))
        return

    st.markdown(f"**{t('orch_memory_editor_label', lang=lang)}**")
    st.caption(t("orch_memory_editor_help", lang=lang))
    st.caption(t("orch_memory_size", lang=lang, bytes=data.get("bytes", 0),
                 max=data.get("max_bytes", 0), revision=data.get("revision", 0)))
    if not data.get("exists"):
        st.caption(t("orch_memory_empty", lang=lang))

    text = st.text_area(
        t("orch_memory_editor_label", lang=lang),
        value=str(data.get("body") or ""),
        height=260,
        key=f"orch_memory_text_{slug}",
        label_visibility="collapsed",
    )
    if st.button(t("orch_memory_save_btn", lang=lang),
                 key=f"orch_save_memory_{slug}", type="primary"):
        try:
            pm.write_memory(scope, text, updated_by="ui")
        except Exception as exc:
            pin_settings_section(slug, "memory")
            flash_error(t("orch_memory_save_error", lang=lang, error=str(exc)))
            st.rerun()
        else:
            pin_settings_section(slug, "memory")
            flash_success(t("orch_memory_saved", lang=lang))
            st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
