# -*- coding: utf-8 -*-
"""Settings panel of the execution sandbox.

The panel answers two questions before anything runs: which profiles this
machine can actually honour, and which one this employee's generated code
executes under. A profile whose isolation or guarantees are unavailable is
marked as such and cannot be saved - the run would be refused by the policy,
and offering the choice anyway would promise a guarantee the platform cannot
keep.

The run list is read-only: it reports what is executing right now, so a run
that the user forgot about is visible instead of silently holding a process.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from core.i18n import t
from core.sandbox.profiles import LEGACY_PROCESS, OFF
from ui.pages.orchestrator_shared import late, st_proxy

# Resolved through the page facade on every access: the test suite swaps the
# whole streamlit module for a double, and a module-level binding would keep
# writing into the double of whichever test imported this module first.
st = st_proxy()

flash_success = late("flash_success")
get_orchestrator = late("get_orchestrator")
save_orchestrator = late("save_orchestrator")

#: Label of the legacy profile in the selector (it has no sandbox of its own).
_LEGACY_LABEL_KEY = "orch_sandbox_profile_legacy"
#: Label of the profile the toolbar checkbox switches to when it is cleared.
_OFF_LABEL_KEY = "orch_sandbox_profile_off"
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


def _current_profile(slug: str) -> str:
    """Return the profile this employee's generated code runs under."""
    try:
        orch = get_orchestrator(slug) or {}
    except Exception:
        orch = {}
    config = orch.get("config") or {}
    return str(config.get("sandbox_profile") or LEGACY_PROCESS)


def _profile_label(profile: Any, usable: bool, lang: str) -> str:
    """Return the selector label of *profile* with its availability."""
    if profile.name == LEGACY_PROCESS:
        return "%s (%s)" % (profile.name, t(_LEGACY_LABEL_KEY, lang=lang))
    if profile.name == OFF:
        return "%s (%s)" % (profile.name, t(_OFF_LABEL_KEY, lang=lang))
    status = (t("orch_sandbox_available", lang=lang) if usable
              else t("orch_sandbox_refused", lang=lang))
    return "%s - %s" % (profile.name, status)


def _save_profile(slug: str, profile: str, lang: str) -> None:
    """Persist the chosen profile and rerun the page."""
    _save_config_value(slug, "sandbox_profile", str(profile or ""), lang)


