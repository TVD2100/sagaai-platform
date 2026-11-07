# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_settings_delegation - Settings tab: delegation, limits and other options.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""
from typing import Dict, List

from core.orchestration.contracts import (
    DEFAULT_ANSWER_MAX_CHARS,
    DEFAULT_ANSWER_WAIT_SECONDS,
    DEFAULT_DIALOG_ROUNDS,
    DEFAULT_MAX_PARKED_RUNS,
    DEFAULT_QUESTION_MAX_CHARS,
)
from core.orchestrators import (
    UNIVERSAL_AGENT_SLUG,
    get_verifier_agent,
)
from storage.models import DEFAULT_MAX_STEPS
from ui.pages.orchestrator_shared import late, st_proxy
from ui.pages.orchestrator_text import _dialog_limit

st = st_proxy()
flash_success = late("flash_success")
get_allowed_agents = late("get_allowed_agents")
get_allowed_assistants = late("get_allowed_assistants")
get_delegation_limits = late("get_delegation_limits")
get_orchestrator = late("get_orchestrator")
get_universal_agent_enabled = late("get_universal_agent_enabled")
list_orchestrators = late("list_orchestrators")
pin_settings_section = late("pin_settings_section")
save_orchestrator = late("save_orchestrator")
set_allowed_agents = late("set_allowed_agents")
set_allowed_assistants = late("set_allowed_assistants")
set_delegation_limits = late("set_delegation_limits")
set_universal_agent_enabled = late("set_universal_agent_enabled")
set_verifier_agent = late("set_verifier_agent")
settings_save_failed = late("settings_save_failed")
t = late("t")


def _render_delegation_settings(slug: str, lang: str) -> None:
    """Manage the delegation allowlists and the six runtime limits.

    The agent list is built from LIVE orchestrators and the assistant list
    from LIVE cards, so a deleted entity never lingers as a stale checkbox
    value; ids that are already stored but no longer resolve are shown with
    an explicit warning instead of disappearing silently, because the agent
    would otherwise keep a config entry it can never satisfy at call time.
    """
    from core.assistants import load_assistants_index

    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return

    st.markdown(t("orch_delegation_section", lang=lang))
    st.markdown(t("orch_delegation_section_desc", lang=lang))

    live_orch = _live_orchestrator_entries()
    agents = _render_allowed_agents(slug, lang, live_orch)
    assistants = _render_allowed_assistants(slug, lang, load_assistants_index)
    verifier = _render_verifier_section(slug, lang, live_orch)
    limits_sel = _render_delegation_limits(slug, lang)
    dialog_sel = _render_dialog_limits(slug, lang)

    st.markdown("---")
    if st.button(t("orch_delegation_save_btn", lang=lang),
                 key=f"orch_save_delegation_{slug}", type="primary"):
        _save_delegation_settings(slug, lang, agents, assistants, verifier,
                                  limits_sel, dialog_sel)


def _live_orchestrator_entries() -> list:
    """Return the live orchestrator cards, or an empty list on failure."""
    try:
        return list_orchestrators()
    except Exception:
        return []


def _render_allowed_agents(slug: str, lang: str, live_orch: list) -> dict:
    """Render the spawnable-orchestrator allowlist.

    Returns ``{agents, universal, current_universal}``: the checked slugs, the
    desired Universal-Agent state and the stored one, so the save step can
    tell a real decision from an untouched checkbox.
    """
    st.markdown(t("orch_delegation_agents", lang=lang))
    # The bundled Universal-Agent is a DEFAULT right, not a list entry: it
    # gets its own checkbox so the decision is a visible on/off state instead
    # of an entry a settings save could drop. It is excluded from the loop
    # below - without that, a user who also added it by hand would see two
    # checkboxes for one agent.
    allowed_agents = set(get_allowed_agents(slug))
    universal_present = any(str(e.get("slug") or "") == UNIVERSAL_AGENT_SLUG
                            for e in live_orch)
    current_universal = get_universal_agent_enabled(slug)
    sel_universal = current_universal
    if universal_present and slug != UNIVERSAL_AGENT_SLUG:
        sel_universal = st.checkbox(
            t("orch_delegation_universal_agent", lang=lang),
            value=bool(sel_universal),
            key=f"orch_allowed_universal_{slug}",
            help=t("orch_delegation_universal_agent_help", lang=lang))
        st.caption(t("orch_delegation_universal_agent_hint", lang=lang))
    live_agent_slugs = set()
    selected_agents: List[str] = []
    for entry in live_orch:
        other = str(entry.get("slug") or "")
        if not other or other == slug or other == UNIVERSAL_AGENT_SLUG:
            continue
        live_agent_slugs.add(other)
        name = str(entry.get("name") or other)
        checked = st.checkbox(f"**{name}** `{other}`", value=(other in allowed_agents),
                              key=f"orch_allowed_agent_{slug}_{other}")
        if checked:
            selected_agents.append(other)
    for stale in sorted(allowed_agents - live_agent_slugs):
        st.warning(t("orch_delegation_stale_agent", lang=lang, slug=stale))
        if st.checkbox(f"**{stale}**", value=True,
                       key=f"orch_allowed_agent_stale_{slug}_{stale}"):
            selected_agents.append(stale)
    st.caption(t("orch_delegation_self_hint", lang=lang))
    return {"agents": selected_agents, "universal": sel_universal,
            "current_universal": current_universal}


