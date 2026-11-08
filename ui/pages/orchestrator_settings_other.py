# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings_other - the "Other" settings section.

Two independent settings live here: the four name slots of an employee and
the autonomous-loop step limit. Each has its own save button, because a
rename and a limit change are different intentions and their confirmations
must not overwrite each other.

A symbol the tests may replace through the page facade is resolved late, at
call time, so a patch installed on ``ui.pages.orchestrator`` reaches the
implementation here.
"""
from storage.models import DEFAULT_MAX_STEPS
from ui.components.name_fields import render_name_fields
from ui.pages.orchestrator_shared import late, st_proxy

st = st_proxy()
flash_success = late("flash_success")
get_orchestrator = late("get_orchestrator")
pin_settings_section = late("pin_settings_section")
save_orchestrator = late("save_orchestrator")
settings_save_failed = late("settings_save_failed")
t = late("t")


def _render_names_block(slug: str, lang: str, orch: dict) -> None:
    """Render the four name inputs and persist them on their own button."""
    names = render_name_fields(lang, slug, orch, st=st)
    if not st.button(t("names_save_btn", lang=lang), key=f"orch_save_names_{slug}",
                     type="primary"):
        return
    if save_orchestrator(
        slug, name=names["name"], name_en=names["name_en"],
        name_ru=names["name_ru"], name_zh=names["name_zh"],
    ):
        pin_settings_section(slug, "other")
        flash_success(t("names_saved", lang=lang))
        st.rerun()
    else:
        settings_save_failed(slug, "other", "orch_other_save_error", lang)


def _render_max_steps_block(slug: str, lang: str, orch: dict) -> None:
    """Render the autonomous-loop step limit and persist it on save."""
    st.markdown(t("orch_other_section", lang=lang))
    st.markdown(t("orch_other_section_desc", lang=lang))

    cur_max_steps = int(orch.get("max_steps", DEFAULT_MAX_STEPS) or DEFAULT_MAX_STEPS)
    sel_max_steps = st.number_input(
        t("orch_max_steps_label", lang=lang),
        min_value=1, max_value=10000,
        value=cur_max_steps, step=50,
        key=f"orch_max_steps_{slug}",
        help=t("orch_max_steps_help", lang=lang),
    )
    st.caption(t("orch_max_steps_hint", lang=lang, default=DEFAULT_MAX_STEPS))

    st.markdown("---")
    if not st.button(t("orch_save_btn", lang=lang), key=f"orch_save_other_{slug}", type="primary"):
        return
    try:
        new_max_steps = int(sel_max_steps)
    except (TypeError, ValueError):
        new_max_steps = cur_max_steps
    new_max_steps = max(1, min(10000, new_max_steps))
    if save_orchestrator(slug, max_steps=new_max_steps):
        pin_settings_section(slug, "other")
        flash_success(t("orch_max_steps_saved", lang=lang, value=new_max_steps))
        st.rerun()
    else:
        settings_save_failed(slug, "other", "orch_other_save_error", lang)


def _render_other_settings(slug: str, lang: str) -> None:
    """Manage miscellaneous orchestrator settings (names and step limit)."""
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return

    _render_names_block(slug, lang, orch)
    st.markdown("---")
    _render_max_steps_block(slug, lang, orch)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
