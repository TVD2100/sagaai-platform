# -*- coding: utf-8 -*-
"""Tests for the thread-search tools exposed by UniversalDevAgent.

Covers:
- the tool catalog and WORKSPACE_TOOL_ARGS entries for search_in_threads,
  list_threads and read_thread;
- default targeting of the CURRENT dialog (no thread listing required);
- explicit thread_id search without any prior list call;
- scope='orchestrator'/'all' plus the orchestrator argument forms;
- access control: the built-in DevAgent sees every dialog, other
  orchestrators only their own (foreign thread_id -> Access denied);
- read_thread defaulting to the current dialog and reporting indexes;
- stringified bool arguments ("true") are accepted.
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
    tmp = tempfile.mkdtemp(prefix="sagaai_test_tsrch_disp_")
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
CURRENT_TID = "th_current"
TOOL_RESULT_CONTENT = '{"tool_result": {"ok": true, "tool": "run_code"}}'


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


@pytest.fixture
def active_thread(monkeypatch):
    """Publish CURRENT_TID as the active dialog thread."""
    from dev_agent import config as dagent_config
    monkeypatch.setattr(dagent_config, "ACTIVE_THREAD_ID", CURRENT_TID)
    return CURRENT_TID


# ─── catalog and argument validation ─────────────────────────────────────────


def test_catalog_advertises_thread_search_tools():
    agent = _agent()
    names = {t["name"] for t in agent.tool_catalog}
    assert {"search_in_threads", "list_threads", "read_thread"} <= names


def test_thread_search_tools_inside_args_spec():
    from dev_agent.universal_agent import WORKSPACE_TOOL_ARGS
    spec = WORKSPACE_TOOL_ARGS["search_in_threads"]
    assert spec["required"] == {"query"}
    assert {"thread_id", "scope", "orchestrator", "date_from", "date_to",
            "date_field", "role", "regex", "include_tool_results",
            "max_results"} <= spec["optional"]
    assert WORKSPACE_TOOL_ARGS["list_threads"]["required"] == set()
    assert WORKSPACE_TOOL_ARGS["read_thread"]["required"] == set()
    assert {"thread_id", "offset", "limit", "include_tool_results"} <= \
        WORKSPACE_TOOL_ARGS["read_thread"]["optional"]


def test_unknown_args_rejected_with_suggestion():
    agent = _agent()
    res = agent.dispatch("search_in_threads", {"query": "x", "threads": ["a"]})
    assert res.get("ok") is False
    assert res.get("unknown_args") == ["threads"]
    assert "search_in_threads" in res.get("suggestion", "")


def test_missing_query_is_rejected():
    agent = _agent()
    res = agent.dispatch("search_in_threads", {"thread_id": "t1"})
    assert res.get("ok") is False
    assert "query" in res.get("error", "")


# ─── search_in_threads defaults and targeting ────────────────────────────────


def test_search_defaults_to_current_dialog_no_lists(active_thread):
    """A plain query searches the CURRENT dialog without any listing calls."""
    _make_thread(CURRENT_TID, ALPHA, "Alpha", title="Свой диалог")
    _add_message(CURRENT_TID, "user", "обсудили бюджет на квартал")
    _make_thread("th_other", BETA, "Beta")
    _add_message("th_other", "user", "бюджет другого агента")
    agent = _agent()

    res = agent.dispatch("search_in_threads", {"query": "бюджет"})

    assert res.get("ok") is True
    assert res.get("scope") == "thread"
    assert res.get("active_thread_id") == CURRENT_TID
    assert [h["thread_id"] for h in res["hits"]] == [CURRENT_TID]


def test_search_explicit_thread_id_without_lists(active_thread):
    """An explicit thread_id wins over the current dialog, lists not needed."""
    _make_thread(CURRENT_TID, ALPHA, "Alpha")
    _add_message(CURRENT_TID, "user", "тут слова нет")
    _make_thread("th_target", BETA, "Beta")
    _add_message("th_target", "assistant", "тут слово есть")
    agent = _agent()

    res = agent.dispatch("search_in_threads", {"query": "слово", "thread_id": "th_target"})

    assert res.get("ok") is True
    assert res.get("scope") == "thread"
    assert [h["thread_id"] for h in res["hits"]] == ["th_target"]


def test_search_scope_orchestrator_and_all(active_thread):
    _make_thread(CURRENT_TID, ALPHA, "Alpha")
    _add_message(CURRENT_TID, "user", "маркер у альфы")
    _make_thread("th_b", BETA, "Beta")
    _add_message("th_b", "user", "маркер у беты")
    agent = _agent()

    by_orch = agent.dispatch("search_in_threads", {
        "query": "маркер", "scope": "orchestrator", "orchestrator": BETA})
    assert [h["thread_id"] for h in by_orch["hits"]] == ["th_b"]
    assert by_orch["scope"].startswith("orchestrator:")

    all_res = agent.dispatch("search_in_threads", {"query": "маркер", "scope": "all"})
    assert {h["thread_id"] for h in all_res["hits"]} == {CURRENT_TID, "th_b"}
    assert all_res["scope"] == "all"


def test_search_accepts_comma_separated_orchestrators(active_thread):
    _make_thread("th_a2", ALPHA, "Alpha")
    _add_message("th_a2", "user", "общий маркер первым")
    _make_thread("th_b2", BETA, "Beta")
    _add_message("th_b2", "user", "общий маркер вторым")
    agent = _agent()

    res = agent.dispatch("search_in_threads", {
        "query": "маркер", "orchestrator": f"{ALPHA},{BETA}"})
    assert res.get("ok") is True
    assert {h["thread_id"] for h in res["hits"]} == {"th_a2", "th_b2"}


def test_search_without_active_dialog_returns_error(monkeypatch):
    from dev_agent import config as dagent_config
    monkeypatch.setattr(dagent_config, "ACTIVE_THREAD_ID", "")
    agent = _agent()
    res = agent.dispatch("search_in_threads", {"query": "x"})
    assert res.get("ok") is False
    assert "No active dialog thread" in res.get("error", "")


def test_search_invalid_scope_and_orchestrator_type(active_thread):
    agent = _agent()
    bad_scope = agent.dispatch("search_in_threads", {"query": "x", "scope": "planets"})
    assert bad_scope.get("ok") is False
    assert "scope" in bad_scope.get("error", "")

    bad_orch = agent.dispatch("search_in_threads", {"query": "x", "orchestrator": 42})
    assert bad_orch.get("ok") is False
    assert "orchestrator" in bad_orch.get("error", "")


# ─── list_threads ────────────────────────────────────────────────────────────


def test_list_threads_defaults_and_orchestrator_filter():
    _make_thread("th_l1", ALPHA, "Alpha", title="Диалог А")
    _add_message("th_l1", "user", "один")
    _make_thread("th_l2", BETA, "Beta", title="Диалог Б")
    _add_message("th_l2", "user", "два")
    _add_message("th_l2", "assistant", "два-ответ")
    agent = _agent()

    everything = agent.dispatch("list_threads", {})
    assert everything.get("ok") is True
    by_id = {t["thread_id"]: t for t in everything["threads"]}
    assert by_id["th_l2"]["message_count"] == 2
    assert by_id["th_l2"]["title"] == "Диалог Б"

    only_alpha = agent.dispatch("list_threads", {"orchestrator": ALPHA})
    assert [t["thread_id"] for t in only_alpha["threads"]] == ["th_l1"]


# ─── read_thread ─────────────────────────────────────────────────────────────


def test_read_thread_defaults_to_current_dialog(active_thread):
    _make_thread(CURRENT_TID, ALPHA, "Alpha", title="Текущий")
    _add_message(CURRENT_TID, "user", "первое")
    _add_message(CURRENT_TID, "assistant", "второе")
    _add_message(CURRENT_TID, "user", "третье")
    agent = _agent()

    res = agent.dispatch("read_thread", {"limit": 2})
    assert res.get("ok") is True
    assert res.get("thread_id") == CURRENT_TID
    assert res.get("active_thread_id") == CURRENT_TID
    assert [m["index"] for m in res["messages"]] == [0, 1]
    assert res.get("remaining") == 1
    assert res.get("has_more") is True



def test_read_thread_explicit_id_and_missing_thread(active_thread):
    _make_thread("th_r", ALPHA, "Alpha", title="Другой")
    _add_message("th_r", "user", "содержимое")
    agent = _agent()

    res = agent.dispatch("read_thread", {"thread_id": "th_r"})
    assert res.get("ok") is True
    assert res.get("thread_id") == "th_r"
    assert res["messages"][0]["content"] == "содержимое"

    missing = agent.dispatch("read_thread", {"thread_id": "nope"})
    assert missing.get("ok") is False
    assert "not found" in missing.get("error", "").lower()


def test_read_thread_stringified_bool_hides_tool_results(active_thread):
    _make_thread(CURRENT_TID, ALPHA, "Alpha")
    _add_message(CURRENT_TID, "user", "видимое")
    _add_message(CURRENT_TID, "user", TOOL_RESULT_CONTENT)
    agent = _agent()

    default = agent.dispatch("read_thread", {})
    assert [m["index"] for m in default["messages"]] == [0]

    with_flag = agent.dispatch("read_thread", {"include_tool_results": "true"})
    assert [m["index"] for m in with_flag["messages"]] == [0, 1]


# ─── access control ──────────────────────────────────────────────────────────


def test_devagent_sees_all_dialogs(active_thread):
    _make_thread("th_da", ALPHA, "Alpha")
    _add_message("th_da", "user", "секрет альфы")
    _make_thread("th_db", BETA, "Beta")
    _add_message("th_db", "user", "секрет беты")
    agent = _agent()
    assert agent.core._orchestrator_slug == "dev_agent"

    res = agent.dispatch("search_in_threads", {"query": "секрет", "scope": "all"})
    assert {h["thread_id"] for h in res["hits"]} == {"th_da", "th_db"}

    listed = agent.dispatch("list_threads", {})
    assert {t["thread_id"] for t in listed["threads"]} == {"th_da", "th_db"}


def test_non_devagent_orchestrator_is_limited(active_thread):
    _make_thread("th_own", ALPHA, "Alpha")
    _add_message("th_own", "user", "секрет альфы")
    _make_thread("th_foreign", BETA, "Beta")
    _add_message("th_foreign", "user", "секрет беты")
    agent = _agent()
    agent.core._orchestrator_slug = ALPHA

    scoped = agent.dispatch("search_in_threads", {"query": "секрет", "scope": "all"})
    assert scoped.get("ok") is True
    assert [h["thread_id"] for h in scoped["hits"]] == ["th_own"]
    assert "access-limited" in scoped["scope"]

    denied = agent.dispatch("search_in_threads", {"query": "секрет", "thread_id": "th_foreign"})
    assert denied.get("ok") is False
    assert "Access denied" in denied.get("error", "")

    denied_read = agent.dispatch("read_thread", {"thread_id": "th_foreign"})
    assert denied_read.get("ok") is False
    assert "Access denied" in denied_read.get("error", "")

    listed = agent.dispatch("list_threads", {})
    assert [t["thread_id"] for t in listed["threads"]] == ["th_own"]

    foreign_scope = agent.dispatch("search_in_threads", {
        "query": "секрет", "scope": "orchestrator", "orchestrator": BETA})
    assert foreign_scope.get("ok") is False


def test_non_devagent_defaults_orchestrator_scope_to_itself(active_thread):
    _make_thread("th_self1", ALPHA, "Alpha")
    _add_message("th_self1", "user", "маркер свой")
    _make_thread("th_self2", BETA, "Beta")
    _add_message("th_self2", "user", "маркер чужой")
    agent = _agent()
    agent.core._orchestrator_slug = BETA

    res = agent.dispatch("search_in_threads", {"query": "маркер", "scope": "orchestrator"})
    assert [h["thread_id"] for h in res["hits"]] == ["th_self2"]