def _render_allowed_assistants(slug: str, lang: str, load_index) -> List[str]:
    """Render the callable-assistant allowlist and return the checked ids."""
    st.markdown(t("orch_delegation_assistants", lang=lang))
    allowed_assistants = set(get_allowed_assistants(slug))
    try:
        live_assistants = load_index() or []
    except Exception:
        live_assistants = []
    live_assistant_ids = set()
    selected_assistants: List[str] = []
    if not live_assistants:
        st.info(t("orch_delegation_no_assistants", lang=lang))
    for card in live_assistants:
        aid = str(card.get("id") or "")
        if not aid:
            continue
        live_assistant_ids.add(aid)
        name = str(card.get("name") or aid)
        checked = st.checkbox(f"**{name}** `{aid}`", value=(aid in allowed_assistants),
                              key=f"orch_allowed_assistant_{slug}_{aid}")
        if checked:
            selected_assistants.append(aid)
    for stale in sorted(allowed_assistants - live_assistant_ids):
        st.warning(t("orch_delegation_stale_assistant", lang=lang, ident=stale))
    st.caption(t("orch_delegation_assistants_hint", lang=lang))
    return selected_assistants


def _render_verifier_section(slug: str, lang: str, live_orch: list) -> dict:
    """Render the result-verifier picker.

    Returns ``{current, selected}`` so the save step writes the appointment
    only when the choice actually changed.
    """
    # The verifier is a SEPARATE right: it is not required in the spawn
    # allowlist above, and its only purpose is checking a finished result.
    # "Not appointed" is the first entry, so an empty appointment is a
    # deliberate choice rather than a missing selection.
    st.markdown(t("orch_delegation_verifier", lang=lang))
    current_verifier = get_verifier_agent(slug)
    verifier_options: List[str] = [""]
    verifier_labels: Dict[str, str] = {"": t("orch_delegation_verifier_none", lang=lang)}
    for entry in live_orch:
        other = str(entry.get("slug") or "")
        if not other:
            continue
        verifier_options.append(other)
        verifier_labels[other] = "%s `%s`" % (str(entry.get("name") or other), other)
    if current_verifier and current_verifier not in verifier_options:
        # A deleted employee must not vanish silently: it stays listed so the
        # user sees what the stored configuration still names.
        st.warning(t("orch_delegation_stale_verifier", lang=lang, slug=current_verifier))
        verifier_options.append(current_verifier)
        verifier_labels[current_verifier] = current_verifier
    sel_verifier = st.selectbox(
        t("orch_delegation_verifier_label", lang=lang),
        options=verifier_options,
        index=verifier_options.index(current_verifier) if current_verifier in verifier_options else 0,
        format_func=lambda value: verifier_labels.get(value, value),
        key=f"orch_delegation_verifier_{slug}",
        help=t("orch_delegation_verifier_help", lang=lang),
    )
    st.caption(t("orch_delegation_verifier_hint", lang=lang))
    return {"current": current_verifier, "selected": sel_verifier}


def _number_input_setting(slug: str, lang: str, label_key: str, help_key: str,
                          key_suffix: str, value: int, min_value: int, max_value: int,
                          step: int):
    """Render one labelled numeric setting of the delegation block."""
    return st.number_input(
        t(label_key, lang=lang),
        min_value=min_value, max_value=max_value, value=int(value), step=step,
        key=f"orch_{key_suffix}_{slug}",
        help=t(help_key, lang=lang))


def _render_delegation_limits(slug: str, lang: str) -> dict:
    """Render the six runtime limits and return their new values."""
    st.markdown(t("orch_delegation_limits", lang=lang))
    limits = get_delegation_limits(slug)
    col_a, col_b = st.columns(2)
    with col_a:
        sel_depth = _number_input_setting(slug, lang, "orch_delegation_max_depth",
                                          "orch_delegation_max_depth_help",
                                          "delegation_max_depth", limits["max_depth"], 0, 10, 1)
        sel_pool = _number_input_setting(slug, lang, "orch_delegation_max_active",
                                         "orch_delegation_max_active_help",
                                         "delegation_max_active", limits["max_active_subagents"], 1, 64, 1)
        sel_steps = _number_input_setting(slug, lang, "orch_delegation_max_steps",
                                          "orch_delegation_max_steps_help",
                                          "delegation_max_steps", limits["subagent_max_steps"], 1, 1000, 1)
    with col_b:
        sel_total = _number_input_setting(slug, lang, "orch_delegation_total_runs",
                                          "orch_delegation_total_runs_help",
                                          "delegation_total_runs", limits["total_subagent_runs"], 1, 1000, 1)
        sel_timeout = _number_input_setting(slug, lang, "orch_delegation_timeout",
                                            "orch_delegation_timeout_help",
                                            "delegation_timeout", limits["subagent_timeout_seconds"], 1, 3600, 30)
        sel_budget = _number_input_setting(slug, lang, "orch_delegation_budget",
                                           "orch_delegation_budget_help",
                                           "delegation_budget", limits["subagent_budget_tokens"], 0, 10000000, 1000)
    return {"max_depth": sel_depth, "max_active_subagents": sel_pool,
            "subagent_max_steps": sel_steps, "total_subagent_runs": sel_total,
            "subagent_timeout_seconds": sel_timeout,
            "subagent_budget_tokens": sel_budget}


