# -*- coding: utf-8 -*-
"""Settings panel of the execution sandbox.

The panel answers ONE question - which access mode this employee's generated
code runs under - and everything it renders follows from that answer: the
modes are a single choice, the chosen mode shows the facts of the profile it
selects, and the machinery behind it (profiles, providers) stays behind
"details" instead of competing with the choice.

The modes are one choice and not independent switches: a run has exactly one
operating-system boundary, so two checkboxes could only disagree. ``off`` and
full access both drop that boundary and differ in the environment the child
receives - ``off`` keeps the allowlist and hides the platform keys, full
access hands the platform environment over - which is why the two are named
and explained apart instead of being left to look identical.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from core.i18n import t
from core.sandbox.profiles import (
    DEFAULT_PROFILE,
    LEGACY_PROCESS,
    OFF,
    READ_ONLY,
    WORKSPACE_WRITE,
)
from ui.pages.orchestrator_flags import (
    ACCESS_FULL,
    ACCESS_PARTIAL,
    access_mode,
    sandbox_profile,
)
from ui.pages.orchestrator_shared import late, st_proxy

# Resolved through the page facade on every access: the test suite swaps the
# whole streamlit module for a double, and a module-level binding would keep
# writing into the double of whichever test imported this module first.
st = st_proxy()

flash_error = late("flash_error")
flash_success = late("flash_success")
get_orchestrator = late("get_orchestrator")
pin_settings_section = late("pin_settings_section")
save_orchestrator = late("save_orchestrator")
settings_save_button = late("settings_save_button")

#: The mode that grants the platform environment. It is not a profile: the
#: resolver forces the ``off`` profile onto such a run, so the setting names
#: the grant and the profile follows from it.
MODE_FULL = "full"

#: The modes in the order the selector lists them.
SECURITY_MODES: Tuple[str, ...] = (READ_ONLY, WORKSPACE_WRITE, LEGACY_PROCESS,
                                   OFF, MODE_FULL)

#: Label key of each listed mode.
_MODE_LABEL_KEYS = {
    READ_ONLY: "orch_security_mode_read_only",
    WORKSPACE_WRITE: "orch_security_mode_workspace_write",
    LEGACY_PROCESS: "orch_security_mode_legacy_process",
    OFF: "orch_security_mode_off",
    MODE_FULL: "orch_security_mode_full",
}

#: The two profiles that never depend on the machine: they isolate nothing.
_NO_ISOLATION = (LEGACY_PROCESS, OFF)


def _provider_rows() -> List[Dict[str, Any]]:
    """Return one row per known provider with its availability and guarantees."""
    from core.sandbox.providers import default_providers

    rows = []
    for provider in default_providers():
        try:
            ready = bool(provider.available())
        except Exception:
            ready = False
        rows.append({"name": getattr(provider, "name", "?"),
                     "isolation": getattr(provider, "isolation", "none"),
                     "capabilities": ", ".join(getattr(provider, "capabilities", ()) or ()),
                     "available": ready})
    return rows


def _profile_rows(providers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return one row per profile and whether this machine can honour it.

    A provider qualifies only when it reaches the required isolation, stays
    within the profile's isolation ceiling AND declares every capability the
    profile needs: an ``os`` backend that leaves the network reachable cannot
    honour a no-network profile, and choosing it would silently weaken the
    guarantee. The ceiling works the other way round - a profile that asks
    for no isolation must not be reported as running under the strongest
    backend this machine has.
    """
    from core.sandbox.contracts import satisfies
    from core.sandbox.policy import within_isolation_ceiling

    rows = []
    for profile in _profiles():
        usable = [p for p in providers
                  if p["available"] and satisfies(p["isolation"], profile.isolation)
                  and within_isolation_ceiling(p["isolation"], profile.max_isolation)
                  and _offers(p, profile)]
        rows.append({"profile": profile, "usable": bool(usable),
                     "via": usable[0]["name"] if usable else ""})
    return rows


def _offers(provider: Dict[str, Any], profile: Any) -> bool:
    """True when *provider* declares every capability *profile* requires."""
    offered = {item.strip() for item in str(provider.get("capabilities") or "").split(",")
               if item.strip()}
    return set(getattr(profile, "requires", ()) or ()).issubset(offered)


def _profiles():
    """Return the declared profiles (imported late to keep the panel cheap)."""
    from core.sandbox.profiles import all_profiles

    return list(all_profiles())


def _employee_config(slug: str) -> Dict[str, Any]:
    """Return a mutable copy of the config of *slug*, or an empty mapping."""
    try:
        orch = get_orchestrator(slug) or {}
    except Exception:
        orch = {}
    config = orch.get("config") if isinstance(orch, dict) else None
    return dict(config) if isinstance(config, dict) else {}


