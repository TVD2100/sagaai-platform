# -*- coding: utf-8 -*-
"""Scenario tests for the thread-search tools (search_in_threads,
list_threads, read_thread).

Each scenario walks the app like a user would - through the public
dispatcher (``UniversalDevAgent().dispatch``) - and follows the
given -> when -> then structure:

1. A user asks about something said earlier in the CURRENT dialog: a bare
   query searches the current thread with no listing calls at all.
2. The orchestrator already holds a thread_id: it searches that dialog
   directly, again without any list_threads call.
3. Cross-dialog discovery for one orchestrator within a date period.
4. Search -> read around the match: the hit's message_index feeds
   read_thread(offset=...) to view the surrounding context.
5. Access control: a non-DevAgent orchestrator sees only its own dialogs;
   foreign thread ids are denied on both search and read.
6. list_threads discovers dialog ids and paginates with a precise
   truncated flag and per-dialog message counters.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture(autouse=True)
def isolated_data_dir():
    """Fresh SAGAAI_DATA_DIR per test with reset engine caches."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_tsrch_scen_")
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
CURRENT_TID = "th_scenario_current"


def _agent():
    from dev_agent.universal_agent import UniversalDevAgent
    return UniversalDevAgent()


def _make_thread(tid, slug, name, title=""):
    from storage.repository_devagent import repo_devagent_create_thread
    assert repo_devagent_create_thread(tid, title=title,
                                       orchestrator_slug=slug,
                                       orchestrator_name=name) is True
    return tid


def _add_message(tid, role, content):
    from storage.repository_devagent import repo_devagent_append_message
    assert repo_devagent_append_message(tid, role, content) is True


def _set_updated(tid, ts):
    """Pin the thread's updated_at (append_message bumps it to now)."""
    from storage.repository_devagent import repo_devagent_save_thread_meta
    assert repo_devagent_save_thread_meta(tid, {"updated_at": ts}) is True


@pytest.fixture
def active_thread(monkeypatch):
    """Publish CURRENT_TID as the active dialog thread."""
    from dev_agent import config as dagent_config
    monkeypatch.setattr(dagent_config, "ACTIVE_THREAD_ID", CURRENT_TID)
    return CURRENT_TID


# ─── Scenario 1: search the current dialog, no lists ────────────────────────


def test_scenario_1_search_current_dialog_no_lists(active_thread):
    """Given a user discussing a budget in the current dialog,
    when  they ask what was said about the budget,
    then  the search targets the current dialog only - no listing calls -
          and a missing word yields an empty successful result.
    """
    _make_thread(CURRENT_TID, ALPHA, "Alpha", title="Бюджетный диалог")
    _add_message(CURRENT_TID, "user", "Согласуем бюджет на квартал: 120 тысяч")
    _add_message(CURRENT_TID, "assistant", "Записал бюджет 120 тысяч на квартал")
    _make_thread("th_scenario_other", BETA, "Beta", title="Чужой диалог")
    _add_message("th_scenario_other", "user", "Другой бюджет: 90 тысяч")
    agent = _agent()

    res = agent.dispatch("search_in_threads", {"query": "бюджет"})

    assert res["ok"] is True
    assert res["scope"] == "thread"
    assert res["active_thread_id"] == CURRENT_TID
    assert {h["thread_id"] for h in res["hits"]} == {CURRENT_TID}
    assert all("бюджет" in h["snippet"].lower() for h in res["hits"])

    empty = agent.dispatch("search_in_threads", {"query": "вертолёт"})
    assert empty["ok"] is True
    assert empty["count"] == 0


# ─── Scenario 2: explicit thread_id without any listing ─────────────────────


def test_scenario_2_search_explicit_thread_id_without_lists(active_thread):
    """Given the orchestrator holds the thread_id of a specific dialog,
    when  it searches that id directly,
    then  the search works with no prior list_threads call.
    """
    _make_thread(CURRENT_TID, ALPHA, "Alpha")
    _add_message(CURRENT_TID, "user", "В текущем диалоге про смету молчали")
    _make_thread("th_scenario_target", ALPHA, "Alpha", title="Диалог со сметой")
    _add_message("th_scenario_target", "user", "Смета согласована на 300 тысяч")
    agent = _agent()

    res = agent.dispatch("search_in_threads", {
        "query": "смета", "thread_id": "th_scenario_target"})

    assert res["ok"] is True
    assert res["scope"] == "thread"
    assert [h["thread_id"] for h in res["hits"]] == ["th_scenario_target"]
    assert res["hits"][0]["title"] == "Диалог со сметой"


# ─── Scenario 3: orchestrator scope within a date period ────────────────────


