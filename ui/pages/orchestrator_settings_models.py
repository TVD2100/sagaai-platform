# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings_models - Settings tab: models, prompt and economy mode.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""

from core.services import (
    default_reasoning_effort,
    get_fetch_models,
    get_image_models,
    get_model_reasoning_effort_options,
    get_vision_models,
    service_supports_reasoning_effort,
)
from ui.pages.orchestrator_chat_input import (
    _services_with_web_search,
    _temp_slider,
)
from ui.pages.orchestrator_shared import late, st_proxy
from ui.pages.orchestrator_text import (
    _get_max_tokens_limit,
    _model_ids,
)

st = st_proxy()
flash_success = late("flash_success")
get_economy_config = late("get_economy_config")
get_economy_tail_messages = late("get_economy_tail_messages")
get_orchestrator = late("get_orchestrator")
get_services = late("get_services")
get_web_search_config = late("get_web_search_config")
pin_settings_section = late("pin_settings_section")
reset_builtin_prompt = late("reset_builtin_prompt")
save_orchestrator = late("save_orchestrator")
settings_save_failed = late("settings_save_failed")
t = late("t")


def _render_optional_model_section(slug: str, lang: str, cfg: dict, service_names: list,
                                   services: dict, catalog_fn, section_key: str,
                                   no_services_key: str, config_prefix: str,
                                   help_key: str = "") -> tuple:
    """Render one optional model block (image recognition or image generation).

    The service selectbox carries the "not selected" option that switches the
    optional capability off; the model selectbox appears only after a service
    is picked and lists the catalog models declared by that service.

    ``catalog_fn`` is get_vision_models / get_image_models, ``config_prefix``
    is "vision" or "image" (<prefix>_service / <prefix>_model). Returns the
    selected pair; ("", "") means "not assigned".
    """
    st.markdown(t(section_key, lang=lang))
    if help_key:
        st.caption(t(help_key, lang=lang))
    catalog_services = {name: svc for name, svc in services.items() if catalog_fn(svc)}
    if not catalog_services:
        st.caption(t(no_services_key, lang=lang))
        return "", ""
    unset_label = t("orch_model_unset_option", lang=lang)
    cur_svc, cur_mdl = _current_optional_pair(slug, lang, cfg, config_prefix,
                                              catalog_fn, catalog_services)
    svc_options = [unset_label] + [n for n in service_names if n in catalog_services]
    col_a, col_b = st.columns(2)
    with col_a:
        picked = st.selectbox(t("orch_service_label", lang=lang), options=svc_options,
                              index=svc_options.index(cur_svc) if cur_svc in svc_options else 0,
                              key=f"orch_set_{config_prefix}_svc_{slug}",
                              help=t("orch_service_label_help", lang=lang))
    sel_svc = "" if picked == unset_label else picked
    sel_mdl = ""
    if sel_svc:
        model_ids = [m["id"] if isinstance(m, dict) else m
                     for m in catalog_fn(catalog_services[sel_svc])]
        with col_b:
            sel_mdl = st.selectbox(t("orch_model_label", lang=lang), options=model_ids,
                                   index=model_ids.index(cur_mdl) if cur_mdl in model_ids else 0,
                                   key=f"orch_set_{config_prefix}_mdl_{slug}",
                                   help=t("orch_model_label_help", lang=lang))
    return sel_svc, sel_mdl


def _current_optional_pair(slug: str, lang: str, cfg: dict, config_prefix: str,
                           catalog_fn, catalog_services: dict) -> tuple:
    """Return the stored (service, model) pair, warning about stale entries.

    A stored service that the catalog no longer offers, or a stored model that
    the service no longer declares, is reported and cleared so the selectboxes
    fall back to their first option instead of silently keeping a dead pair.
    """
    cur_svc = str(cfg.get(config_prefix + "_service", "") or "")
    cur_mdl = str(cfg.get(config_prefix + "_model", "") or "")
    if cur_svc and cur_svc not in catalog_services:
        st.warning(t("orch_service_unavailable", lang=lang, service=cur_svc))
        return "", ""
    if cur_svc:
        cur_ids = [m["id"] if isinstance(m, dict) else m
                   for m in catalog_fn(catalog_services[cur_svc])]
        if cur_mdl and cur_mdl not in cur_ids:
            st.warning(t("orch_model_unavailable", lang=lang,
                         model=cur_mdl, service=cur_svc))
            cur_mdl = ""
    return cur_svc, cur_mdl