def _current_mode(slug: str) -> str:
    """Return the mode the runs of this employee execute under right now.

    A record that cleared the removed toolbar checkbox reads as ``off``: the
    machine is open while the environment is still filtered, which is exactly
    what that mode means. An unset profile reads as the policy default rather
    than as an empty selector, which would misreport the state.
    """
    state = access_mode(get_orchestrator, slug)
    if state == ACCESS_FULL:
        return MODE_FULL
    if state == ACCESS_PARTIAL:
        return OFF
    return str(sandbox_profile(get_orchestrator, slug) or DEFAULT_PROFILE)


def _mode_options(current: str) -> List[str]:
    """Return the listed modes plus a stored one that is no longer listed.

    A profile saved by an older build keeps its own entry: the selector must
    show what is in force instead of quietly pretending another mode runs.
    """
    options = list(SECURITY_MODES)
    if current and current not in options:
        options.append(str(current))
    return options


def _mode_label(mode: str, lang: str) -> str:
    """Return the selector label of *mode*."""
    key = _MODE_LABEL_KEYS.get(mode)
    if key:
        return t(key, lang=lang)
    return t("orch_security_mode_extra", lang=lang, name=mode)


def _facts_row(mode: str, rows: Dict[str, Dict[str, Any]], lang: str) -> Dict[str, str]:
    """Return the profile table row of *mode*.

    The columns are the ones the panel always reported, so the choice and the
    diagnostics describe the same run in the same words.
    """
    yes = t("orch_sandbox_yes", lang=lang)
    no = t("orch_sandbox_no", lang=lang)
    if mode == MODE_FULL:
        return {"profile": "%s (%s)" % (t("orch_access_option_full", lang=lang), OFF),
                "isolation": "none",
                "network": yes,
                "via": "-",
                "status": t("orch_sandbox_available", lang=lang)}
    row = rows.get(mode)
    if row is None:
        return {"profile": mode, "isolation": "-", "network": "-", "via": "-",
                "status": t("orch_sandbox_refused", lang=lang)}
    profile = row["profile"]
    return {"profile": profile.name,
            "isolation": profile.isolation,
            "network": yes if profile.network else no,
            "via": row["via"] or "-",
            "status": (t("orch_sandbox_available", lang=lang) if row["usable"]
                       else t("orch_sandbox_refused", lang=lang))}


def _mode_usable(mode: str, rows: Dict[str, Dict[str, Any]]) -> bool:
    """True when this machine can honour the run *mode* selects."""
    if mode == MODE_FULL or mode in _NO_ISOLATION:
        return True
    row = rows.get(mode)
    return bool(row and row["usable"])


def _render_mode_facts(mode: str, rows: Dict[str, Dict[str, Any]], lang: str) -> None:
    """Show the facts of the chosen mode, with what it does to the environment."""
    st.markdown(t("orch_security_selected_profile", lang=lang))
    st.table([_facts_row(mode, rows, lang)])
    if mode == MODE_FULL:
        st.caption(t("orch_security_env_full", lang=lang))
    elif mode == OFF:
        st.caption(t("orch_security_env_off", lang=lang))
    else:
        st.caption(t("orch_security_env_isolated", lang=lang))


def _save_mode(slug: str, mode: str, lang: str) -> None:
    """Persist the chosen mode as one coherent pair of config values.

    The access grant and the profile are two keys of the same decision, so a
    save writes both: leaving the other one behind would let a mode the user
    has just replaced decide the next run.
    """
    config = _employee_config(slug)
    # The removed toolbar switch is dropped from the record: left in place it
    # would keep overriding the profile chosen here.
    config.pop("chat_sandbox", None)
    if mode == MODE_FULL:
        config["env_isolation"] = False
        config["sandbox_profile"] = OFF
    else:
        config["env_isolation"] = True
        config["sandbox_profile"] = str(mode)
    _save_config(slug, config, lang=lang, mode=mode)


def _save_config(slug: str, config: Dict[str, Any], lang: str, mode: str) -> None:
    """Write the config, keep the section open and report the outcome."""
    try:
        ok = bool(save_orchestrator(slug, config=config))
    except Exception:
        ok = False
    if not ok:
        st.error(t("orch_security_save_error", lang=lang))
        return
    pin_settings_section(slug, "security")
    flash_success(t("orch_security_saved", lang=lang, mode=_mode_label(mode, lang)))
    st.rerun()