def test_scenario_3_search_orchestrator_scope_with_period():
    """Given two dialogs of one orchestrator updated in different months,
    when  the orchestrator searches with a date period,
    then  only the dialog active inside the period contributes hits.
    """
    _make_thread("th_scenario_jan", ALPHA, "Alpha", title="Январь")
    _add_message("th_scenario_jan", "user", "План релиза на январь утверждён")
    _set_updated("th_scenario_jan", "2026-01-15T10:00:00")
    _make_thread("th_scenario_feb", ALPHA, "Alpha", title="Февраль")
    _add_message("th_scenario_feb", "user", "План релиза на февраль изменён")
    _set_updated("th_scenario_feb", "2026-02-20T10:00:00")
    agent = _agent()

    res = agent.dispatch("search_in_threads", {
        "query": "план релиза",
        "scope": "orchestrator",
        "orchestrator": ALPHA,
        "date_from": "2026-01-01",
        "date_to": "2026-01-31",
    })

    assert res["ok"] is True
    assert res["scope"] == "orchestrator:" + ALPHA
    assert [h["thread_id"] for h in res["hits"]] == ["th_scenario_jan"]


# ─── Scenario 4: search a match, then read around it ────────────────────────


def test_scenario_4_search_then_read_around_match(active_thread):
    """Given a long dialog with a keyword deep inside,
    when  the search finds its message_index,
    then  read_thread shows the surrounding context by that index.
    """
    _make_thread(CURRENT_TID, ALPHA, "Alpha", title="Длинный диалог")
    for i in range(7):
        _add_message(CURRENT_TID, "user" if i % 2 == 0 else "assistant",
                     f"шаг обсуждения номер {i}")
    _add_message(CURRENT_TID, "assistant", "ключевой момент здесь: меняем схему")
    _add_message(CURRENT_TID, "user", "принято")
    agent = _agent()

    found = agent.dispatch("search_in_threads", {"query": "ключевой момент"})
    assert found["ok"] is True
    hit = found["hits"][0]
    assert hit["message_index"] == 7

    window = agent.dispatch("read_thread", {
        "thread_id": hit["thread_id"],
        "offset": hit["message_index"] - 1,
        "limit": 3,
    })

    assert window["ok"] is True
    assert [m["index"] for m in window["messages"]] == [6, 7, 8]
    assert "ключевой момент" in window["messages"][1]["content"]


# ─── Scenario 5: access control for foreign dialogs ─────────────────────────


def test_scenario_5_access_control_for_foreign_dialogs(active_thread):
    """Given two orchestrators with their own dialogs,
    when  a non-DevAgent orchestrator searches, reads and lists,
    then  it only ever sees its own dialogs.
    """
    _make_thread("th_scenario_own", BETA, "Beta")
    _add_message("th_scenario_own", "user", "секрет беты про запуск")
    _make_thread("th_scenario_foreign", ALPHA, "Alpha")
    _add_message("th_scenario_foreign", "user", "секрет альфы про запуск")
    agent = _agent()
    agent.core._orchestrator_slug = BETA

    scoped = agent.dispatch("search_in_threads", {"query": "секрет", "scope": "all"})
    assert scoped["ok"] is True
    assert [h["thread_id"] for h in scoped["hits"]] == ["th_scenario_own"]
    assert "access-limited" in scoped["scope"]

    denied = agent.dispatch("search_in_threads", {
        "query": "секрет", "thread_id": "th_scenario_foreign"})
    assert denied["ok"] is False
    assert "Access denied" in denied["error"]

    denied_read = agent.dispatch("read_thread", {"thread_id": "th_scenario_foreign"})
    assert denied_read["ok"] is False
    assert "Access denied" in denied_read["error"]

    listed = agent.dispatch("list_threads", {})
    assert [t["thread_id"] for t in listed["threads"]] == ["th_scenario_own"]


# ─── Scenario 6: discover dialogs, paginate ─────────────────────────────────


def test_scenario_6_list_threads_discovery_and_pagination():
    """Given several dialogs of an orchestrator,
    when  the orchestrator lists them with pagination,
    then  ids, per-dialog counters and the truncated flag are correct.
    """
    for i, tid in enumerate(["th_sc_1", "th_sc_2", "th_sc_3"]):
        _make_thread(tid, ALPHA, "Alpha", title=f"Диалог {i}")
        _add_message(tid, "user", f"вопрос {i}")
        if tid == "th_sc_2":
            _add_message(tid, "assistant", "ответ")
        _set_updated(tid, f"2026-05-0{i + 1}T09:00:00")
    agent = _agent()

    page1 = agent.dispatch("list_threads", {"orchestrator": ALPHA, "limit": 2})
    assert page1["ok"] is True
    assert page1["truncated"] is True
    assert [t["thread_id"] for t in page1["threads"]] == ["th_sc_3", "th_sc_2"]
    counts = {t["thread_id"]: t["message_count"] for t in page1["threads"]}
    assert counts["th_sc_3"] == 1
    assert counts["th_sc_2"] == 2

    page2 = agent.dispatch("list_threads", {
        "orchestrator": ALPHA, "limit": 2, "offset": 2})
    assert [t["thread_id"] for t in page2["threads"]] == ["th_sc_1"]
    assert page2["truncated"] is False