def _render_models_settings(slug: str, lang: str) -> None:
    """Render the model-assignment settings of one orchestrator.

    One main model serves every task; the optional capabilities (vision,
    image generation, page fetch) are separate assignments that stay off
    while unset. The web-search model is a separate provider pair because
    only services with a search catalog can answer a search request.
    """
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return
    cfg = orch.get("config", {})
    services = get_services()
    service_names = list(services.keys())
    if not service_names:
        st.info(t("orch_no_services", lang=lang))
        return

    strong = _render_strong_model_section(slug, lang, cfg, service_names, services)
    optional = _render_optional_capability_sections(slug, lang, cfg, service_names, services)
    search = _render_search_model_section(slug, lang, cfg)

    # Save models
    if st.button(t("orch_save_models_btn", lang=lang), key=f"orch_save_models_{slug}", type="primary"):
        _save_models_settings(slug, lang, cfg, strong, optional, search)


def _warn_unavailable_pair(lang: str, cfg: dict, svc_key: str, mdl_key: str,
                           service_names: list, services: dict) -> None:
    """Warn when the stored pair names a service or model that is gone.

    A silently missing entry would look like an empty selection while the
    stored value keeps its old pair, so the user never learns that the
    assignment is dead.
    """
    saved_svc = cfg.get(svc_key, "")
    saved_mdl = cfg.get(mdl_key, "")
    if saved_svc and saved_svc not in service_names:
        st.warning(t("orch_service_unavailable", lang=lang, service=saved_svc))
    elif saved_svc in service_names:
        if saved_mdl and saved_mdl not in _model_ids(services.get(saved_svc, {})):
            st.warning(t("orch_model_unavailable", lang=lang,
                         model=saved_mdl, service=saved_svc))


def _render_reasoning_effort(slug: str, lang: str, svc_def: dict, model: str,
                             current: str, key_suffix: str, strong: bool) -> str:
    """Render the reasoning-effort selector when the service supports it."""
    if not service_supports_reasoning_effort(svc_def):
        return ""
    effort = str(current or "").strip()
    options = get_model_reasoning_effort_options(svc_def, model)
    if not effort or effort not in options:
        effort = default_reasoning_effort(svc_def, strong=strong, model=model) or (options[0] if options else "")
    if not options:
        return ""
    return st.selectbox(
        t("orch_reasoning_effort_label", lang=lang),
        options=options,
        index=options.index(effort) if effort in options else 0,
        key=f"orch_set_{key_suffix}_{slug}",
        help=t("orch_reasoning_effort_label_help", lang=lang),
    )


