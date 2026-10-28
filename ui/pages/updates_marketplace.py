"""
ui.pages.updates_marketplace - marketplace entities inside the Updates page.

The platform updater keeps its own sections on that page; this module adds
the marketplace ones below them: one block per entity type, the available
updates with the path to take for edited files, and the run journal with
the undo of a finished run.

All four entity types are installable, so every block can hold rows. A
block with nothing to show renders its empty state deliberately instead of
being hidden: the section has to say that the type exists and that there is
no update for it, otherwise the page looks broken when the first agent
arrives.

Everything here is paint: the decisions live in ``core.marketplace``.
Nothing contacts the catalog until the user presses the check button, and
that is the reason the section renders a "press to check" caption rather
than an empty list on first visit.
"""
from __future__ import annotations

from typing import List

import streamlit as st

from core.i18n import t
from core.marketplace import config as marketplace_config
from core.marketplace import rollback, store
from core.marketplace import updates as marketplace_updates
from ui.components.flash import flash_error, flash_success


#: The four entity types, in the order the page lists them.
SECTIONS = (
    ("skill", "mp_upd_skills"),
    ("agent", "mp_upd_agents"),
    ("rag", "mp_upd_rag"),
    ("mcp", "mp_upd_mcp"),
)

#: Update policies, in display order, with their label keys.
POLICIES = (
    ("keep_mine", "mp_upd_policy_keep"),
    ("take_catalog", "mp_upd_policy_take"),
    ("skip", "mp_upd_policy_skip"),
)

_REPORT_KEY = "mp_upd_report"
_POLICY_KEY = "mp_upd_policy_choice"
_JOURNAL_LIMIT = 10


# ─── Helpers ──────────────────────────────────────────────────────────────

def _report() -> dict:
    """Return the report of the last check, or an empty dict."""
    data = st.session_state.get(_REPORT_KEY)
    return data if isinstance(data, dict) else {}


def _policy(lang: str) -> str:
    """Render the policy choice and return its machine value.

    The choice is asked once for the whole block: an edited installation is
    the exception, not the rule, and one question per row would bury the
    updates themselves under widgets.
    """
    labels = {t(key, lang=lang): value for value, key in POLICIES}
    chosen = st.radio(
        t("mp_upd_policy", lang=lang),
        options=list(labels),
        key=_POLICY_KEY,
    )
    return labels.get(str(chosen), "keep_mine")


def _run_update(ids: List[str], policy: str, lang: str) -> None:
    """Apply updates for *ids* and report the outcome through the flash queue."""
    if not ids:
        return
    try:
        with st.spinner(t("mp_upd_applying", lang=lang)):
            report = marketplace_updates.apply_entity_updates(ids, policy)
    except store.MarketplaceError as exc:
        flash_error(t("mp_upd_failed", lang=lang, message=exc.message))
        st.rerun()
        return
    except Exception as exc:  # pragma: no cover - unexpected transport failure
        flash_error(t("mp_upd_failed", lang=lang, message=str(exc)))
        st.rerun()
        return

    applied = report.get("applied") or []
    if applied:
        names = ", ".join(
            str(item.get("name") or item.get("id") or "") for item in applied
        )
        flash_success(t("mp_upd_applied", lang=lang, names=names))
    for item in report.get("failed") or []:
        flash_error(
            t(
                "mp_upd_failed",
                lang=lang,
                message=str(item.get("message") or item.get("error_code") or ""),
            )
        )
    st.session_state.pop(_REPORT_KEY, None)
    st.rerun()


def _undo(install_id: str, lang: str) -> None:
    """Undo one finished run and report the outcome."""
    try:
        report = rollback.rollback_run(install_id, reason="user")
    except Exception as exc:
        flash_error(t("mp_upd_undo_failed", lang=lang, message=str(exc)))
        st.rerun()
        return
    if report.get("ok"):
        flash_success(t("mp_upd_undo_ok", lang=lang, install_id=install_id))
    else:
        flash_error(
            t(
                "mp_upd_undo_failed",
                lang=lang,
                message=", ".join(str(item) for item in report.get("unrestored") or []),
            )
        )
    st.session_state.pop(_REPORT_KEY, None)
    st.rerun()


# ─── Rows ─────────────────────────────────────────────────────────────────