def _save_config_value(slug: str, key: str, value, lang: str) -> None:
    """Persist one config value of the employee and rerun the page."""
    try:
        orch = get_orchestrator(slug) or {}
        config = dict(orch.get("config") or {})
        config[key] = value
        ok = bool(save_orchestrator(slug, config=config))
    except Exception:
        ok = False
    if ok:
        try:
            flash_success(t("orch_sandbox_profile_saved", lang=lang, profile=value))
        except Exception:
            st.success(t("orch_sandbox_profile_saved", lang=lang, profile=value))
        st.rerun()
        return
    st.error(t("orch_sandbox_profile_error", lang=lang))


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
    """Render the access section of the settings page.

    The section answers ONE question the user actually asks - may this agent
    reach the machine? Everything else on the panel describes how the answer
    is implemented, so it lives behind "details" and never competes with the
    choice itself.

    Args:
        slug: the employee whose settings are being edited.
        lang: the active UI language.
    """
    from core.sandbox.profiles import OFF
    from ui.pages.orchestrator_flags import (
        ACCESS_PARTIAL,
        access_mode,
        env_isolation,
    )

    st.markdown(t("orch_access_section", lang=lang))
    st.caption(t("orch_access_section_help", lang=lang))

    mode = access_mode(get_orchestrator, slug)
    if mode == ACCESS_PARTIAL:
        # A record that cleared the old sandbox checkbox before the access
        # setting existed. Neither answer is safe to assume for the owner, so
        # the section states what is in force and asks for an explicit choice.
        st.warning(t("orch_access_partial_banner", lang=lang))

    isolated_now = env_isolation(get_orchestrator, slug)
    options = [True, False]
    chosen = st.radio(
        t("orch_access_label", lang=lang),
        options=options,
        index=0 if isolated_now else 1,
        format_func=lambda value: (t("orch_access_option_isolated", lang=lang)
                                   if value else t("orch_access_option_full", lang=lang)),
        key=f"orch_access_mode_{slug}",
        help=t("orch_access_help", lang=lang),
    )
    if chosen is False:
        st.error(t("orch_access_full_warning", lang=lang))
    else:
        st.caption(t("orch_access_isolated_hint", lang=lang))

    if st.button(t("orch_save_btn", lang=lang), key=f"orch_access_save_{slug}",
                 type="primary"):
        _save_config_value(slug, "env_isolation", bool(chosen), lang)

    # The profile only matters inside the isolated mode: a full-access run is
    # forced to ``off`` by the resolver, so offering the selector there would
    # promise a choice that does not take effect.
    with st.expander(t("orch_access_details", lang=lang), expanded=False):
        providers = _provider_rows()
        st.markdown(t("orch_sandbox_providers", lang=lang))
        st.table([{"provider": p["name"], "isolation": p["isolation"],
                   "capabilities": p["capabilities"] or "-",
                   "available": t("orch_sandbox_yes" if p["available"]
                                  else "orch_sandbox_no", lang=lang)}
                  for p in providers])

        rows = _profile_rows(providers)
        st.markdown(t("orch_sandbox_profiles", lang=lang))
        st.table([{"profile": row["profile"].name,
                   "isolation": row["profile"].isolation,
                   "network": t("orch_sandbox_yes" if row["profile"].network
                                else "orch_sandbox_no", lang=lang),
                   "via": row["via"] or "-",
                   "status": (t("orch_sandbox_available", lang=lang)
                              if row["usable"] else t("orch_sandbox_refused", lang=lang))}
                  for row in rows])

        if not any(row["usable"] for row in rows):
            st.warning(t("orch_sandbox_no_provider", lang=lang))

        current = _current_profile(slug)
        by_name = {row["profile"].name: row for row in rows}
        names = [profile.name for profile in _profiles()]
        if current and current not in names:
            names.insert(0, current)
        index = names.index(current) if current in names else 0
        chosen_profile = st.selectbox(
            t("orch_sandbox_profile_label", lang=lang), options=names, index=index,
            key=f"orch_sandbox_profile_{slug}",
            help=t("orch_sandbox_profile_help", lang=lang),
            disabled=chosen is False,
            format_func=lambda name: _profile_label(
                by_name[name]["profile"] if name in by_name else _stub_profile(name),
                bool(by_name[name]["usable"]) if name in by_name else False, lang),
        )
        profile_ok = (bool(by_name.get(chosen_profile, {}).get("usable"))
                      or chosen_profile in _NO_ISOLATION)
        if chosen is False:
            st.caption(t("orch_access_profile_ignored", lang=lang,
                         profile=OFF))
        elif not profile_ok:
            st.warning(t("orch_sandbox_refused", lang=lang))
        elif st.button(t("orch_save_btn", lang=lang),
                       key=f"orch_sandbox_save_{slug}", type="primary"):
            _save_profile(slug, chosen_profile, lang)

    _render_active_runs(lang)
    _render_recent_runs(lang)


def _render_recent_runs(lang: str) -> None:
    """Show the newest journal rows (read-only).

    The journal is what makes a refusal auditable after the fact: the panel
    reports the decision (refused or executed) and its outcome, while the
    full output stays in the log file the row points to.
    """
    from storage import sandbox_journal

    try:
        from storage.db import get_engine

        rows = sandbox_journal.recent_runs(get_engine(), limit=10)
    except Exception:
        rows = []
    if not rows:
        return
    st.markdown(t("orch_sandbox_recent_runs", lang=lang))
    st.table([{"when": _format_when(row.get("started_at")),
               "run": sandbox_journal.summarise(row)} for row in rows])


def _format_when(value: Any) -> str:
    """Return a journal timestamp as a short local date and time."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return "-"


def _stub_profile(name: str):
    """Return a minimal profile-like object for a name that no longer exists."""
    from types import SimpleNamespace

    return SimpleNamespace(name=name, requires=())

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