def _render_strong_model_section(slug: str, lang: str, cfg: dict,
                                 service_names: list, services: dict) -> dict:
    """Render the main model pair, its temperature and its token limit."""
    # Main model (single model for all tasks)
    st.markdown(t("orch_strong_model_section", lang=lang))
    cur_strong_svc = cfg.get("strong_service", "") or service_names[0]
    if cur_strong_svc not in service_names:
        cur_strong_svc = service_names[0]
    cur_strong_mdl = cfg.get("strong_model", "")
    cur_strong_temp = float(cfg.get("strong_temperature", 0.4) or 0.4)

    _warn_unavailable_pair(lang, cfg, "strong_service", "strong_model",
                           service_names, services)

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        sel_strong_svc = st.selectbox(t("orch_service_label", lang=lang), options=service_names,
                                       index=service_names.index(cur_strong_svc),
                                       key=f"orch_set_strong_svc_{slug}",
                                       help=t("orch_service_label_help", lang=lang))
    with col_s2:
        model_ids = _model_ids(services.get(sel_strong_svc, {}))
        if cur_strong_mdl not in model_ids and model_ids:
            cur_strong_mdl = model_ids[0]
        mdl_idx = model_ids.index(cur_strong_mdl) if cur_strong_mdl in model_ids else 0
        sel_strong_mdl = st.selectbox(t("orch_model_label", lang=lang), options=model_ids if model_ids else [cur_strong_mdl],
                                       index=mdl_idx, key=f"orch_set_strong_mdl_{slug}",
                                       help=t("orch_model_label_help", lang=lang))

    strong_svc_def = services.get(sel_strong_svc, {})
    sel_strong_temp = _temp_slider(strong_svc_def, lang, t("orch_temperature_label", lang=lang), cur_strong_temp,
                                     f"orch_set_strong_temp_{slug}")

    sel_strong_re = _render_reasoning_effort(
        slug, lang, strong_svc_def, sel_strong_mdl,
        cfg.get("strong_reasoning_effort", ""), "strong_re", strong=True)

    cur_strong_max_tokens = int(cfg.get("strong_max_tokens", 0) or 0)
    strong_limit = _get_max_tokens_limit(strong_svc_def, sel_strong_mdl)
    if cur_strong_max_tokens > strong_limit:
        cur_strong_max_tokens = strong_limit
    sel_strong_max_tokens = st.number_input(t("orch_max_tokens", lang=lang), min_value=0, max_value=strong_limit,
                                             value=cur_strong_max_tokens, step=256,
                                             help=t("orch_max_tokens_help", lang=lang),
                                             key=f"orch_set_strong_max_tokens_{slug}")
    st.caption(t("orch_max_tokens_hint", lang=lang, max=f"{strong_limit:,}"))
    return {"service": sel_strong_svc, "model": sel_strong_mdl,
            "temperature": sel_strong_temp, "max_tokens": sel_strong_max_tokens,
            "reasoning_effort": sel_strong_re}


def _render_optional_capability_sections(slug: str, lang: str, cfg: dict,
                                         service_names: list, services: dict) -> dict:
    """Render the vision, image-generation and page-fetch assignments.

    Optional image-model sections carry ("", "") while the capability stays
    switched off. Selenium is an alternative transport: while it is on, the
    page-fetch selectors are hidden because the main model converts the page,
    but the stored pair is kept and comes back when the checkbox is cleared.
    """
    vision_svc, vision_mdl = _render_optional_model_section(
        slug, lang, cfg, service_names, services, get_vision_models,
        "orch_vision_model_section", "orch_no_vision_services", "vision",
        help_key="orch_vision_model_help")
    image_svc, image_mdl = _render_optional_model_section(
        slug, lang, cfg, service_names, services, get_image_models,
        "orch_image_model_section", "orch_no_image_services", "image",
        help_key="orch_image_model_help")
    sel_fetch_selenium = st.checkbox(
        t("orch_fetch_selenium_label", lang=lang),
        value=bool(cfg.get("fetch_selenium")),
        key=f"orch_set_fetch_selenium_{slug}",
        help=t("orch_fetch_selenium_help", lang=lang),
    )

    # Page fetch is a per-provider Search/Fetch API: only services that
    # declare a fetch catalog are offered. An unassigned pair keeps the
    # url_fetch tool absent from the orchestrator's tool set.
    if sel_fetch_selenium:
        st.caption(t("orch_fetch_selenium_main_model", lang=lang))
        fetch_svc = str(cfg.get("fetch_service", "") or "")
        fetch_mdl = str(cfg.get("fetch_model", "") or "")
    else:
        fetch_svc, fetch_mdl = _render_optional_model_section(
            slug, lang, cfg, service_names, services, get_fetch_models,
            "orch_fetch_model_section", "orch_no_fetch_services", "fetch",
            help_key="orch_fetch_model_help")
    return {"vision_service": vision_svc, "vision_model": vision_mdl,
            "image_service": image_svc, "image_model": image_mdl,
            "fetch_service": fetch_svc, "fetch_model": fetch_mdl,
            "fetch_selenium": sel_fetch_selenium}