def _render_item(item: dict, policy: str, lang: str) -> None:
    """Render one catalog entry: a row, a metadata note or a refusal.

    A metadata refresh and a platform refusal are not updates - they carry
    no files to install - so they get their own one-line note instead of an
    update button that could not work.
    """
    action = str(item.get("action") or "")
    name = str(item.get("name") or item.get("id") or "")

    if action == "metadata":
        st.caption(t("mp_upd_metadata", lang=lang, name=name))
        return
    if action == "platform_required":
        st.warning(
            t(
                "mp_upd_platform",
                lang=lang,
                name=name,
                reason=str(item.get("required_platform") or ""),
            )
        )
        return

    entity_id = str(item.get("id") or "")
    type_ = str(item.get("type") or "skill")

    line = t(
        "mp_upd_row",
        lang=lang,
        name=name,
        installed=str(item.get("installed_version") or "?"),
        available=str(item.get("version") or "?"),
    )
    if item.get("user_modified"):
        line = f"{line} · {t('mp_upd_modified', lang=lang)}"
    # An agent update replaces either an employee bundle or a helper profile:
    # the row names which one so the user knows what the update touches.
    kind = str(item.get("agent_kind") or "")
    if type_ == "agent" and kind:
        kind_label = t(f"mp_agent_kind_{kind}", lang=lang)
        line = f"{line} · {t('mp_agent_kind', lang=lang, kind=kind_label or kind)}"
    st.markdown(f"- {line}")
    if item.get("license_required"):
        st.caption(t("mp_upd_license_hint", lang=lang))

    entity_id = str(item.get("id") or "")
    ref = f"{type_}:{entity_id}"
    if st.button(t("mp_upd_apply", lang=lang), key=f"mp_upd_apply_{type_}_{entity_id}"):
        _run_update([ref], policy, lang)


# ─── Journal ──────────────────────────────────────────────────────────────

def _journal_records() -> List[dict]:
    """Return the newest marketplace runs, oldest first."""
    records = [record for record in store.load_history() if record.get("type")]
    return records[-_JOURNAL_LIMIT:]


def _render_journal(lang: str) -> None:
    """Run journal: what happened, and the undo of a finished run."""
    st.markdown(f"**{t('mp_upd_journal', lang=lang)}**")
    records = _journal_records()
    if not records:
        st.caption(t("mp_upd_journal_empty", lang=lang))
        return

    for record in reversed(records):
        install_id = str(record.get("install_id") or "")
        st.markdown(
            "- "
            + t(
                "mp_upd_journal_row",
                lang=lang,
                action=str(record.get("action") or record.get("type") or ""),
                name=str(record.get("id") or ""),
                status=str(record.get("status") or ""),
                when=str(record.get("started_at") or ""),
            )
        )
        # Only a finished run with backup metadata can be undone: a failed
        # run already rolled itself back, and a running one holds the lock.
        if str(record.get("status")) != "done" or not install_id:
            continue
        if store.read_run_meta(install_id) is None:
            continue
        if st.button(t("mp_upd_undo", lang=lang), key=f"mp_upd_undo_{install_id}"):
            _undo(install_id, lang)


# ─── Section ──────────────────────────────────────────────────────────────

def render_marketplace_sections(lang: str = "en") -> None:
    """Render the marketplace subsections of the Updates page.

    Returns without painting anything when the master switch is off: the
    page then shows only the platform updater, and the marketplace leaves
    no trace beyond its empty tables.
    """
    if not marketplace_config.is_enabled():
        return

    st.subheader(f"\U0001f6d2 {t('mp_upd_title', lang=lang)}")
    st.caption(t("mp_upd_hint", lang=lang))

    if st.button(t("mp_upd_check", lang=lang), key="mp_upd_check"):
        with st.spinner(t("mp_upd_checking", lang=lang)):
            try:
                report = marketplace_updates.check_entity_updates()
            except store.MarketplaceError as exc:
                report = {"ok": False, "message": exc.message, "items": []}
            except Exception as exc:  # pragma: no cover - unexpected failure
                report = {"ok": False, "message": str(exc), "items": []}
        st.session_state[_REPORT_KEY] = report

    report = _report()
    if report:
        _render_report(report, lang)
    else:
        st.caption(t("mp_upd_idle", lang=lang))

    st.markdown("---")
    _render_journal(lang)


def _render_report(report: dict, lang: str) -> None:
    """Render the outcome of one check: the counters, the rows, the action."""
    if not report.get("ok", True):
        st.error(t("mp_upd_check_failed", lang=lang, message=str(report.get("message") or "")))
        return

    items = report.get("items") or []
    st.caption(
        t(
            "mp_upd_checked",
            lang=lang,
            checked=int(report.get("checked") or 0),
            found=len(items),
        )
    )
    for entry in report.get("unavailable") or []:
        st.caption(
            t(
                "mp_upd_unavailable",
                lang=lang,
                name=str(entry.get("id") or ""),
                reason=str(entry.get("reason") or ""),
            )
        )

    policy = _policy(lang)
    for type_, title_key in SECTIONS:
        st.markdown(f"**{t(title_key, lang=lang)}**")
        section_items = report.get(type_) or []
        if not section_items:
            st.caption(t("mp_upd_section_empty", lang=lang))
            continue
        for item in section_items:
            _render_item(item, policy, lang)

    updatable = [
        f"{item.get('type') or 'skill'}:{item.get('id') or ''}"
        for item in items
        if str(item.get("action") or "") == "update"
    ]
    if len(updatable) > 1:
        if st.button(t("mp_upd_apply_all", lang=lang), key="mp_upd_apply_all"):
            _run_update(updatable, policy, lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
