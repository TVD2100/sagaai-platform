# -*- coding: utf-8 -*-
"""
ui/pages/stats.py - token usage statistics page.

Shows aggregate token usage for a selected period, sourced from BOTH
thread databases (regular assistant chats and DevAgent / employee threads).
Aggregation is done by pure helpers in core.statistics; this module only
renders the UI:

  1. period selection (from / to dates);
  2. granularity selector (day / week / month / whole period);
  3. summary metric row (total / input / output / cache% / messages);
  4. per-bucket breakdown chart + table (hidden for "whole period");
  5. per-provider filter + table (provider attribution from Assistant.service
     for chats and the orchestrator's strong_service for DevAgent threads).
"""
import datetime as _dt

import streamlit as st

from core.i18n import t
from core import statistics


def page_stats() -> None:
    """Render the Statistics page (dispatched from ui.app)."""
    lang = st.session_state.get("ui_lang", "en")
    st.title(t("page_stats_title", lang=lang))
    st.markdown(t("stats_hint", lang=lang))

    # Default period: from the first to the last stored message; when there
    # is no data yet, the last 30 days.
    lo, hi = statistics.available_bounds()
    today = _dt.date.today()
    default_start = (lo.date() if lo else None) or (today - _dt.timedelta(days=30))
    default_end = (hi.date() if hi else None) or today

    col_from, col_to = st.columns(2)
    with col_from:
        start_date = st.date_input(
            t("stats_period_from", lang=lang),
            value=default_start,
            key="stats_date_from",
        )
    with col_to:
        end_date = st.date_input(
            t("stats_period_to", lang=lang),
            value=default_end,
            key="stats_date_to",
        )
    start_date = start_date or default_start
    end_date = end_date or default_end
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    gran_options = list(statistics.GRANULARITIES)
    gran_labels = {g: t(f"stats_gran_{g}", lang=lang) for g in gran_options}
    granularity = st.selectbox(
        t("stats_granularity", lang=lang),
        options=gran_options,
        format_func=lambda g: gran_labels.get(g, g),
        key="stats_granularity",
    )
    if granularity not in gran_options:
        granularity = "day"

    start_dt = _dt.datetime.combine(start_date, _dt.time.min)
    end_dt = _dt.datetime.combine(end_date, _dt.time.max)

    records = statistics.collect_usage(start_dt=start_dt, end_dt=end_dt)
    summary = statistics.build_summary(records, granularity,
                                       start_dt=start_dt, end_dt=end_dt)
    if summary["total"]["messages"] == 0:
        st.info(t("stats_empty", lang=lang))
        return

    st.caption(
        t(
            "stats_period_caption",
            lang=lang,
            start=str(start_date),
            end=str(end_date),
        )
    )

    # Per-provider filter (all providers pre-selected by default).
    view = summary
    if summary["provider_names"]:
        selected = st.multiselect(
            t("stats_filter_provider", lang=lang),
            options=summary["provider_names"],
            default=list(summary["provider_names"]),
            key="stats_provider_filter",
        )
        if not selected:
            st.info(t("stats_filter_empty", lang=lang))
            return
        if set(selected) != set(summary["provider_names"]):
            wanted = set(selected)
            filtered = [
                r for r in records
                if str(r.get("provider") or "unknown") in wanted
            ]
            view = statistics.build_summary(filtered, granularity,
                                            start_dt=start_dt, end_dt=end_dt)

    # Summary metric row.
    total = view["total"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric(t("stats_total_tokens", lang=lang), f"{total['total']:,}")
    m2.metric(t("stats_tokens_in", lang=lang), f"{total['in']:,}")
    m3.metric(t("stats_tokens_out", lang=lang), f"{total['out']:,}")
    m4.metric(t("stats_cache_pct", lang=lang), f"{total['cache_pct']}%")
    m5.metric(t("stats_messages", lang=lang), f"{total['messages']:,}")

    # Breakdown by time bucket (only for day/week/month granularities).
    if view["buckets"]:
        st.markdown("---")
        st.markdown(f"**{t('stats_breakdown_title', lang=lang)}**")
        st.bar_chart({b["label"]: b["total"] for b in view["buckets"]})
        bucket_rows = [
            {
                t("stats_col_period", lang=lang): b["label"],
                t("stats_col_total", lang=lang): b["total"],
                t("stats_col_in", lang=lang): b["in"],
                t("stats_col_out", lang=lang): b["out"],
                t("stats_col_cache_pct", lang=lang): f"{b['cache_pct']}%",
                t("stats_col_messages", lang=lang): b["count"],
            }
            for b in view["buckets"]
        ]
        st.dataframe(bucket_rows, use_container_width=True)

    # Per-provider table.
    st.markdown("---")
    st.markdown(f"**{t('stats_provider_title', lang=lang)}**")
    provider_rows = [
        {
            t("stats_col_provider", lang=lang): p["provider"],
            t("stats_col_total", lang=lang): p["total"],
            t("stats_col_in", lang=lang): p["in"],
            t("stats_col_out", lang=lang): p["out"],
            t("stats_col_cache_pct", lang=lang): f"{p['cache_pct']}%",
            t("stats_col_share", lang=lang): f"{p['share_pct']}%",
            t("stats_col_messages", lang=lang): p["messages"],
        }
        for p in view["providers"]
    ]
    st.dataframe(provider_rows, use_container_width=True)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
