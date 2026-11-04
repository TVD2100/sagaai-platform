# -*- coding: utf-8 -*-
"""
ui.pages.schedules - the "Run on a schedule" settings section.

The page owns the whole lifecycle of a rule: creating it from a form,
editing, pausing, running once by hand, reading its history and deleting it.
One product decision shapes the whole layout - a scheduled run IS a dialog -
so every run row links to the dialog it produced instead of showing a
separate log the owner would have to trust.

The page is honest about the one limitation the architecture has: runs fire
while the platform process is alive. That is stated at the top, with the
scheduler heartbeat, because a silently dead scheduler is worse than a
visibly stopped one.
"""
import streamlit as st

from core.i18n import t
from core.orchestrators import DEVAGENT_SLUG, list_orchestrators
from core.schedules import daemon, form, store
from core.schedules.model import (
    EXCEPTION_POLICIES,
    INTERVAL_UNITS,
    MISFIRE_POLICIES,
    OVERLAP_POLICIES,
    STATUS_ACTIVE,
    STATUS_ERROR_HOLD,
    STATUS_FINISHED,
    STATUS_PAUSED,
    Schedule,
    format_quiet_hours,
    new_schedule_id,
    now_epoch,
)
from ui.components.flash import flash_error, flash_success, flash_warning

#: Statuses whose runs are still in flight; the history shows them as live.
_LIVE_STATUSES = ("started",)

#: Localized labels, kept as explicit keys: a key built by concatenation is
#: invisible to the translation check, and an untranslated status then shows
#: up as a raw identifier in the interface.
_STATUS_KEYS = {
    "active": "sched_status_active",
    "running": "sched_status_running",
    "paused": "sched_status_paused",
    "error": "sched_status_error",
    "finished": "sched_status_finished",
    "overdue": "sched_status_overdue",
}
_UNIT_KEYS = {
    "minutes": "sched_unit_minutes",
    "hours": "sched_unit_hours",
    "days": "sched_unit_days",
    "weeks": "sched_unit_weeks",
    "months": "sched_unit_months",
}
_DAYS_KEYS = {
    "every_day": "sched_days_every_day",
    "weekdays_only": "sched_days_weekdays_only",
    "weekends_only": "sched_days_weekends_only",
    "custom": "sched_days_custom",
}
_EXCEPTION_KEYS = {"skip": "sched_exception_skip", "postpone": "sched_exception_postpone"}
_MISFIRE_KEYS = {"skip": "sched_misfire_skip", "catch_up_one": "sched_misfire_catch_up_one"}
_OVERLAP_KEYS = {
    "skip": "sched_overlap_skip",
    "queue_one": "sched_overlap_queue_one",
    "cancel_previous": "sched_overlap_cancel_previous",
}
_WEEKDAY_KEYS = {
    1: "sched_weekday_1",
    2: "sched_weekday_2",
    3: "sched_weekday_3",
    4: "sched_weekday_4",
    5: "sched_weekday_5",
    6: "sched_weekday_6",
    7: "sched_weekday_7",
}
_ERR_KEYS = {
    "title_required": "sched_err_title_required",
    "title_too_long": "sched_err_title_too_long",
    "employee_required": "sched_err_employee_required",
    "task_required": "sched_err_task_required",
    "task_too_long": "sched_err_task_too_long",
    "first_run_required": "sched_err_first_run_required",
    "first_run_past": "sched_err_first_run_past",
    "end_before_start": "sched_err_end_before_start",
    "interval_out_of_range": "sched_err_interval_out_of_range",
    "interval_unit_unknown": "sched_err_interval_unit_unknown",
    "interval_below_minute": "sched_err_interval_below_minute",
    "weekday_invalid": "sched_err_weekday_invalid",
    "too_many_ranges": "sched_err_too_many_ranges",
    "range_reversed": "sched_err_range_reversed",
    "quiet_hours_invalid": "sched_err_quiet_hours_invalid",
    "exception_policy_unknown": "sched_err_exception_policy_unknown",
    "misfire_policy_unknown": "sched_err_misfire_policy_unknown",
    "overlap_policy_unknown": "sched_err_overlap_policy_unknown",
    "max_steps_out_of_range": "sched_err_max_steps_out_of_range",
    "budget_negative": "sched_err_budget_negative",
    "timeout_out_of_range": "sched_err_timeout_out_of_range",
    "status_unknown": "sched_err_status_unknown",
}


