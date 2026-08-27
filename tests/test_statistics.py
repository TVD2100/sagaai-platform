# -*- coding: utf-8 -*-
"""Unit tests for core.statistics: pure aggregation helpers.

Database collection (collect_chat_records / collect_orchestrator_records)
is covered by the end-to-end scenario tests; the math below is exercised
with plain record dicts.
"""
from datetime import datetime, timedelta

from core import statistics


def _rec(day, hour=12, tin=100, out=20, cache=50, provider="DeepSeek"):
    return {
        "ts": datetime(2026, 8, day, hour),
        "in": tin,
        "out": out,
        "cache": cache,
        "provider": provider,
        "source": "chat",
    }


# ─── parse / coercion ───────────────────────────────────────────────────────

def test_parse_ts_accepts_stored_iso_format():
    dt = statistics._parse_ts("2026-08-23T09:30:49.100474")
    assert dt == datetime(2026, 8, 23, 9, 30, 49, 100474)


def test_parse_ts_rejects_garbage_and_empty():
    assert statistics._parse_ts(None) is None
    assert statistics._parse_ts("") is None
    assert statistics._parse_ts("not-a-date") is None


def test_int_coercion():
    assert statistics._int(0) == 0
    assert statistics._int("12") == 12
    assert statistics._int(None) == 0
    assert statistics._int("x") == 0
    assert statistics._int(-5) == 0


# ─── cache percentage ───────────────────────────────────────────────────────

def test_cache_pct_normal():
    assert statistics.cache_pct(830, 1000) == 83


def test_cache_pct_zero_and_clamping():
    assert statistics.cache_pct(0, 100) == 0
    assert statistics.cache_pct(50, 0) == 0
    assert statistics.cache_pct(1200, 1000) == 100


# ─── bucket keys ────────────────────────────────────────────────────────────

def test_bucket_keys():
    dt = datetime(2026, 8, 16, 22, 30)  # a Sunday
    assert statistics._bucket_key(dt, "day") == "2026-08-16"
    assert statistics._bucket_key(dt, "month") == "2026-08"
    assert statistics._bucket_key(dt, "period") == "period"


def test_week_bucket_uses_monday():
    # 2026-08-16 is a Sunday; its ISO week starts on Monday 2026-08-10.
    dt = datetime(2026, 8, 16)
    assert statistics._bucket_key(dt, "week") == "2026-08-10 (week)"
    # A Monday belongs to its own week.
    monday = datetime(2026, 8, 10)
    assert statistics._bucket_key(monday, "week") == "2026-08-10 (week)"


# ─── bucketing / rows ───────────────────────────────────────────────────────

def _sorted_keys(buckets):
    return list(buckets.keys())


def test_bucketize_groups_and_sorts():
    records = [
        _rec(16),
        _rec(10),
        _rec(16, hour=18),
        _rec(22),
    ]
    buckets = statistics._bucketize(records, "day")
    assert _sorted_keys(buckets) == ["2026-08-10", "2026-08-16", "2026-08-22"]
    assert len(buckets["2026-08-16"]) == 2


def test_bucket_rows_sums_tokens():
    records = [_rec(10, tin=100, out=20, cache=80), _rec(10, hour=18, tin=50, out=5, cache=0)]
    rows = statistics._bucket_rows(records, "day")
    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == "2026-08-10"
    assert row["in"] == 150
    assert row["out"] == 25
    assert row["cache"] == 80
    assert row["total"] == 175
    assert row["count"] == 2
    assert row["cache_pct"] == 53  # int(round(80*100/150))


# ─── build_summary ──────────────────────────────────────────────────────────

def test_build_summary_totals_and_sorting():
    records = [
        _rec(10, tin=100, out=20, cache=80, provider="DeepSeek"),
        _rec(11, tin=50, out=5, cache=0, provider="YandexAI"),
        _rec(12, tin=30, out=10, cache=30, provider="DeepSeek"),
    ]
    summary = statistics.build_summary(records, "day")
    total = summary["total"]
    assert total["in"] == 180
    assert total["out"] == 35
    assert total["cache"] == 110
    assert total["total"] == 215
    assert total["cache_pct"] == 61  # int(round(110*100/180))
    assert total["messages"] == 3

    # Providers sorted by total desc: DeepSeek (160) before YandexAI (55).
    providers = summary["providers"]
    assert [p["provider"] for p in providers] == ["DeepSeek", "YandexAI"]
    assert summary["provider_names"] == ["DeepSeek", "YandexAI"]
    deepseek = providers[0]
    assert deepseek["in"] == 130
    assert deepseek["out"] == 30
    assert deepseek["cache"] == 110
    assert deepseek["total"] == 160
    assert deepseek["share_pct"] == round(160 * 100.0 / 215, 1)
    assert deepseek["cache_pct"] == 85  # int(round(110*100/130))


def test_build_summary_period_has_no_buckets():
    records = [_rec(10), _rec(11), _rec(12)]
    summary = statistics.build_summary(records, "period")
    assert summary["buckets"] == []
    assert summary["total"]["total"] == 360


def test_build_summary_invalid_granularity_falls_back_to_day():
    records = [_rec(10)]
    summary = statistics.build_summary(records, "bogus")
    assert summary["granularity"] == "day"


def test_build_summary_empty_records():
    summary = statistics.build_summary([], "day")
    assert summary["total"]["total"] == 0
    assert summary["buckets"] == []
    assert summary["providers"] == []
    assert summary["provider_names"] == []


def test_build_summary_unknown_provider_grouping():
    records = [
        _rec(10, provider=""),
        _rec(11, provider=None),
    ]
    records[1]["provider"] = None
    summary = statistics.build_summary(records, "month")
    assert summary["provider_names"] == ["unknown"]
    assert summary["providers"][0]["messages"] == 2


# ─── collect_usage filtering ────────────────────────────────────────────────

def test_collect_usage_filters_period(monkeypatch):
    all_records = [_rec(10), _rec(12), _rec(14)]
    monkeypatch.setattr(statistics, "collect_chat_records", lambda: list(all_records))
    monkeypatch.setattr(statistics, "collect_orchestrator_records", lambda: [])

    start = datetime(2026, 8, 11)
    end = datetime(2026, 8, 13, 23, 59)
    records = statistics.collect_usage(start_dt=start, end_dt=end)
    assert [r["ts"].day for r in records] == [12]


def test_available_bounds(monkeypatch):
    monkeypatch.setattr(statistics, "collect_chat_records",
                        lambda: [_rec(10), _rec(14)])
    monkeypatch.setattr(statistics, "collect_orchestrator_records", lambda: [])
    lo, hi = statistics.available_bounds()
    assert lo == datetime(2026, 8, 10, 12)
    assert hi == datetime(2026, 8, 14, 12)


def test_available_bounds_empty(monkeypatch):
    monkeypatch.setattr(statistics, "collect_chat_records", lambda: [])
    monkeypatch.setattr(statistics, "collect_orchestrator_records", lambda: [])
    assert statistics.available_bounds() == (None, None)
