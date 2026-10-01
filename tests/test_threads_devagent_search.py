# -*- coding: utf-8 -*-
"""Unit tests for the thread-search service layer in core.threads_devagent.

The layer powers three orchestrator tools (search_in_threads, list_threads,
read_thread):
- search_thread_messages: explicit thread_ids (current-thread flow with no
  prior listing), orchestrator slugs + inclusive date range, case-insensitive
  Cyrillic matching, regex mode, role filter, hidden service messages,
  access control via allowed_slugs;
- list_threads_filtered: pagination (limit/offset/truncated), message
  counters, date range, sort order, access control;
- read_thread_window: absolute message indices, offset/limit window,
  remaining/has_more navigation, hidden-message filtering, access control.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture(autouse=True)
def isolated_data_dir():
    """Fresh SAGAAI_DATA_DIR per test with reset engine caches."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_tsearch_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    reset_engine()
    reset_devagent_engine()
    yield tmp
    reset_engine()
    reset_devagent_engine()

    if old_data_dir:
        os.environ["SAGAAI_DATA_DIR"] = old_data_dir
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


ALPHA = "orchestrator_alpha"
BETA = "orchestrator_beta"

TOOL_RESULT_CONTENT = '{"tool_result": {"ok": true, "tool": "run_code"}}'
AUTO_CONTINUE_CONTENT = "AUTO_CONTINUE: Continue to the next step."


def _make_thread(tid, slug, name, title=""):
    """Create a devagent thread row via the storage layer."""
    from storage.repository_devagent import repo_devagent_create_thread
    ok = repo_devagent_create_thread(tid, title=title,
                                     orchestrator_slug=slug,
                                     orchestrator_name=name)
    assert ok is True
    return tid


def _add_message(tid, role, content):
    """Append one stored message (content stored verbatim)."""
    from storage.repository_devagent import repo_devagent_append_message
    assert repo_devagent_append_message(tid, role, content) is True


def _set_updated(tid, ts):
    """Pin the thread's updated_at (append_message bumps it to now)."""
    from storage.repository_devagent import repo_devagent_save_thread_meta
    assert repo_devagent_save_thread_meta(tid, {"updated_at": ts}) is True


# ─── search_thread_messages ───────────────────────────────────────────────────


def test_search_by_explicit_thread_id_without_lists():
    """Searching an explicit thread_id works with no prior listing calls."""
    _make_thread("t_current", ALPHA, "Alpha", title="Текущий диалог")
    _add_message("t_current", "user", "Обсудим настройки проекта")
    _add_message("t_current", "assistant", "Какие именно настройки?")
    from core.threads_devagent import search_thread_messages

    res = search_thread_messages("настройки", thread_ids=["t_current"])

    assert res["ok"] is True
    assert res["scope"] == "thread"
    assert res["count"] == 2
    assert {h["message_index"] for h in res["hits"]} == {0, 1}
    hit = res["hits"][0]
    assert hit["thread_id"] == "t_current"
    assert hit["title"] == "Текущий диалог"
    assert hit["orchestrator_slug"] == ALPHA
    assert hit["match_count"] == 1
    assert "настройки" in hit["snippet"]


def test_search_scope_orchestrator_and_date_range():
    """Slug scope and inclusive date range narrow the scanned dialogs."""
    _make_thread("t_a", ALPHA, "Alpha")
    _add_message("t_a", "user", "проект дельта готов")
    _set_updated("t_a", "2026-01-10T12:00:00")
    _make_thread("t_b", BETA, "Beta")
    _add_message("t_b", "user", "проект дельта отложен")
    _set_updated("t_b", "2026-02-15T09:30:00")
    from core.threads_devagent import search_thread_messages

    by_slug = search_thread_messages("дельта", slugs=[ALPHA])
    assert by_slug["ok"] is True
    assert by_slug["scope"] == "orchestrator:" + ALPHA
    assert [h["thread_id"] for h in by_slug["hits"]] == ["t_a"]

    jan = search_thread_messages("дельта", date_from="2026-01-01", date_to="2026-01-31")
    assert [h["thread_id"] for h in jan["hits"]] == ["t_a"]

    feb = search_thread_messages("дельта", date_from="2026-02-01", date_to="2026-02-28")
    assert [h["thread_id"] for h in feb["hits"]] == ["t_b"]

    bad_field = search_thread_messages("дельта", date_field="bogus")
    assert bad_field["ok"] is False


def test_search_case_insensitive_cyrillic():
    """Cyrillic matching is case-insensitive in both directions."""
    _make_thread("t_c", ALPHA, "Alpha")
    _add_message("t_c", "user", "Проверка НАСТРОЕК соединения")
    from core.threads_devagent import search_thread_messages

    assert search_thread_messages("настроек", thread_ids=["t_c"])["count"] == 1
    assert search_thread_messages("НАСТРОЕК", thread_ids=["t_c"])["count"] == 1
    assert search_thread_messages("отсутствует", thread_ids=["t_c"])["count"] == 0