def _label(keys: dict, value, lang: str) -> str:
    """Return the localized label of *value*, falling back to the raw value.

    Keys may be strings or integers (weekdays): the lookup coerces both sides
    the same way so a translated label never silently becomes a bare number.
    """
    for key, translation in keys.items():
        if str(key) == str(value):
            return t(translation, lang=lang)
    return str(value)


def page_schedules() -> None:
    """Render the schedule section (dispatched from ui.app)."""
    lang = st.session_state.get("ui_lang", "en")
    st.title(t("sched_title", lang=lang))
    _render_health(lang)

    edit_id = st.session_state.get("sched_edit_id")
    if edit_id:
        _render_form(lang, existing_id=edit_id if edit_id != "new" else "")
        return
    if st.session_state.get("sched_form_open"):
        _render_form(lang, existing_id="")
        return

    if st.button("➕ " + t("sched_new", lang=lang), key="sched_new_btn", type="primary"):
        st.session_state["sched_form_open"] = True
        st.rerun()

    st.markdown("---")
    _render_list(lang)


def _render_health(lang: str) -> None:
    """State whether anything is actually watching the clock right now."""
    alive = daemon.is_alive()
    stamp = daemon.read_heartbeat()
    if alive and stamp:
        st.caption(t("sched_health_ok", lang=lang,
                     when=form.format_moment(stamp, "UTC")))
        return
    st.warning(t("sched_health_off", lang=lang))


# ── List ────────────────────────────────────────────────────────────────────


def _render_list(lang: str) -> None:
    """Render every rule with its status, its next point and its actions."""
    try:
        rules = store.list_schedules()
    except Exception as exc:
        st.error(t("sched_load_failed", lang=lang, error=str(exc)))
        return
    if not rules:
        st.info(t("sched_empty", lang=lang))
        return

    moment = now_epoch()
    running = {row.get("schedule_id") for row in store.unfinished_runs()}
    employees = {item.get("slug"): item.get("name") for item in list_orchestrators()}

    for rule in rules:
        _render_card(lang, rule, moment, bool(rule.id in running), employees)


def _status_label(lang: str, rule: Schedule, moment: int, running: bool) -> str:
    """Return the localized status shown on the card."""
    status = rule.effective_status(moment, running=running)
    return _label(_STATUS_KEYS, status, lang)


def _render_card(lang: str, rule: Schedule, moment: int, running: bool,
                 employees: dict) -> None:
    """Render one rule as a card with its actions."""
    status = rule.effective_status(moment, running=running)
    icon = {"running": "⏳", "active": "🟢", "paused": "⏸️", "error": "🔴",
            "finished": "🏁", "overdue": "⚠️"}.get(status, "⚪")
    name = employees.get(rule.agent_slug) or rule.agent_slug
    with st.container():
        st.markdown("### %s %s" % (icon, rule.title or rule.id))
        detail = t("sched_card_meta", lang=lang, employee=name,
                   interval=form.humanize_interval(rule.interval_value, rule.interval_unit),
                   tz=rule.tz)
        st.caption(detail)
        st.markdown(t("sched_card_next", lang=lang,
                      when=form.format_moment(rule.next_run_at, rule.tz),
                      status=_status_label(lang, rule, moment, running)))
        if rule.status == STATUS_ERROR_HOLD and rule.last_error_code:
            st.error(t("sched_card_error", lang=lang, code=rule.last_error_code))
        elif rule.last_status:
            st.caption(t("sched_card_last", lang=lang,
                         status=rule.last_status,
                         when=form.format_moment(rule.last_run_at, rule.tz),
                         runs=rule.run_count, fails=rule.fail_count))

        cols = st.columns(5)
        _render_card_actions(lang, rule, cols)
        _render_history(lang, rule)
        st.markdown("---")