def _render_search_model_section(slug: str, lang: str, cfg: dict) -> dict:
    """Render the web-search provider pair, its limits and its prompt."""
    st.markdown(t("orch_search_model_section", lang=lang))
    search_services = _services_with_web_search()
    search_svc_names = list(search_services.keys())
    cur_search_mdl = cfg.get("search_model", "")
    cur_search_temp = float(cfg.get("search_temperature", 0.3) or 0.3)
    cur_search_mtc = int(cfg.get("search_max_tool_calls", 3) or 3)

    if not search_svc_names:
        st.warning(t("orch_no_search_services", lang=lang))
        search = {"service": "", "model": "", "temperature": 0.3,
                  "max_tool_calls": 3, "reasoning_effort": ""}
    else:
        search = _render_search_pair(slug, lang, cfg, search_services, search_svc_names,
                                     cur_search_mdl, cur_search_temp, cur_search_mtc)

    search["prompt"] = _render_search_prompt(slug, lang, cfg)
    return search


def _render_search_pair(slug: str, lang: str, cfg: dict, search_services: dict,
                        search_svc_names: list, cur_search_mdl: str,
                        cur_search_temp: float, cur_search_mtc: int) -> dict:
    """Render the search service/model pair with its limits."""
    cur_search_svc = cfg.get("search_service", "")
    if cur_search_svc not in search_svc_names:
        cur_search_svc = search_svc_names[0] if search_svc_names else ""

    _warn_unavailable_pair(lang, cfg, "search_service", "search_model",
                           search_svc_names, search_services)

    col_wb1, col_wb2 = st.columns(2)
    with col_wb1:
        sel_search_svc = st.selectbox(t("orch_service_label", lang=lang), options=search_svc_names,
                                       index=search_svc_names.index(cur_search_svc) if cur_search_svc in search_svc_names else 0,
                                       key=f"orch_set_search_svc_{slug}",
                                       help=t("orch_service_label_help", lang=lang))
    with col_wb2:
        model_ids_search = _model_ids(search_services.get(sel_search_svc, {}))
        if not model_ids_search:
            model_ids_search = [cur_search_mdl] if cur_search_mdl else []
        if cur_search_mdl not in model_ids_search and model_ids_search:
            cur_search_mdl = model_ids_search[0]
        mdl_idx_s = model_ids_search.index(cur_search_mdl) if cur_search_mdl in model_ids_search else 0
        sel_search_mdl = st.selectbox(t("orch_model_label", lang=lang), options=model_ids_search,
                                       index=mdl_idx_s, key=f"orch_set_search_mdl_{slug}",
                                       help=t("orch_model_label_help", lang=lang))

    search_svc_def = search_services.get(sel_search_svc, {})
    sel_search_temp = _temp_slider(search_svc_def, lang, t("orch_temperature_label", lang=lang), cur_search_temp,
                                    f"orch_set_search_temp_{slug}")
    sel_search_mtc = st.slider(t("orch_max_tool_calls", lang=lang), min_value=1, max_value=7,
                                value=cur_search_mtc, step=1, key=f"orch_set_search_mtc_{slug}",
                                help=t("orch_max_tool_calls_help", lang=lang))
    sel_search_re = _render_reasoning_effort(
        slug, lang, search_svc_def, sel_search_mdl,
        cfg.get("search_reasoning_effort", ""), "search_re", strong=False)
    return {"service": sel_search_svc, "model": sel_search_mdl,
            "temperature": sel_search_temp, "max_tool_calls": sel_search_mtc,
            "reasoning_effort": sel_search_re}