def _render_dialog_limits(slug: str, lang: str) -> dict:
    """Render the agent-dialog limits and return their new values."""
    # The dialog lives in the SAME block as the other delegation limits: a
    # separate section would let the two sets of numbers drift apart, and a
    # user looking for "how long a child may wait" would not know where to
    # look. Each value is clamped by the resolver, so a typed 0 becomes the
    # documented default instead of removing the limit.
    st.markdown(t("orch_dialog_section", lang=lang))
    st.caption(t("orch_dialog_section_desc", lang=lang))
    limits = get_delegation_limits(slug)
    col_c, col_d = st.columns(2)
    with col_c:
        sel_rounds = _number_input_setting(slug, lang, "orch_dialog_rounds",
                                           "orch_dialog_rounds_help", "dialog_rounds",
                                           _dialog_limit(limits, "dialog_rounds", DEFAULT_DIALOG_ROUNDS),
                                           1, 20, 1)
        sel_question_chars = _number_input_setting(slug, lang, "orch_dialog_question_chars",
                                                   "orch_dialog_question_chars_help",
                                                   "dialog_question_chars",
                                                   _dialog_limit(limits, "question_max_chars", DEFAULT_QUESTION_MAX_CHARS),
                                                   100, 20000, 100)
        sel_parked = _number_input_setting(slug, lang, "orch_dialog_max_parked",
                                           "orch_dialog_max_parked_help", "dialog_max_parked",
                                           _dialog_limit(limits, "max_parked_runs", DEFAULT_MAX_PARKED_RUNS),
                                           1, 64, 1)
    with col_d:
        sel_answer_chars = _number_input_setting(slug, lang, "orch_dialog_answer_chars",
                                                 "orch_dialog_answer_chars_help",
                                                 "dialog_answer_chars",
                                                 _dialog_limit(limits, "answer_max_chars", DEFAULT_ANSWER_MAX_CHARS),
                                                 100, 20000, 100)
        sel_wait = _number_input_setting(slug, lang, "orch_dialog_wait",
                                         "orch_dialog_wait_help", "dialog_wait",
                                         _dialog_limit(limits, "answer_wait_seconds", DEFAULT_ANSWER_WAIT_SECONDS),
                                         1, 3600, 60)
    return {"dialog_rounds": sel_rounds, "question_max_chars": sel_question_chars,
            "answer_max_chars": sel_answer_chars, "answer_wait_seconds": sel_wait,
            "max_parked_runs": sel_parked}


def _save_delegation_settings(slug: str, lang: str, agents: dict, assistants: list,
                              verifier: dict, limits_sel: dict, dialog_sel: dict) -> None:
    """Persist the delegation block and report the outcome."""
    ok_agents = set_allowed_agents(slug, agents["agents"])
    ok_assistants = set_allowed_assistants(slug, assistants)
    # Written only when the checkbox actually changed its state: a save of
    # the other settings must not record a decision the user never made.
    ok_universal = True
    if bool(agents["universal"]) != bool(agents["current_universal"]):
        ok_universal = set_universal_agent_enabled(slug, bool(agents["universal"]))
    # Written only when the choice actually changed: a save of the other
    # settings must not touch the verifier appointment, and a no-op write
    # would still sync the orchestrator folder.
    selected_verifier = str(verifier["selected"] or "")
    ok_verifier = True
    if selected_verifier != verifier["current"]:
        ok_verifier = set_verifier_agent(slug, selected_verifier)
    merged = dict(limits_sel)
    merged.update(dialog_sel)
    ok_limits = set_delegation_limits(slug, {key: int(value) for key, value in merged.items()})
    if ok_agents and ok_assistants and ok_limits and ok_verifier and ok_universal:
        pin_settings_section(slug, "delegation")
        flash_success(t("orch_delegation_saved", lang=lang))
        st.rerun()
    else:
        settings_save_failed(slug, "delegation", "orch_delegation_save_error", lang)


def _render_other_settings(slug: str, lang: str) -> None:
    """Manage miscellaneous orchestrator settings (autonomous-loop step limit)."""
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return

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
    if st.button(t("orch_save_btn", lang=lang), key=f"orch_save_other_{slug}", type="primary"):
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
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