def _render_card_actions(lang: str, rule: Schedule, cols) -> None:
    """Render the five buttons of one card."""
    with cols[0]:
        if st.button("✏️ " + t("sched_edit", lang=lang), key=f"sched_edit_{rule.id}",
                     use_container_width=True):
            st.session_state["sched_edit_id"] = rule.id
            st.rerun()
    with cols[1]:
        paused = rule.status in (STATUS_PAUSED, STATUS_ERROR_HOLD, STATUS_FINISHED)
        label = "▶️ " + t("sched_resume" if paused else "sched_pause", lang=lang)
        if st.button(label, key=f"sched_toggle_{rule.id}", use_container_width=True):
            _toggle_state(rule, lang)
    with cols[2]:
        if st.button("🚀 " + t("sched_run_now", lang=lang), key=f"sched_run_{rule.id}",
                     use_container_width=True):
            _run_now(rule, lang)
    with cols[3]:
        if st.button("🗑 " + t("sched_delete", lang=lang), key=f"sched_del_{rule.id}",
                     use_container_width=True):
            st.session_state[f"sched_confirm_{rule.id}"] = True
            st.rerun()
    with cols[4]:
        if st.button("🔔 " + t("sched_test_notify", lang=lang), key=f"sched_notify_{rule.id}",
                     use_container_width=True, help=t("sched_test_notify_help", lang=lang)):
            st.caption(t("sched_notify_pending", lang=lang))
    if st.session_state.get(f"sched_confirm_{rule.id}"):
        _render_delete_confirm(lang, rule)


def _render_delete_confirm(lang: str, rule: Schedule) -> None:
    """Ask once before a rule and its point log are removed."""
    st.warning(t("sched_delete_confirm", lang=lang, title=rule.title))
    cols = st.columns(2)
    with cols[0]:
        if st.button(t("btn_yes_delete", lang=lang), key=f"sched_del_yes_{rule.id}",
                     type="primary", use_container_width=True):
            try:
                store.delete_schedule(rule.id)
            except Exception as exc:
                st.session_state.pop(f"sched_confirm_{rule.id}", None)
                flash_error(t("sched_delete_failed", lang=lang, error=str(exc)))
                st.rerun()
            st.session_state.pop(f"sched_confirm_{rule.id}", None)
            flash_success(t("sched_deleted", lang=lang, title=rule.title))
            st.rerun()
    with cols[1]:
        if st.button(t("btn_cancel", lang=lang), key=f"sched_del_no_{rule.id}",
                     use_container_width=True):
            st.session_state.pop(f"sched_confirm_{rule.id}", None)
            st.rerun()


def _toggle_state(rule: Schedule, lang: str) -> None:
    """Pause an active rule, or bring a held one back into service."""
    try:
        if rule.status in (STATUS_PAUSED, STATUS_ERROR_HOLD, STATUS_FINISHED):
            target = STATUS_ACTIVE
            upcoming = rule.next_run_at
            if not upcoming or upcoming < now_epoch():
                from core.schedules.calc import next_run_epoch
                upcoming = next_run_epoch(rule, now_epoch())
            store.set_next_run(rule.id, upcoming, status=target)
            store.settle_schedule(rule.id, status=target, last_status=rule.last_status,
                                  error_code="", success=rule.consecutive_failures == 0,
                                  run_count=rule.run_count, fail_count=rule.fail_count,
                                  consecutive_failures=0)
            flash_success(t("sched_resumed", lang=lang, title=rule.title))
        else:
            store.settle_schedule(rule.id, status=STATUS_PAUSED, last_status=rule.last_status,
                                  error_code=rule.last_error_code, success=False,
                                  run_count=rule.run_count, fail_count=rule.fail_count,
                                  consecutive_failures=rule.consecutive_failures)
            flash_success(t("sched_paused", lang=lang, title=rule.title))
    except Exception as exc:
        flash_error(t("sched_toggle_failed", lang=lang, error=str(exc)))
        st.rerun()
    daemon.wake_scheduler()
    st.rerun()


def _run_now(rule: Schedule, lang: str) -> None:
    """Start one run of the rule right away."""
    if store.active_run(rule.id) is not None:
        flash_warning(t("sched_already_running", lang=lang, title=rule.title))
        st.rerun()
    started = daemon.run_now(rule)
    if started is None:
        flash_error(t("sched_run_failed", lang=lang, title=rule.title))
    else:
        flash_success(t("sched_run_started", lang=lang, title=rule.title))
    st.rerun()