def test_search_regex_mode_and_invalid_pattern():
    """regex=True switches to pattern matching; a broken pattern is an error."""
    _make_thread("t_r", ALPHA, "Alpha")
    _add_message("t_r", "user", "версия 1.8.0 и версия 1.9.0")
    from core.threads_devagent import search_thread_messages

    res = search_thread_messages(r"версия\s+\d+\.\d+\.\d+", thread_ids=["t_r"], regex=True)
    assert res["ok"] is True
    assert res["count"] == 1
    assert res["hits"][0]["match_count"] == 2

    bad = search_thread_messages("([", thread_ids=["t_r"], regex=True)
    assert bad["ok"] is False
    assert "Invalid regular expression" in bad["error"]


def test_search_role_filter_and_hidden_messages():
    """Hidden service messages are skipped by default and filterable by role."""
    _make_thread("t_h", ALPHA, "Alpha")
    _add_message("t_h", "user", "первый вопрос про деплой")
    _add_message("t_h", "user", TOOL_RESULT_CONTENT)
    _add_message("t_h", "assistant", "деплой завершён")
    _add_message("t_h", "user", AUTO_CONTINUE_CONTENT)
    from core.threads_devagent import search_thread_messages

    visible = search_thread_messages("деплой", thread_ids=["t_h"])
    assert visible["count"] == 2
    assert {h["message_index"] for h in visible["hits"]} == {0, 2}

    hidden_step = search_thread_messages("step", thread_ids=["t_h"])
    assert hidden_step["count"] == 0
    with_hidden = search_thread_messages("step", thread_ids=["t_h"], include_tool_results=True)
    assert with_hidden["count"] == 1
    assert with_hidden["hits"][0]["message_index"] == 3

    users = search_thread_messages("деплой", thread_ids=["t_h"], role="user")
    assert [h["message_index"] for h in users["hits"]] == [0]

    bad_role = search_thread_messages("деплой", thread_ids=["t_h"], role="tool")
    assert bad_role["ok"] is False


def test_search_max_results_truncates():
    """max_results caps the hits and flags the truncation."""
    _make_thread("t_m", ALPHA, "Alpha")
    for i in range(5):
        _add_message("t_m", "user", f"строка {i} со словом маркер")
    from core.threads_devagent import search_thread_messages

    res = search_thread_messages("маркер", thread_ids=["t_m"], max_results=2)
    assert res["ok"] is True
    assert res["count"] == 2
    assert res["truncated"] is True


def test_search_access_control_denies_foreign_thread():
    """A thread of another orchestrator is denied, not silently searched."""
    _make_thread("t_own", ALPHA, "Alpha")
    _add_message("t_own", "user", "секрет альфы")
    _make_thread("t_foreign", BETA, "Beta")
    _add_message("t_foreign", "user", "секрет беты")
    from core.threads_devagent import search_thread_messages

    res = search_thread_messages("секрет", thread_ids=["t_foreign"], allowed_slugs=[ALPHA])
    assert res["ok"] is False
    assert res["denied_threads"] == ["t_foreign"]
    assert "Access denied" in res["error"]


def test_search_access_control_narrows_scope():
    """Without explicit ids the scan silently narrows to allowed orchestrators."""
    _make_thread("t_own2", ALPHA, "Alpha")
    _add_message("t_own2", "user", "слово маркер у альфы")
    _make_thread("t_f2", BETA, "Beta")
    _add_message("t_f2", "user", "слово маркер у беты")
    from core.threads_devagent import search_thread_messages

    res = search_thread_messages("маркер", allowed_slugs=[ALPHA])
    assert res["ok"] is True
    assert "access-limited" in res["scope"]
    assert [h["thread_id"] for h in res["hits"]] == ["t_own2"]

    denied = search_thread_messages("маркер", slugs=[BETA], allowed_slugs=[ALPHA])
    assert denied["ok"] is False


def test_search_missing_query_and_unknown_thread():
    """An empty query and an unknown thread id both fail cleanly."""
    from core.threads_devagent import search_thread_messages

    assert search_thread_messages("   ")["ok"] is False
    res = search_thread_messages("x", thread_ids=["no_such_thread"])
    assert res["ok"] is False
    assert "not found" in res["error"].lower()


# ─── list_threads_filtered ────────────────────────────────────────────────────


