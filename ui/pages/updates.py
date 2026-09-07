# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""
ui.pages.updates - the self-update page: check, select, stage, apply, rollback.

SagaAI ships updates through a public GitHub repository. The updater pipeline
(core.updater / core.updater_apply) fetches the file_versions.json manifest and
raw file payloads without any token, stages them under
.dev_agent/updates/pending/ while the application is running, and applies them
atomically at the next cold start (see the hook in app.py). Code packages
(units) install only as a whole; selectable content files can be picked
individually.

This page therefore offers Check / Download (staging) controls in the live
process and keeps Apply / Rollback disabled while the running marker exists,
with a hint to restart the application.

All user-facing strings go through t(key, lang=lang).
"""
import streamlit as st

from core.i18n import t
from core.updater import (
    DEFAULT_CHANNEL,
    apply_updates,
    check_updates,
    default_root,
    fetch_manifest,
    is_app_running,
    rollback_updates,
    stage_updates,
)
from core.updater_apply import list_backup_runs, load_health, load_state, read_pending
from core.version import __version__


def _cached_manifest():
    """Return the last successfully fetched manifest (or None)."""
    cached = st.session_state.get("updates_manifest")
    return cached if isinstance(cached, dict) else None


def _cached_report():
    """Return the last check_updates report (or None)."""
    report = st.session_state.get("updates_report")
    return report if isinstance(report, dict) else None


def _cached_channel():
    """Return the update channel used for the last check (or the default)."""
    return st.session_state.get("updates_channel") or DEFAULT_CHANNEL


def _action_label(item, lang):
    """Localized 'new' / 'update' label for an available update."""
    if item.get("action") == "new":
        return t("updates_action_new", lang=lang)
    return t("updates_action_update", lang=lang)


def _split_updates(manifest, available):
    """Split available updates into {unit_name: [items]} and [selectable items]."""
    manifest = manifest or {}
    unit_of = {}
    for unit_name, unit in (manifest.get("units") or {}).items():
        files = unit.get("files") if isinstance(unit, dict) else None
        if not isinstance(files, dict):
            continue
        for rel in files:
            unit_of[rel] = unit_name
    units = {}
    selectables = []
    for item in available:
        rel = item.get("path", "")
        unit_name = unit_of.get(rel)
        if unit_name:
            units.setdefault(unit_name, []).append(item)
        else:
            selectables.append(item)
    return units, selectables


def _selected_paths(units, selectables):
    """Collect the paths ticked by the rendered checkboxes."""
    selection = []
    for unit_name, items in units.items():
        if st.session_state.get(f"upd_unit_{unit_name}"):
            selection.extend(item.get("path", "") for item in items)
    for item in selectables:
        rel = item.get("path", "")
        if st.session_state.get(f"upd_sel_{rel}"):
            selection.append(rel)
    return selection


def _handle_stage(lang, root, units, selectables):
    """Download the ticked files into the pending store."""
    selection = _selected_paths(units, selectables)
    if not selection:
        st.warning(t("updates_stage_none", lang=lang))
        return
    with st.spinner(t("updates_staging", lang=lang)):
        result = stage_updates(
            root,
            selection=selection,
            manifest=_cached_manifest(),
            channel=_cached_channel(),
        )
    if result.get("ok"):
        st.success(
            t("updates_stage_done", lang=lang, count=len(result.get("staged") or []))
        )
    else:
        st.error(t("updates_stage_error", lang=lang, error=result.get("error")))


def _render_check_section(lang, root):
    """Version/channel status, the check button and the upgrade selection."""
    st.subheader(t("updates_check_btn", lang=lang))
    st.caption(t("updates_current_version", lang=lang, version=__version__))
    st.caption(t("updates_channel_label", lang=lang, channel=_cached_channel()))

    if st.button(t("updates_check_btn", lang=lang), key="updates_check"):
        with st.spinner(t("updates_checking", lang=lang)):
            manifest, manifest_err = fetch_manifest(_cached_channel())
        if manifest_err is not None:
            st.session_state["updates_manifest"] = None
            st.session_state["updates_report"] = {"ok": False, "error": manifest_err}
        else:
            st.session_state["updates_manifest"] = manifest
            st.session_state["updates_channel"] = _cached_channel()
            st.session_state["updates_report"] = check_updates(
                root, manifest=manifest, channel=_cached_channel()
            )

    report = _cached_report()
    if report is None:
        return
    if not report.get("ok"):
        st.error(t("updates_check_error", lang=lang, error=report.get("error")))
        return

    app_version = report.get("app_version") or {}
    if app_version.get("newer_remote"):
        st.info(
            t(
                "updates_new_version",
                lang=lang,
                version=app_version.get("remote"),
                local=app_version.get("local"),
            )
        )

    available = report.get("available") or []
    if not available:
        st.success(t("updates_up_to_date", lang=lang))
        return

    st.markdown(f"**{t('updates_found_title', lang=lang)}**")
    units, selectables = _split_updates(_cached_manifest(), available)

    if selectables:
        st.caption(t("updates_selectable_hint", lang=lang))
        for item in selectables:
            rel = item.get("path", "")
            st.checkbox(
                f"`{rel}` - {_action_label(item, lang)} - v{item.get('version', '')}",
                key=f"upd_sel_{rel}",
            )

    for unit_name, items in units.items():
        unit = (_cached_manifest() or {}).get("units", {}).get(unit_name) or {}
        with st.expander(
            t(
                "updates_unit_label",
                lang=lang,
                name=unit_name,
                version=unit.get("version", "?"),
            )
        ):
            for item in items:
                st.markdown(
                    f"- `{item.get('path', '')}` - {_action_label(item, lang)}"
                    f" - v{item.get('version', '')}"
                )
            st.caption(t("updates_unit_hint", lang=lang))
        st.checkbox(
            t("updates_unit_install", lang=lang, name=unit_name),
            key=f"upd_unit_{unit_name}",
        )

    if st.button(t("updates_stage_btn", lang=lang), key="updates_stage"):
        _handle_stage(lang, root, units, selectables)


def _render_pending_section(lang, root):
    """Staged files and the apply button (disabled while the app is live)."""
    st.subheader(t("updates_pending_title", lang=lang))
    pending, _errors = read_pending(root)
    files = (pending or {}).get("files") or {}
    if not files:
        st.caption(t("updates_pending_empty", lang=lang))
        return
    for rel, entry in files.items():
        version = (entry or {}).get("version", "")
        st.markdown(f"- `{rel}` - v{version}")
    running = is_app_running(root)
    if running:
        st.info(t("updates_apply_running_hint", lang=lang))
    if st.button(
        t("updates_apply_btn", lang=lang), key="updates_apply", disabled=running
    ):
        result = apply_updates(root)
        if result.get("ok"):
            st.success(
                t("updates_apply_done", lang=lang, count=len(result.get("applied") or []))
            )
        else:
            st.error(t("updates_apply_error", lang=lang, error=result.get("error")))


def _render_state_section(lang, root):
    """Applied-state summary, backup runs and the rollback button."""
    st.subheader(t("updates_state_title", lang=lang))
    state = load_state(root)
    last = state.get("last_run") or {}
    if last:
        outcome = "OK" if last.get("ok") else (last.get("error") or "error")
        st.caption(
            t(
                "updates_state_last_run",
                lang=lang,
                result=outcome,
                time=last.get("at", "-"),
            )
        )
    st.caption(
        t("updates_state_applied", lang=lang, count=len(state.get("entries") or {}))
    )
    runs = list_backup_runs(root)
    if not runs:
        st.caption(t("updates_rollback_none", lang=lang))
        return
    st.caption(t("updates_backup_runs", lang=lang, count=len(runs)))
    running = is_app_running(root)
    if running:
        st.info(t("updates_rollback_hint", lang=lang))
    if st.button(
        t("updates_rollback_btn", lang=lang), key="updates_rollback", disabled=running
    ):
        result = rollback_updates(root)
        if result.get("ok"):
            st.success(
                t(
                    "updates_rollback_done",
                    lang=lang,
                    count=len(result.get("restored") or []),
                )
            )
        else:
            st.error(t("updates_rollback_error", lang=lang, error=result.get("error")))


def _render_health_section(lang, root):
    """Outcome of the last cold-start apply (health.json)."""
    health = load_health(root)
    if not isinstance(health, dict):
        return
    if health.get("ok"):
        st.success(t("updates_health_ok", lang=lang))
    else:
        st.warning(
            t("updates_health_error", lang=lang, error=health.get("error") or "-")
        )


def page_updates():
    """Updates page: check for updates, choose files, stage, apply/rollback."""
    lang = st.session_state.get("ui_lang")
    root = default_root()
    st.title(f"\U0001f504 {t('nav_updates', lang=lang)}")
    _render_health_section(lang, root)
    _render_check_section(lang, root)
    st.markdown("---")
    _render_pending_section(lang, root)
    st.markdown("---")
    _render_state_section(lang, root)