# ── Run history ─────────────────────────────────────────────────────────────


def _render_history(lang: str, rule: Schedule) -> None:
    """Show the recent points of one rule and link each to its dialog."""
    try:
        rows = store.list_runs(rule.id, limit=10)
    except Exception:
        rows = []
    if not rows:
        return
    counts = store.count_runs(rule.id)
    summary = ", ".join("%s: %d" % (key, value) for key, value in sorted(counts.items()))
    with st.expander(t("sched_history", lang=lang, summary=summary), expanded=False):
        for row in rows:
            _render_run_row(lang, rule, row)


def _render_run_row(lang: str, rule: Schedule, row: dict) -> None:
    """Render one point: when it was planned, how it ended, where to read it."""
    status = str(row.get("status") or "")
    when = form.format_moment(row.get("planned_at"), rule.tz)
    reason = str(row.get("reason") or row.get("error_code") or "")
    line = t("sched_run_row", lang=lang, when=when, status=status)
    if reason:
        line += " · " + reason
    cols = st.columns([4, 1, 1])
    with cols[0]:
        st.markdown(line)
    with cols[1]:
        thread_id = str(row.get("thread_id") or "")
        if thread_id and st.button(t("sched_open_dialog", lang=lang),
                                   key=f"sched_open_{row.get('id')}",
                                   use_container_width=True):
            _open_dialog(rule, thread_id)
    with cols[2]:
        if status in _LIVE_STATUSES:
            st.caption("⏳")


def _open_dialog(rule: Schedule, thread_id: str) -> None:
    """Open the dialog a run produced, exactly like the History page does."""
    import ui.pages.orchestrator as _orch_page

    slug = rule.agent_slug or DEVAGENT_SLUG
    _orch_page._init_orch_state(slug)
    _orch_page._load_thread(slug, thread_id)
    st.session_state["devagent_thread_id"] = thread_id
    st.session_state["devagent_history"] = _orch_page._ss(slug, "history") or []
    st.session_state["last_active_entity_type"] = "orchestrator"
    st.session_state["last_active_entity_id"] = slug
    st.session_state["current_page"] = f"orchestrator:{slug}"
    st.rerun()


# ── Form ────────────────────────────────────────────────────────────────────


def _render_form(lang: str, existing_id: str) -> None:
    """Render the create/edit form for one rule."""
    existing = store.get_schedule(existing_id) if existing_id else None
    if existing_id and existing is None:
        st.error(t("sched_not_found", lang=lang))
        if st.button("← " + t("sched_back", lang=lang), key="sched_back_missing"):
            _close_form()
        return

    title = t("sched_form_edit", lang=lang) if existing else t("sched_form_new", lang=lang)
    st.subheader(title)
    if st.button("← " + t("sched_back", lang=lang), key="sched_back"):
        _close_form()

    base = existing or Schedule(id=new_schedule_id(), agent_slug=DEVAGENT_SLUG)
    employees = _employee_options()
    with st.form("sched_form", clear_on_submit=False):
        draft = _render_form_fields(lang, base, employees)
        submitted = st.form_submit_button(
            t("sched_save", lang=lang), type="primary", use_container_width=True)
    if submitted:
        _save_draft(lang, draft, existing)


def _close_form() -> None:
    """Return from the form to the list."""
    st.session_state["sched_edit_id"] = None
    st.session_state["sched_form_open"] = False
    st.rerun()


def _employee_options() -> dict:
    """Return ``{slug: name}`` of every employee an owner may schedule."""
    options = {}
    for item in list_orchestrators():
        slug = str(item.get("slug") or "")
        if slug:
            options[slug] = str(item.get("name") or slug)
    return options or {DEVAGENT_SLUG: DEVAGENT_SLUG}


