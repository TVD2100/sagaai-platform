# -*- coding: utf-8 -*-
"""
ui/pages/skills_library.py - страница управления библиотекой навыков (Skills).

Показывает установленные навыки с возможностью:
  * загрузить новый навык ZIP-архивом,
  * скачать навык с GitHub по URL,
  * указать локальную папку с уже загруженным навыком,
  * редактировать название, описание и имя папки (файлы не редактируются),
  * удалить навык.
"""
import streamlit as st

from core.i18n import t
from core.skills_library import (
    list_skills,
    get_skill,
    update_skill,
    delete_skill,
    import_skill_from_zip,
    import_skill_from_folder,
    import_skill_from_github,
    get_skills_root,
    SkillsLibraryError,
)


def page_skills_library() -> None:
    """Страница управления навыками (Skills)."""
    lang = st.session_state.get("ui_lang", "en")
    st.title(t("slib_title", lang=lang))
    st.caption(t("slib_intro", lang=lang, root=get_skills_root()))

    # ── Режим редактирования ────────────────────────────────────────────
    if st.session_state.get("slib_edit_id"):
        _render_edit_form(lang)
        return

    # ── Импорт ───────────────────────────────────────────────────────────
    st.markdown("---")
    _render_import_section(lang)

    # ── Список навыков ───────────────────────────────────────────────────
    st.markdown("---")
    _render_skills_list(lang)


def _render_import_section(lang: str) -> None:
    """Рендер секции добавления навыка: ZIP / GitHub / локальная папка."""
    st.subheader(t("slib_install_title", lang=lang))

    # Tabs для трёх способов загрузки
    tab_zip, tab_github, tab_folder = st.tabs([
        f"📦 {t('slib_from_zip', lang=lang)}",
        f"🌐 {t('slib_from_github', lang=lang)}",
        f"📁 {t('slib_from_folder', lang=lang)}",
    ])

    # ── ZIP ──
    with tab_zip:
        zip_file = st.file_uploader(
            t("slib_zip_label", lang=lang),
            type=["zip"],
            key="slib_zip_upload",
            help=t("slib_zip_label_help", lang=lang),
        )
        if zip_file is not None:
            zip_name = st.text_input(
                t("slib_name", lang=lang),
                key="slib_zip_name",
                placeholder=zip_file.name.replace(".zip", ""),
                help=t("slib_name_help", lang=lang),
            )
            zip_desc = st.text_area(
                t("slib_desc", lang=lang),
                key="slib_zip_desc",
                height=80,
                help=t("slib_desc_help", lang=lang),
            )
            if st.button(t("slib_install_btn", lang=lang), key="slib_zip_btn",
                         type="primary", use_container_width=True):
                try:
                    result = import_skill_from_zip(
                        zip_file.getvalue(),
                        name=zip_name or None,
                        description=zip_desc,
                    )
                    if result.get("ok"):
                        st.success(t("slib_installed", lang=lang,
                                       name=result["skill"]["name"]))
                        st.rerun()
                except SkillsLibraryError as e:
                    st.error(t("slib_error", lang=lang, error=str(e)))

    # ── GitHub ──
    with tab_github:
        gh_url = st.text_input(
            t("slib_github_url", lang=lang),
            key="slib_gh_url",
            placeholder=t("slib_github_url_ph", lang=lang),
            help=t("slib_github_url_help", lang=lang),
        )
        gh_name = st.text_input(
            t("slib_name", lang=lang),
            key="slib_gh_name",
            help=t("slib_name_help", lang=lang),
        )
        gh_desc = st.text_area(
            t("slib_desc", lang=lang),
            key="slib_gh_desc",
            height=80,
            help=t("slib_desc_help", lang=lang),
        )
        st.caption(t("slib_github_hint", lang=lang))
        if st.button(t("slib_install_btn", lang=lang), key="slib_gh_btn",
                     type="primary", use_container_width=True):
            if not gh_url.strip():
                st.error(t("slib_github_url_required", lang=lang))
            else:
                with st.spinner(t("slib_installing", lang=lang)):
                    try:
                        result = import_skill_from_github(
                            gh_url,
                            name=gh_name or None,
                            description=gh_desc,
                        )
                        if result.get("ok"):
                            st.success(t("slib_installed", lang=lang,
                                           name=result["skill"]["name"]))
                            st.rerun()
                    except SkillsLibraryError as e:
                        st.error(t("slib_error", lang=lang, error=str(e)))

    # ── Локальная папка ──
    with tab_folder:
        local_path = st.text_input(
            t("slib_folder_path", lang=lang),
            key="slib_folder_path",
            placeholder="/path/to/skill/folder",
            help=t("slib_folder_path_help", lang=lang),
        )
        local_name = st.text_input(
            t("slib_name", lang=lang),
            key="slib_folder_name",
            help=t("slib_name_help", lang=lang),
        )
        local_desc = st.text_area(
            t("slib_desc", lang=lang),
            key="slib_folder_desc",
            height=80,
            help=t("slib_desc_help", lang=lang),
        )
        if st.button(t("slib_install_btn", lang=lang), key="slib_folder_btn",
                     type="primary", use_container_width=True):
            if not local_path.strip():
                st.error(t("slib_folder_required", lang=lang))
            else:
                try:
                    result = import_skill_from_folder(
                        local_path,
                        name=local_name or None,
                        description=local_desc,
                    )
                    if result.get("ok"):
                        st.success(t("slib_installed", lang=lang,
                                       name=result["skill"]["name"]))
                        st.rerun()
                except SkillsLibraryError as e:
                    st.error(t("slib_error", lang=lang, error=str(e)))