def _render_search_prompt(slug: str, lang: str, cfg: dict) -> str:
    """Render the web-search system prompt, falling back to the global one."""
    cur_search_prompt = str(cfg.get("web_search_prompt", "") or "").strip()
    if not cur_search_prompt:
        cur_search_prompt = get_web_search_config(slug).get("prompt", "")
    return st.text_area(
        t("orch_search_prompt_label", lang=lang),
        value=cur_search_prompt,
        height=120,
        key=f"orch_set_search_prompt_{slug}",
        help=t("orch_search_prompt_help", lang=lang),
    )


def _save_models_settings(slug: str, lang: str, cfg: dict, strong: dict,
                          optional: dict, search: dict) -> None:
    """Persist the model block and report the outcome."""
    new_cfg = dict(cfg)
    new_cfg["strong_service"] = strong["service"]
    new_cfg["strong_model"] = strong["model"]
    new_cfg["strong_temperature"] = strong["temperature"]
    new_cfg["strong_max_tokens"] = strong["max_tokens"]
    new_cfg["strong_reasoning_effort"] = strong["reasoning_effort"]
    new_cfg.update(optional)
    # Single-model mode: drop legacy weak_* leftovers on save.
    for _legacy_key in ("weak_service", "weak_model", "weak_temperature",
                        "weak_max_tokens", "weak_reasoning_effort"):
        new_cfg.pop(_legacy_key, None)
    new_cfg["search_service"] = search["service"]
    new_cfg["search_model"] = search["model"]
    new_cfg["search_temperature"] = search["temperature"]
    new_cfg["search_max_tool_calls"] = search["max_tool_calls"]
    new_cfg["search_reasoning_effort"] = search["reasoning_effort"]
    new_cfg["web_search_prompt"] = search["prompt"]
    if save_orchestrator(slug, config=new_cfg):
        pin_settings_section(slug, "models")
        flash_success(t("orch_save_models_ok", lang=lang))
        st.rerun()
    else:
        settings_save_failed(slug, "models", "orch_save_models_error", lang)


def _render_prompt_settings(slug: str, lang: str) -> None:
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return
    prompt_text = orch.get("prompt_text", "")

    st.markdown(t("orch_prompt_section", lang=lang))
    st.caption(t("orch_prompt_label_help", lang=lang))
    is_builtin = bool(orch.get("is_builtin"))
    cfg = orch.get("config", {}) or {}
    prompt_edited = is_builtin and cfg.get("prompt_user_edited") is True
    if prompt_edited:
        st.info(t("orch_prompt_edited_notice", lang=lang))
    new_prompt = st.text_area(
        label=t("orch_prompt_label", lang=lang),
        value=prompt_text,
        height=400,
        key=f"orch_prompt_{slug}",
        label_visibility="collapsed",
    )
    if st.button(t("orch_save_prompt_btn", lang=lang), key=f"orch_save_prompt_{slug}", type="primary"):
        if save_orchestrator(slug, prompt_text=new_prompt):
            # Drop the widget state so the field re-reads the stored prompt,
            # then ask the page to reopen this section after the rerun.
            st.session_state.pop(f"orch_prompt_{slug}", None)
            pin_settings_section(slug, "prompt")
            flash_success(t("orch_save_prompt_ok", lang=lang))
            st.rerun()
        else:
            settings_save_failed(slug, "prompt", "orch_save_prompt_error", lang)
    if prompt_edited:
        if st.button(t("orch_prompt_reset_btn", lang=lang), key=f"orch_reset_prompt_{slug}"):
            if reset_builtin_prompt(slug):
                st.session_state.pop(f"orch_prompt_{slug}", None)
                pin_settings_section(slug, "prompt")
                flash_success(t("orch_prompt_reset_ok", lang=lang))
                st.rerun()
            else:
                settings_save_failed(slug, "prompt", "orch_prompt_reset_error", lang)


def _render_economy_settings(slug: str, lang: str) -> None:
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

    if st.button(t("orch_save_btn", lang=lang), key=f"orch_save_economy_{slug}", type="primary"):
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
