# -*- coding: utf-8 -*-
"""tests/scenarios/test_stats_scenario.py - end-to-end scenarios for the
Statistics page.

User-level scenarios (given -> when -> then) walking the app through the
REAL public core/storage layers (no monkeypatched aggregation):

  Scenario 1 - новая статистика с реальными данными: сообщения в обычном чате
               и потоке DevAgent/сотрудника попадают в сводку с правильными
               итогами, разбивкой по дням и распределением по провайдерам.
  Scenario 2 - пустой период рендерит пустое состояние без метрик и без
               ошибок (виджетные ключи при этом уникальны).

Seeded usage uses the public persistence APIs of core.threads and
core.threads_devagent (assistant_id/service mapping and orchestrator
config.strong_service are the real sources of provider attribution), so
provider attribution is exercised end-to-end rather than mocked.
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock  # noqa: E402


@pytest.fixture()
def stats_data(isolated_app_modules, monkeypatch, tmp_path):
    """Seed token usage through the real persistence APIs into a throwaway
    DATA_DIR, yield the data dir, and clean up on exit."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))

    # Collectors import lazily; when the env var changes inside an already
    # imported interpreter, cached module instances would keep the old paths.
    from tests._test_isolation import drop_app_modules
    drop_app_modules()

    from core import assistants, orchestrators, statistics, threads
    from core import threads_devagent

    now = datetime.now().replace(microsecond=0)

    # ── assistant "summarizer" -> provider DeepSeek ───────────────────────
    assistant_id = assistants.create_assistant(
        name="stats scenario assistant", service="DeepSeek", model="m1",
        temperature=0.5, text="summarize the input", description="",
    )
    assert assistant_id, "assistant creation failed"

    chat_tid = threads.create_thread(assistant_id, "stats scenario assistant")
    threads.save_thread_messages(chat_tid, [
        {
            "role": "user",
            "content": "hello",
            "ts": (now - timedelta(days=30)).isoformat(),
        },
        {
            "role": "assistant",
            "content": "hi",
            "ts": (now - timedelta(days=30) + timedelta(minutes=5)).isoformat(),
            "_tokens": {"in": 100, "out": 31, "cache": 60},
        },
        {
            "role": "user",
            "content": "again",
            "ts": (now - timedelta(days=10)).isoformat(),
        },
        {
            "role": "assistant",
            "content": "again",
            "ts": (now - timedelta(days=10) + timedelta(minutes=5)).isoformat(),
            "_tokens": {"in": 300, "out": 40, "cache": 100},
        },
    ])

    # ── orchestrator "workman" -> provider YandexAI via config.strong_service
    orch_cfg = {
        "strong_service": "YandexAI",
        "weak_service": "OpenRouter",
        "search_service": "Tavily",
    }
    orch_id = orchestrators.create_orchestrator(
        slug="stats_scn_workman", name="stats scenario workman",
        description="", prompt_text="build it", config=orch_cfg,
    )
    assert orch_id, "orchestrator creation failed"

    orch_tid = threads_devagent.create_devagent_thread(
        title="build it", orchestrator_slug="stats_scn_workman",
        orchestrator_name="stats scenario workman",
    )
    threads_devagent.save_thread_messages(orch_tid, [
        {
            "role": "user",
            "content": "build",
            "ts": (now - timedelta(days=10)).isoformat(),
        },
        {
            "role": "assistant",
            "content": "{\"loop_status\": \"continue\"}",
            "ts": (now - timedelta(days=10) + timedelta(minutes=10)).isoformat(),
            "_tokens": {"in": 110, "out": 20, "cache": 80},
        },
    ])

    yield data_dir, statistics, chat_tid, orch_tid

    # Clean up the seeded DB state on fixture teardown (fresh app modules
    # per test are handled by tests/_test_isolation.py).


# ─── Scenario 1: seeded usage shows up with correct totals ──────────────────