def _render_form_fields(lang: str, base: Schedule, employees: dict) -> Schedule:
    """Render every field of the form and return the draft rule."""
    title = st.text_input(t("sched_field_title", lang=lang), value=base.title,
                          key="sched_f_title", max_chars=120,
                          help=t("sched_field_title_help", lang=lang))
    slugs = list(employees.keys())
    slug_index = slugs.index(base.agent_slug) if base.agent_slug in slugs else 0
    employee = st.selectbox(
        t("sched_field_employee", lang=lang), options=slugs, index=slug_index,
        format_func=lambda slug: employees.get(slug, slug), key="sched_f_employee",
        help=t("sched_field_employee_help", lang=lang))
    task = st.text_area(t("sched_field_task", lang=lang), value=base.task_text,
                        key="sched_f_task", height=140,
                        help=t("sched_field_task_help", lang=lang))
    brief = st.text_area(t("sched_field_brief", lang=lang), value=base.brief_text,
                         key="sched_f_brief", height=90,
                         help=t("sched_field_brief_help", lang=lang))

    tz_index = (form.COMMON_ZONES.index(base.tz)
                if base.tz in form.COMMON_ZONES else 0)
    tz_name = st.selectbox(t("sched_field_tz", lang=lang), options=list(form.COMMON_ZONES),
                           index=tz_index, key="sched_f_tz",
                           help=t("sched_field_tz_help", lang=lang))

    default_day, default_clock = form.local_inputs(base.first_run_at, tz_name)
    cols = st.columns([2, 1, 1])
    with cols[0]:
        first_day = st.date_input(t("sched_field_first_date", lang=lang),
                                  value=default_day, key="sched_f_first_day")
    with cols[1]:
        first_clock = st.text_input(t("sched_field_first_time", lang=lang),
                                    value=default_clock, key="sched_f_first_time",
                                    help=t("sched_field_first_time_help", lang=lang))
    with cols[2]:
        unit_index = (INTERVAL_UNITS.index(base.interval_unit)
                      if base.interval_unit in INTERVAL_UNITS else 2)
        unit = st.selectbox(t("sched_field_unit", lang=lang), options=list(INTERVAL_UNITS),
                            index=unit_index, key="sched_f_unit",
                            format_func=lambda item: _label(_UNIT_KEYS, item, lang))
    interval = st.number_input(t("sched_field_interval", lang=lang), min_value=1,
                               max_value=100000, value=int(base.interval_value or 1),
                               step=1, key="sched_f_interval",
                               help=t("sched_field_interval_help", lang=lang))

    weekday_preset = form.weekday_preset_of(base.blocked_weekdays)
    preset_ids = [item[0] for item in form.WEEKDAY_PRESETS] + ["custom"]
    preset_index = preset_ids.index(weekday_preset)
    preset = st.selectbox(
        t("sched_field_days", lang=lang), options=preset_ids, index=preset_index,
        format_func=lambda item: _label(_DAYS_KEYS, item, lang),
        key="sched_f_days", help=t("sched_field_days_help", lang=lang))
    if preset == "custom":
        options = list(range(1, 8))
        defaults = list(base.blocked_weekdays)
        blocked = st.multiselect(
            t("sched_field_blocked", lang=lang), options=options, default=defaults,
            format_func=lambda day: _label(_WEEKDAY_KEYS, day, lang),
            key="sched_f_blocked", help=t("sched_field_blocked_help", lang=lang))
        blocked_days = tuple(sorted(int(day) for day in blocked))
    else:
        blocked_days = dict(form.WEEKDAY_PRESETS).get(preset, ())

    quiet = st.text_input(t("sched_field_quiet", lang=lang), value=base.quiet_hours,
                          key="sched_f_quiet", placeholder="22:00-07:00",
                          help=t("sched_field_quiet_help", lang=lang))

    with st.expander(t("sched_advanced", lang=lang), expanded=False):
        draft_advanced = _render_advanced_fields(lang, base)

    draft = base.clone(
        title=title, agent_slug=employee, task_text=task, brief_text=brief,
        workspace=st.session_state.get("sched_f_workspace", base.workspace),
        tz=tz_name, interval_value=int(interval), interval_unit=unit,
        blocked_weekdays=blocked_days,
        quiet_hours=format_quiet_hours(quiet),
        **draft_advanced,
    )
    first_at = form.epoch_from_local(first_day, first_clock, tz_name)
    if first_at:
        draft = draft.clone(first_run_at=first_at)
    _render_preview(lang, draft)
    return draft