def _request_skill_adaptation(skill_id: str, name: str, lang: str) -> None:
    """Передать задачу адаптации навыка в DevAgent (Skill Developer).

    Кнопка «Адаптировать» не меняет статус навыка напрямую: статус поменяет
    только DevAgent по завершении адаптации (инструмент mark_skill_adapted).
    """
    from core.orchestrators import DEVAGENT_SLUG
    import ui.pages.orchestrator as _orch_page

    slug = DEVAGENT_SLUG
    _orch_page._init_orch_state(slug)
    _orch_page._reset_dialog(slug)
    task = t("slib_adapt_task", lang=lang, name=name, skill_id=skill_id)
    st.session_state[f"orch_{slug}_pending_task"] = task
    st.session_state["last_active_entity_type"] = "orchestrator"
    st.session_state["last_active_entity_id"] = slug
    st.session_state["current_page"] = f"orchestrator:{slug}"
    st.rerun()


def _render_skills_list(lang: str) -> None:
    """Рендер списка установленных навыков с кнопками редактирования/адаптации."""
    st.subheader(t("slib_list_title", lang=lang))
    skills = list_skills()
    if not skills:
        st.info(t("slib_empty", lang=lang))
        return

    for skill in skills:
        skill_id = skill["id"]
        adapted = bool(skill.get("adapted"))
        developer = skill.get("developer") or ""
        with st.container(border=True):
            col_info, col_edit, col_adapt = st.columns([6, 1, 1])
            with col_info:
                st.markdown(f"**{skill['name']}**  `{skill['id']}`")
                if skill.get("description"):
                    st.caption(f"📝 {skill['description']}")
                st.caption(f"📁 {skill.get('folder', '')}")
                dev = developer if developer else "-"
                status = t("slib_adapted", lang=lang) if adapted else t("slib_not_adapted", lang=lang)
                st.caption(f"👤 {dev} · {status}")
            with col_edit:
                if st.button(t("slib_edit_btn", lang=lang),
                             key=f"slib_edit_{skill_id}",
                             use_container_width=True):
                    st.session_state["slib_edit_id"] = skill_id
                    st.rerun()
            with col_adapt:
                if adapted:
                    st.caption("✅")
                else:
                    if st.button(t("slib_adapt_btn", lang=lang),
                                 key=f"slib_adapt_{skill_id}",
                                 use_container_width=True,
                                 help=t("slib_adapt_help", lang=lang)):
                        _request_skill_adaptation(skill_id, skill["name"], lang)


def _render_edit_form(lang: str) -> None:
    """Рендер формы редактирования метаданных навыка (без файлов)."""
    skill_id = st.session_state.get("slib_edit_id")
    skill = get_skill(skill_id)
    if skill is None:
        st.error(t("slib_not_found", lang=lang))
        st.session_state["slib_edit_id"] = None
        st.rerun()

    st.subheader(t("slib_edit_title", lang=lang, name=skill["name"]))
    with st.container(border=True):
        new_name = st.text_input(
            t("slib_name", lang=lang),
            value=skill.get("name", ""),
            key="slib_edit_name",
            help=t("slib_name_help", lang=lang),
        )
        new_desc = st.text_area(
            t("slib_desc", lang=lang),
            value=skill.get("description", ""),
            key="slib_edit_desc",
            height=80,
            help=t("slib_desc_help", lang=lang),
        )
        new_folder = st.text_input(
            t("slib_folder", lang=lang),
            value=skill.get("folder", ""),
            key="slib_edit_folder",
            help=t("slib_folder_help", lang=lang),
        )
        st.caption(t("slib_folder_hint", lang=lang))

        col_save, col_cancel, col_del = st.columns([2, 2, 1])
        with col_save:
            if st.button(t("btn_save", lang=lang), key="slib_save_btn",
                         type="primary", use_container_width=True):
                if not new_name.strip():
                    st.error(t("err_name_required", lang=lang))
                else:
                    try:
                        ok = update_skill(
                            skill_id,
                            name=new_name,
                            description=new_desc,
                            folder=new_folder,
                        )
                        if ok:
                            st.session_state["slib_edit_id"] = None
                            st.success(t("slib_saved", lang=lang))
                            st.rerun()
                        else:
                            st.error(t("slib_save_error", lang=lang))
                    except SkillsLibraryError as e:
                        st.error(t("slib_error", lang=lang, error=str(e)))
        with col_cancel:
            if st.button(t("btn_cancel", lang=lang), key="slib_cancel_btn",
                         use_container_width=True):
                st.session_state["slib_edit_id"] = None
                st.rerun()
        with col_del:
            if st.button("🗑", key="slib_del_btn",
                         help=t("slib_delete_help", lang=lang)):
                st.session_state["slib_confirm_delete"] = True
                st.rerun()

        # Inline delete confirmation (destructive action gate).
        if st.session_state.get("slib_confirm_delete"):
            with st.container(border=True):
                st.warning(t("confirm_delete", lang=lang, name=skill["name"]))
                c_yes, c_no = st.columns(2)
                with c_yes:
                    if st.button(t("btn_yes_delete", lang=lang),
                                 key="slib_del_yes_btn",
                                 type="primary", use_container_width=True):
                        delete_skill(skill_id)
                        st.session_state["slib_edit_id"] = None
                        st.session_state["slib_confirm_delete"] = False
                        st.success(t("slib_deleted", lang=lang))
                        st.rerun()
                with c_no:
                    if st.button(t("btn_cancel", lang=lang),
                                 key="slib_del_no_btn",
                                 use_container_width=True):
                        st.session_state["slib_confirm_delete"] = False
                        st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