def test_stats_page_aggregates_real_seeded_usage(stats_data):
    """
    Given regular chat messages (DeepSeek) and DevAgent thread messages
          (YandexAI) stored through the real persistence APIs,
    when the user opens the Statistics page with the all-time period and
          month granularity,
    then the summary shows the seeded totals (in/out/cache% correct), the
          page renders without errors, and provider attribution
          (DeepSeek / YandexAI) is exercised end-to-end.
    """
    data_dir, statistics, chat_tid, orch_tid = stats_data
    del data_dir, chat_tid, orch_tid  # seeded through public APIs; not needed later

    chat_records = statistics.collect_chat_records()
    orch_records = statistics.collect_orchestrator_records()

    # Regular chat: two assistant messages with tokens.
    chat_tokens = [(r["in"], r["out"], r["cache"]) for r in chat_records]
    assert sorted(chat_tokens) == sorted([(100, 31, 60), (300, 40, 100)]), \
        f"unexpected chat records: {chat_records}"
    assert {r["provider"] for r in chat_records} == {"DeepSeek"}, \
        f"chat provider attribution broken: {chat_records}"

    # DevAgent thread: one assistant message with tokens.
    assert [(r["in"], r["out"], r["cache"]) for r in orch_records] == \
        [(110, 20, 80)], f"unexpected orchestrator records: {orch_records}"
    assert {r["provider"] for r in orch_records} == {"YandexAI"}, \
        f"orchestrator provider attribution broken: {orch_records}"

    all_records = statistics.collect_usage()
    assert len(all_records) == 3, f"expected 3 token records, got {all_records}"

    summary = statistics.build_summary(all_records, "month")
    total = summary["total"]
    assert total["in"] == 510          # 100 + 300 + 110
    assert total["out"] == 91          # 31 + 40 + 20
    assert total["cache"] == 240       # 60 + 100 + 80
    assert total["total"] == 601       # in + out
    assert total["cache_pct"] == 47    # round(240*100/510) = 47
    assert total["messages"] == 3

    provider_names = summary["provider_names"]
    assert sorted(provider_names) == ["DeepSeek", "YandexAI"]
    by_name = {p["provider"]: p for p in summary["providers"]}
    assert by_name["DeepSeek"]["total"] == 471    # 400 + 71
    assert by_name["YandexAI"]["total"] == 130    # 110 + 20

    # Page render path - with month granularity both dataframes still appear.
    importlib.invalidate_caches()
    with install_streamlit_mock() as st:
        from ui.pages.stats import page_stats
        st.session_state["ui_lang"] = "English"
        # Cover the whole seeded window so the page always has data,
        # regardless of the current day of the month.
        lo, hi = statistics.available_bounds()
        st._date_returns["stats_date_from"] = lo.date()
        st._date_returns["stats_date_to"] = hi.date()
        st._selectbox_returns["stats_granularity"] = "month"
        page_stats()
        assert st.errors == [], f"stats page emitted errors: {st.errors}"
        # Metrics render inside st.columns(); the mock's column objects do not
        # record their child calls, so verify the rendered state through
        # st-level calls: caption, breakdown chart and both tables.
        assert any(call[0] == "caption" for call in st.calls)
        assert len([c for c in st.calls if c[0] == "bar_chart"]) == 1
        filter_calls = [c for c in st.calls
                        if c[0] == "multiselect"
                        and c[2].get("key") == "stats_provider_filter"]
        assert len(filter_calls) == 1
        options = list(filter_calls[0][2].get("options") or [])
        assert sorted(options) == ["DeepSeek", "YandexAI"]
        dataframe_calls = [c for c in st.calls if c[0] == "dataframe"]
        assert len(dataframe_calls) == 2  # bucket table + provider table


# ─── Scenario 2: empty database keeps the page calm and unique ──────────────

def test_stats_page_empty_database_renders_info_without_metrics(
        isolated_app_modules, monkeypatch, tmp_path):
    """
    Given a brand-new install with no stored messages,
    when the user opens the Statistics page,
    then the page renders the empty state via st.info, emits no metrics and
          no errors, and all widget keys are unique.
    """
    data_dir = tmp_path / "empty_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    from tests._test_isolation import drop_app_modules
    drop_app_modules()

    with install_streamlit_mock() as st:
        from ui.pages.stats import page_stats
        st.session_state["ui_lang"] = "English"
        page_stats()

        assert st.errors == [], f"stats page emitted errors: {st.errors}"
        assert any(call[0] == "info" for call in st.calls), \
            "empty state info message not rendered"
        assert all(call[0] != "metric" for call in st.calls), \
            "empty state should not render numeric metrics"

        seen_keys = []
        for name, args, kwargs in st.calls:
            key = kwargs.get("key")
            if key is not None:
                assert key not in seen_keys, f"duplicate widget key: {key}"
                seen_keys.append(key)
        assert seen_keys
