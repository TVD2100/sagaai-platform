# -*- coding: utf-8 -*-
"""Render tests for ui/pages/stats.py using the Streamlit mock.

These tests are PRIMARILY concerned with the UI layer:
  - the page renders without errors in empty and non-empty states;
  - widget keys are unique;
  - granularity and provider-filter paths work.

Isolation:
  - the ``monkeypatch`` fixture restores ``core.statistics`` attributes after
    every test, so no injected records leak into the shared regression run;
  - ``_fresh_page_stats`` re-imports ``ui.pages.stats`` inside the active
    mock, so the module-level ``st`` binding never points at a mock instance
    owned by another test (or by tests/smoke/test_app_smoke.py).
"""
import sys
from datetime import datetime

from tests._st_mock import install_streamlit_mock


def _make_records():
    return [
        {"ts": datetime(2026, 8, 10, 12), "in": 100, "out": 20, "cache": 80,
         "provider": "DeepSeek", "source": "chat"},
        {"ts": datetime(2026, 8, 11, 12), "in": 50, "out": 5, "cache": 0,
         "provider": "YandexAI", "source": "chat"},
        {"ts": datetime(2026, 8, 12, 12), "in": 30, "out": 10, "cache": 30,
         "provider": "DeepSeek", "source": "orchestrator"},
    ]


def _inject_records(monkeypatch, records):
    import core.statistics as statistics
    monkeypatch.setattr(statistics, "collect_usage", lambda **kwargs: list(records))
    if records:
        monkeypatch.setattr(statistics, "available_bounds", lambda: (
            min(r["ts"] for r in records),
            max(r["ts"] for r in records),
        ))
    else:
        monkeypatch.setattr(statistics, "available_bounds", lambda: (None, None))


def _fresh_page_stats():
    sys.modules.pop("ui.pages.stats", None)
    from ui.pages.stats import page_stats
    return page_stats


def test_stats_page_renders_empty_state(monkeypatch):
    _inject_records(monkeypatch, [])
    with install_streamlit_mock() as st:
        st.session_state["ui_lang"] = "English"
        _fresh_page_stats()()
        assert any(call[0] == "info" for call in st.calls)
        # The empty state must not produce numeric metric rows.
        assert all(call[0] != "metric" for call in st.calls)


def test_stats_page_renders_data(monkeypatch):
    _inject_records(monkeypatch, _make_records())
    with install_streamlit_mock() as st:
        st.session_state["ui_lang"] = "English"
        _fresh_page_stats()()
        assert len([c for c in st.calls if c[0] == "date_input"]) == 2
        assert len([c for c in st.calls if c[0] == "selectbox"]) == 1
        assert len([c for c in st.calls if c[0] == "multiselect"]) == 1
        dataframe_calls = [c for c in st.calls if c[0] == "dataframe"]
        assert len(dataframe_calls) == 2  # bucket table + provider table
        bar_calls = [c for c in st.calls if c[0] == "bar_chart"]
        assert len(bar_calls) == 1


def test_stats_page_selectbox_defaults_to_day(monkeypatch):
    _inject_records(monkeypatch, _make_records())
    with install_streamlit_mock() as st:
        st.session_state["ui_lang"] = "English"
        _fresh_page_stats()()
        selectboxes = [c for c in st.calls if c[0] == "selectbox"]
        assert selectboxes  # granularity selector present
        opts = selectboxes[0][2].get("options", [])
        assert list(opts) == ["day", "week", "month", "period"]


def test_stats_page_key_uniqueness(monkeypatch):
    _inject_records(monkeypatch, _make_records())
    with install_streamlit_mock() as st:
        st.session_state["ui_lang"] = "English"
        _fresh_page_stats()()
    seen = []
    for name, args, kwargs in st.calls:
        key = kwargs.get("key")
        if key is not None:
            assert key not in seen, f"duplicate widget key: {key}"
            seen.append(key)


def test_stats_period_granularity_hides_buckets(monkeypatch):
    _inject_records(monkeypatch, _make_records())
    with install_streamlit_mock() as st:
        st.session_state["ui_lang"] = "English"
        st._selectbox_returns["stats_granularity"] = "period"
        _fresh_page_stats()()
        bar_calls = [c for c in st.calls if c[0] == "bar_chart"]
        assert len(bar_calls) == 0
        dataframe_calls = [c for c in st.calls if c[0] == "dataframe"]
        assert len(dataframe_calls) == 1  # only the provider table remains