def _render_active_runs(lang: str) -> None:
    """Show the runs that are executing right now, with a live tail.

    A long run is the one case where a blocking call is visible to the user
    as a frozen page: the panel therefore follows the log file of every
    running child and offers a Stop button per run. The tail is read from
    the end of the file, so following a chatty run costs the same as
    following a quiet one.
    """
    from core.sandbox import active

    try:
        runs = active.active_runs()
    except Exception:
        runs = []
    if not runs:
        return

    @st.fragment(run_every=2)
    def _live() -> None:
        """Re-render the list while at least one run is alive."""
        try:
            current = active.active_runs()
        except Exception:
            current = []
        if not current:
            # The run finished: one last rerun replaces the cards with the
            # journal row, which is the durable record of the same run.
            st.rerun()
            return
        st.markdown(t("orch_sandbox_active_runs", lang=lang))
        for run in current:
            _render_one_run(run, lang)

    _live()


def _render_one_run(run: Dict[str, Any], lang: str) -> None:
    """Render one running child: its facts, its tail and its Stop button."""
    from core.sandbox import active

    title = "%s - %s (%ss)" % (run.get("tool") or "?", run.get("profile") or "?",
                               run.get("elapsed_s"))
    with st.expander(title, expanded=False):
        st.caption(t("orch_sandbox_run_facts", lang=lang, pid=run.get("pid") or "?",
                     thread=(run.get("thread_id") or "-")))
        lines = active.tail(int(run.get("key") or 0), 20)
        if lines:
            st.code("\n".join(lines), language="text")
        else:
            st.caption(t("orch_sandbox_no_output", lang=lang))
        if st.button(t("orch_sandbox_stop_run", lang=lang),
                     key=f"orch_sandbox_stop_{run.get('key')}",
                     type="secondary"):
            stopped = active.cancel_run(int(run.get("key") or 0))
            if stopped:
                st.success(t("orch_sandbox_run_stopped", lang=lang))
            st.rerun()


def render_sandbox_settings(slug: str, lang: str) -> None:
    """Render the Security section of the settings page.

    The section answers ONE question the user actually asks - what may this
    agent's code reach - as a single choice. The chosen mode shows the facts
    of its profile at once, so the difference between the modes (the boundary
    and the environment are two of them) is visible where the choice is made
    instead of being inferred from a second widget.

    Args:
        slug: the employee whose settings are being edited.
        lang: the active UI language.
    """
    st.markdown(t("orch_access_section", lang=lang))
    st.caption(t("orch_access_section_help", lang=lang))

    current = _current_mode(slug)
    if access_mode(get_orchestrator, slug) == ACCESS_PARTIAL:
        # A record that cleared the old sandbox checkbox before the access
        # setting existed. Neither answer is safe to assume for the owner, so
        # the section states what is in force and asks for an explicit choice.
        st.warning(t("orch_access_partial_banner", lang=lang))

    options = _mode_options(current)
    chosen = st.radio(
        t("orch_access_label", lang=lang),
        options=options,
        index=options.index(current) if current in options else 0,
        format_func=lambda value: _mode_label(value, lang),
        key=f"orch_access_mode_{slug}",
        help=t("orch_security_modes_help", lang=lang),
    )

    rows = {row["profile"].name: row for row in _profile_rows(_provider_rows())}
    _render_mode_facts(chosen, rows, lang)

    if chosen == MODE_FULL:
        st.error(t("orch_access_full_warning", lang=lang))
    usable = _mode_usable(chosen, rows)
    if not usable:
        # Offering the choice is honest; saving a mode this machine cannot
        # honour is not - the run would be refused by the policy.
        st.warning(t("orch_sandbox_refused", lang=lang))
    if usable and settings_save_button(t("orch_save_btn", lang=lang),
                                       key=f"orch_access_save_{slug}", type="primary"):
        _save_mode(slug, chosen, lang)

    with st.expander(t("orch_access_details", lang=lang), expanded=False):
        providers = _provider_rows()
        st.markdown(t("orch_sandbox_providers", lang=lang))
        st.table([{"provider": p["name"], "isolation": p["isolation"],
                   "capabilities": p["capabilities"] or "-",
                   "available": t("orch_sandbox_yes" if p["available"]
                                  else "orch_sandbox_no", lang=lang)}
                  for p in providers])

        st.markdown(t("orch_sandbox_profiles", lang=lang))
        st.table([{"profile": row["profile"].name,
                   "isolation": row["profile"].isolation,
                   "network": t("orch_sandbox_yes" if row["profile"].network
                                else "orch_sandbox_no", lang=lang),
                   "via": row["via"] or "-",
                   "status": (t("orch_sandbox_available", lang=lang)
                              if row["usable"] else t("orch_sandbox_refused", lang=lang))}
                  for row in rows.values()])

        if not any(row["usable"] for row in rows.values()):
            st.warning(t("orch_sandbox_no_provider", lang=lang))

    _render_active_runs(lang)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
