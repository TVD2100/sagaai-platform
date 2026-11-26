# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings_economy - Settings tab: economy mode.

The section owns one decision - how much of a dialog the chat keeps in view
and how it caches the prompt prefix - and renders it as a single block: the
tail size, the cache switch and the multiplier, with the save button that
reports its result beneath itself.

Every name the tests may replace through the page facade is resolved late, at
call time, so a patch installed on ``ui.pages.orchestrator`` reaches the code
here.
"""

from ui.pages.orchestrator_shared import late, st_proxy

st = st_proxy()
flash_success = late("flash_success")
get_economy_config = late("get_economy_config")
get_economy_tail_messages = late("get_economy_tail_messages")
get_orchestrator = late("get_orchestrator")
pin_settings_section = late("pin_settings_section")
save_orchestrator = late("save_orchestrator")
settings_save_button = late("settings_save_button")
settings_save_failed = late("settings_save_failed")
t = late("t")


def _render_economy_settings(slug: str, lang: str) -> None:
    """Render the economy block of the settings page.

    A legacy value outside the slider range is clamped instead of rejected:
    the stored number stays as it was, and the next save writes the value the
    user actually sees.
    """
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return
    economy_config = get_economy_config(slug)
    cur_tail = min(100, max(4, int(economy_config.get("tail_messages", get_economy_tail_messages(slug)))))
    cur_cache_enabled = bool(economy_config.get("cache_enabled", False))
    cur_multiplier = int(economy_config.get("cache_multiplier", 2))

    st.markdown(t("orch_economy_section", lang=lang))
    st.markdown(t("orch_economy_desc", lang=lang))

    col_t1, col_t2 = st.columns([2, 1])
    with col_t1:
        sel_tail = st.slider(
            t("orch_economy_tail_label", lang=lang),
            min_value=4, max_value=100, value=cur_tail, step=2,
            key=f"orch_economy_tail_{slug}",
            help=t("orch_economy_tail_help", lang=lang),
        )
    with col_t2:
        st.metric("L", sel_tail)

    sel_cache_enabled = st.checkbox(
        t("orch_economy_cache_label", lang=lang),
        value=cur_cache_enabled,
        key=f"orch_economy_cache_{slug}",
        help=t("orch_economy_cache_help", lang=lang),
    )

    multiplier_options = [1, 2, 3, 4, 5]
    if cur_multiplier not in multiplier_options:
        cur_multiplier = 2
    sel_multiplier = st.selectbox(
        t("orch_economy_multiplier_label", lang=lang),
        options=multiplier_options,
        index=multiplier_options.index(cur_multiplier),
        format_func=lambda x: f"x{x}",
        key=f"orch_economy_multiplier_{slug}",
        help=t("orch_economy_multiplier_help", lang=lang),
    )

    if settings_save_button(t("orch_save_btn", lang=lang),
                            key=f"orch_save_economy_{slug}", type="primary"):
        cfg = orch.get("config", {})
        new_cfg = dict(cfg)
        new_cfg["economy_tail_messages"] = sel_tail
        new_cfg["economy_cache_enabled"] = bool(sel_cache_enabled)
        new_cfg["economy_cache_multiplier"] = int(sel_multiplier)
        if save_orchestrator(slug, config=new_cfg):
            pin_settings_section(slug, "economy")
            flash_success(t("orch_economy_saved", lang=lang, tail=sel_tail))
            st.rerun()
        else:
            settings_save_failed(slug, "economy", "orch_economy_save_error", lang)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