def test_list_threads_pagination_counts_and_order():
    """Pagination, precise truncated flag, counters and order work together."""
    for i, tid in enumerate(["t1", "t2", "t3"]):
        _make_thread(tid, ALPHA, "Alpha", title=f"Диалог {i}")
        _add_message(tid, "user", f"сообщение {i}")
        if tid == "t2":
            _add_message(tid, "assistant", "ответ")
        _set_updated(tid, f"2026-03-0{i + 1}T10:00:00")
    from core.threads_devagent import list_threads_filtered

    page1 = list_threads_filtered(limit=2)
    assert page1["ok"] is True
    assert [t["thread_id"] for t in page1["threads"]] == ["t3", "t2"]
    assert page1["count"] == 2
    assert page1["truncated"] is True
    counts = {t["thread_id"]: t.get("message_count") for t in page1["threads"]}
    assert counts["t3"] == 1 and counts["t2"] == 2

    page2 = list_threads_filtered(limit=2, offset=2)
    assert [t["thread_id"] for t in page2["threads"]] == ["t1"]
    assert page2["truncated"] is False

    ascending = list_threads_filtered(limit=3, order="asc")
    assert [t["thread_id"] for t in ascending["threads"]] == ["t1", "t2", "t3"]

    no_counts = list_threads_filtered(limit=1, with_counts=False)
    assert "message_count" not in no_counts["threads"][0]


def test_list_threads_date_range_and_validation():
    """The date range is inclusive; bad order/date_field values are errors."""
    _make_thread("t_d1", ALPHA, "Alpha")
    _add_message("t_d1", "user", "один")
    _set_updated("t_d1", "2026-03-01T10:00:00")
    _make_thread("t_d2", ALPHA, "Alpha")
    _add_message("t_d2", "user", "два")
    _set_updated("t_d2", "2026-03-02T10:00:00")
    from core.threads_devagent import list_threads_filtered

    rng = list_threads_filtered(date_from="2026-03-02", date_to="2026-03-02")
    assert [t["thread_id"] for t in rng["threads"]] == ["t_d2"]

    assert list_threads_filtered(order="sideways")["ok"] is False
    assert list_threads_filtered(date_field="bogus")["ok"] is False


# ─── read_thread_window ───────────────────────────────────────────────────────


def test_read_thread_window_indices_and_remaining():
    """A window returns absolute message indices and navigation counters."""
    _make_thread("t_read", ALPHA, "Alpha", title="Читаемый")
    for i in range(5):
        _add_message("t_read", "user" if i % 2 == 0 else "assistant", f"сообщение номер {i}")
    from core.threads_devagent import read_thread_window

    win = read_thread_window("t_read", offset=1, limit=2)
    assert win["ok"] is True
    assert win["title"] == "Читаемый"
    assert win["total"] == 5
    assert [m["index"] for m in win["messages"]] == [1, 2]
    assert win["messages"][0]["content"] == "сообщение номер 1"
    assert win["remaining"] == 2
    assert win["has_more"] is True

    tail = read_thread_window("t_read", offset=4, limit=10)
    assert [m["index"] for m in tail["messages"]] == [4]
    assert tail["remaining"] == 0
    assert tail["has_more"] is False


def test_read_thread_window_skips_hidden_and_reports_totals():
    """Hidden service messages are skipped but indices stay absolute."""
    _make_thread("t_read2", ALPHA, "Alpha")
    _add_message("t_read2", "user", "видимое")
    _add_message("t_read2", "user", TOOL_RESULT_CONTENT)
    _add_message("t_read2", "assistant", "тоже видимое")
    from core.threads_devagent import read_thread_window

    win = read_thread_window("t_read2")
    assert [m["index"] for m in win["messages"]] == [0, 2]
    assert win["total"] == 3
    assert win["count"] == 2
    assert win["remaining"] == 0

    full = read_thread_window("t_read2", include_tool_results=True)
    assert [m["index"] for m in full["messages"]] == [0, 1, 2]


def test_read_thread_truncates_long_messages(monkeypatch):
    """Per-message content is capped with an explicit truncation marker."""
    _make_thread("t_long", ALPHA, "Alpha")
    _add_message("t_long", "user", "х" * 50)
    import core.threads_devagent as td
    monkeypatch.setattr(td, "MAX_MESSAGE_CHARS", 10)

    win = td.read_thread_window("t_long")
    content = win["messages"][0]["content"]
    assert content.startswith("х" * 10)
    assert "truncated" in content
    assert "50" in content


def test_read_thread_access_and_errors():
    """Foreign dialogs are denied; unknown/empty ids are clean errors."""
    _make_thread("t_own3", ALPHA, "Alpha")
    _add_message("t_own3", "user", "привет")
    from core.threads_devagent import read_thread_window

    assert read_thread_window("t_own3", allowed_slugs=[BETA])["ok"] is False
    denied = read_thread_window("t_own3", allowed_slugs=[BETA])
    assert "Access denied" in denied["error"]

    missing = read_thread_window("no_such")
    assert missing["ok"] is False
    assert "not found" in missing["error"].lower()

    assert read_thread_window("")["ok"] is False