def _render_advanced_fields(lang: str, base: Schedule) -> dict:
    """Render the policy and limit fields inside the advanced block."""
    cols = st.columns(2)
    with cols[0]:
        exception = st.selectbox(
            t("sched_field_exception", lang=lang), options=list(EXCEPTION_POLICIES),
            index=list(EXCEPTION_POLICIES).index(base.exception_policy)
            if base.exception_policy in EXCEPTION_POLICIES else 0,
            format_func=lambda item: _label(_EXCEPTION_KEYS, item, lang),
            key="sched_f_exception", help=t("sched_field_exception_help", lang=lang))
        overlap = st.selectbox(
            t("sched_field_overlap", lang=lang), options=list(OVERLAP_POLICIES),
            index=list(OVERLAP_POLICIES).index(base.overlap_policy)
            if base.overlap_policy in OVERLAP_POLICIES else 0,
            format_func=lambda item: _label(_OVERLAP_KEYS, item, lang),
            key="sched_f_overlap", help=t("sched_field_overlap_help", lang=lang))
    with cols[1]:
        misfire = st.selectbox(
            t("sched_field_misfire", lang=lang), options=list(MISFIRE_POLICIES),
            index=list(MISFIRE_POLICIES).index(base.misfire_policy)
            if base.misfire_policy in MISFIRE_POLICIES else 0,
            format_func=lambda item: _label(_MISFIRE_KEYS, item, lang),
            key="sched_f_misfire", help=t("sched_field_misfire_help", lang=lang))
        grace_minutes = st.number_input(
            t("sched_field_grace", lang=lang), min_value=1, max_value=1440,
            value=max(1, int(base.misfire_grace_seconds or 3600) // 60), step=1,
            key="sched_f_grace", help=t("sched_field_grace_help", lang=lang))

    cols = st.columns(3)
    with cols[0]:
        max_steps = st.number_input(t("sched_field_steps", lang=lang), min_value=1,
                                    max_value=10000, value=int(base.max_steps or 60),
                                    step=1, key="sched_f_steps",
                                    help=t("sched_field_steps_help", lang=lang))
    with cols[1]:
        timeout = st.number_input(t("sched_field_timeout", lang=lang), min_value=30,
                                  max_value=3600, value=int(base.timeout_seconds or 900),
                                  step=30, key="sched_f_timeout",
                                  help=t("sched_field_timeout_help", lang=lang))
    with cols[2]:
        budget = st.number_input(t("sched_field_budget", lang=lang), min_value=0,
                                 max_value=10_000_000, value=int(base.budget_tokens or 0),
                                 step=1000, key="sched_f_budget",
                                 help=t("sched_field_budget_help", lang=lang))
    return {
        "exception_policy": exception,
        "overlap_policy": overlap,
        "misfire_policy": misfire,
        "misfire_grace_seconds": int(grace_minutes) * 60,
        "max_steps": int(max_steps),
        "timeout_seconds": int(timeout),
        "budget_tokens": int(budget),
    }


def _render_preview(lang: str, draft: Schedule) -> None:
    """Show the next firing moments the draft would actually take."""
    if not draft.first_run_at:
        return
    points = form.next_points(draft, count=5)
    if not points:
        st.warning(t("sched_preview_empty", lang=lang))
        return
    rendered = ", ".join(form.format_moment(item, draft.tz) for item in points)
    st.caption(t("sched_preview", lang=lang, points=rendered))


def _save_draft(lang: str, draft: Schedule, existing) -> None:
    """Validate and persist the form draft."""
    errors = draft.validate(now=now_epoch(), creating=existing is None)
    if errors:
        for item in errors:
            st.error(_label(_ERR_KEYS, item["code"], lang))
        return
    try:
        if existing is None:
            store.create_schedule(draft)
            message = t("sched_created", lang=lang, title=draft.title)
        else:
            store.update_schedule(draft)
            message = t("sched_updated", lang=lang, title=draft.title)
    except Exception as exc:
        st.error(t("sched_save_failed", lang=lang, error=str(exc)))
        return
    st.session_state["sched_edit_id"] = None
    st.session_state["sched_form_open"] = False
    flash_success(message)
    daemon.wake_scheduler()
    st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
