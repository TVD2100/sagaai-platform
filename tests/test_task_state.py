# -*- coding: utf-8 -*-
"""tests/test_task_state.py - unit tests for the per-thread task-state journal.

Covers the dev_agent.task_state module (journal rendering/parsing, section
updates, step marking with Progress regeneration, archiving, legacy
migration, and the context-injection helper) plus the ToolExecutor
task_state_* tool methods.

Uses an isolated temporary workspace via set_target_root so the tests never
write into the real project root, and an isolated thread id so journal files
are written into TASK_STATE__<thread_id>.md under the temp root.
"""
import pytest

from dev_agent import config
from dev_agent import task_state as ts


@pytest.fixture
def sandbox(tmp_path):
    """Repoint DevAgent at a temp workspace and isolate the thread id."""
    old_root = config.PROJECT_ROOT
    old_thread = config.ACTIVE_THREAD_ID
    try:
        config.set_target_root(tmp_path)
        config.ACTIVE_THREAD_ID = "test_thread_123"
        yield tmp_path
    finally:
        config.set_target_root(old_root)
        config.ACTIVE_THREAD_ID = old_thread


PLAN_TEXT = (
    "### Step 1 - Data layer\n"
    "- verification: run tests/test_step1.py\n"
    "### Step 2 - API layer\n"
    "- verification: run tests/test_step2.py\n"
)


# --- Journal structure & parsing ----------------------------------------

def test_journal_file_name_embeds_thread_id(sandbox):
    ts.ensure_task_state_file()
    path = ts.task_state_path()
    assert path.name == "TASK_STATE__test_thread_123.md"
    assert path.parent == config.TASK_STATES_DIR


def test_build_contains_all_sections():
    text = ts.build_task_state(
        task="Build X", architecture="modular", plan=PLAN_TEXT,
        progress="none", handoff="handoff facts",
    )
    assert "## Active Task" in text
    assert "## Task History" in text
    for title in ("Task", "Architecture", "Plan", "Progress", "Handoff"):
        assert ("### " + title) in text
    assert "Build X" in text
    assert "### Step 1 - Data layer" in text


def test_split_active_sections_roundtrip():
    text = ts.build_task_state(
        task="Build X", architecture="modular", plan=PLAN_TEXT, handoff="facts",
    )
    top = ts._split_top_sections(text)
    active = ts._split_active_sections(top["active_task"])
    assert active["task"] == "Build X"
    assert active["architecture"] == "modular"
    assert "Step 1" in active["plan"]
    assert active["handoff"] == "facts"


def test_extract_step_ids():
    text = ts.build_task_state(task="t", plan=PLAN_TEXT)
    assert ts.extract_step_ids(text) == ["step_1", "step_2"]


def test_parse_step_handles_variants():
    assert ts._parse_step("### Step 1 - Title")["id"] == "step_1"
    assert ts._parse_step("### Step 2 -- Title")["id"] == "step_2"
    assert ts._parse_step("### Step 3 : Title")["id"] == "step_3"
    assert ts._parse_step("### Step 10\u2014Title")["id"] == "step_10"


# --- File operations ----------------------------------------------------

def test_ensure_and_read_roundtrip(sandbox):
    res = ts.ensure_task_state_file()
    assert res.get("ok") and res.get("wrote")
    assert res.get("thread_id") == "test_thread_123"
    r = ts.read_task_state()
    assert r["ok"] and r["exists"]
    assert r["path"].endswith("TASK_STATE__test_thread_123.md")
    assert r["thread_id"] == "test_thread_123"
    assert set(r["sections"]) == {"task", "architecture", "plan", "progress", "handoff"}
    assert r["history"] == []


def test_ensure_existing_not_overwritten(sandbox):
    ts.ensure_task_state_file()
    path = ts.task_state_path()
    path.write_text("custom\n", encoding="utf-8")
    res = ts.ensure_task_state_file()
    assert res.get("ok") and not res.get("wrote")
    assert path.read_text(encoding="utf-8") == "custom\n"
    res2 = ts.ensure_task_state_file(force=True)
    assert res2.get("ok") and res2.get("wrote")
    assert path.read_text(encoding="utf-8") != "custom\n"


def test_update_section_preserves_others(sandbox):
    ts.ensure_task_state_file()
    ts.update_task_state_section("task", "Goal A")
    ts.update_task_state_section("plan", PLAN_TEXT)
    ts.update_task_state_section("handoff", "fact-1")
    r = ts.read_task_state()
    assert r["sections"]["task"] == "Goal A"
    assert r["sections"]["handoff"] == "fact-1"
    assert r["sections"]["architecture"] == "_(not set)_"
    assert r["step_ids"] == ["step_1", "step_2"]


def test_update_section_unknown_rejected(sandbox):
    ts.ensure_task_state_file()
    res = ts.update_task_state_section("bogus", "x")
    assert not res.get("ok")
    assert "Unknown section" in res.get("error", "")


def test_mark_step_updates_status_and_progress(sandbox):
    ts.ensure_task_state_file()
    ts.update_task_state_section("plan", PLAN_TEXT)
    res = ts.update_plan_step_status(
        "step_1", status="done", verification="12 passed", result="module created",
    )
    assert res.get("ok") and res.get("status") == "done"
    r = ts.read_task_state()
    assert "(status: done)" in r["sections"]["plan"]
    assert "- verification: 12 passed" in r["sections"]["plan"]
    assert "- result: module created" in r["sections"]["plan"]
    assert "- [x] Step 1 - Data layer" in r["sections"]["progress"]
    assert "- [ ] Step 2 - API layer" in r["sections"]["progress"]
    assert "Progress: 1/2 steps done." in r["sections"]["progress"]


def test_mark_step_records_context_for_next_step(sandbox):
    ts.ensure_task_state_file()
    ts.update_task_state_section("plan", PLAN_TEXT)
    res = ts.update_plan_step_status(
        "step_1", status="done", context="storage API chosen: SQLite",
    )
    assert res.get("ok")
    r = ts.read_task_state()
    assert "- context: storage API chosen: SQLite" in r["sections"]["plan"]


def test_mark_step_replaces_existing_meta(sandbox):
    ts.ensure_task_state_file()
    ts.update_task_state_section("plan", PLAN_TEXT)
    ts.update_plan_step_status("step_1", status="done", verification="a", result="b")
    ts.update_plan_step_status("step_1", status="done", verification="c", result="d")
    r = ts.read_task_state()
    plan = r["sections"]["plan"]
    assert "- verification: c" in plan
    assert "- result: d" in plan
    assert "- verification: a" not in plan
    assert "- result: b" not in plan


def test_mark_step_unknown_fails(sandbox):
    ts.ensure_task_state_file()
    ts.update_task_state_section("plan", PLAN_TEXT)
    res = ts.update_plan_step_status("step_9")
    assert not res.get("ok")
    assert "Step not found" in res.get("error", "")


def test_mark_step_invalid_status_fails(sandbox):
    ts.ensure_task_state_file()
    ts.update_task_state_section("plan", PLAN_TEXT)
    res = ts.update_plan_step_status("step_1", status="bogus")
    assert not res.get("ok")
    assert "Invalid status" in res.get("error", "")


# --- Lifecycle: archive and never delete ---------------------------------

def test_clear_archives_active_task_and_keeps_file(sandbox):
    ts.ensure_task_state_file()
    ts.update_task_state_section("task", "Task One")
    ts.update_task_state_section("plan", PLAN_TEXT)
    ts.update_plan_step_status("step_1", status="done", result="layer done")
    path = ts.task_state_path()
    res = ts.clear_task_state()
    assert res.get("ok") and res.get("archived")
    assert path.exists(), "journal file must never be deleted"
    r = ts.read_task_state()
    assert len(r["history"]) == 1
    entry = r["history"][0]
    assert entry["task"] == "Task One"
    assert entry["completed_steps"] == "1/2"
    assert "layer done" in entry["summary"]
    res2 = ts.clear_task_state()
    assert res2.get("ok") and not res2.get("archived")


def test_archive_and_start_task_appends_to_same_journal(sandbox):
    first = ts.archive_and_start_task(task="Task One", plan=PLAN_TEXT)
    assert first.get("ok") and not first.get("archived_previous")
    ts.update_plan_step_status("step_1", status="done", context="fact-A")
    second = ts.archive_and_start_task(
        task="Task Two", plan="### Step 1 - New data\n"
    )
    assert second.get("ok") and second.get("archived_previous")
    assert second["path"] == first["path"], "same thread must reuse the SAME journal"
    r = ts.read_task_state()
    assert r["sections"]["task"] == "Task Two"
    assert len(r["history"]) == 1
    assert r["history"][0]["task"] == "Task One"
    assert "fact-A" in r["history"][0]["summary"]


def test_history_survives_multiple_tasks(sandbox):
    for name in ("T1", "T2", "T3"):
        ts.archive_and_start_task(task=name)
        assert ts.read_task_state()["sections"]["task"] == name
    r = ts.read_task_state()
    assert len(r["history"]) == 2
    assert [e["task"] for e in r["history"]] == ["T1", "T2"]


# --- Legacy migration ----------------------------------------------------

def test_legacy_root_file_migrated_once(sandbox):
    legacy = config.PROJECT_ROOT / ts.TASK_STATE_FILENAME
    legacy.write_text(
        "# Task State\n\n"
        "## Task\n\nLegacy Goal\n\n"
        "## Plan\n\n### Step 1 - Old work\n- result: old-result\n",
        encoding="utf-8",
    )
    ts.ensure_task_state_file()
    journal = ts.task_state_path()
    assert journal.exists()
    assert not legacy.exists(), "legacy file must be renamed after migration"
    assert legacy.with_suffix(".md.legacy").exists()
    r = ts.read_task_state()
    assert len(r["history"]) == 1
    assert r["history"][0]["task"] == "Legacy Goal"
    assert "old-result" in r["history"][0]["summary"]


# --- Context injection helper -------------------------------------------

def test_context_helper_missing_returns_none(sandbox):
    assert ts.task_state_for_context() is None


def test_context_helper_returns_block_with_meta(sandbox):
    ts.archive_and_start_task(task="Goal A")
    block = ts.task_state_for_context()
    assert block is not None
    assert block.startswith("CURRENT TASK STATE:")
    assert "thread_id: test_thread_123" in block
    assert "task_state_file:" in block
    assert "Goal A" in block


def test_context_includes_recent_history(sandbox):
    ts.archive_and_start_task(
        task="Finished job", plan="### Step 1 - X\n- verification: t\n"
    )
    ts.update_plan_step_status("step_1", status="done", context="kept-fact")
    ts.archive_and_start_task(task="Current job")
    block = ts.task_state_for_context()
    assert block is not None
    assert "## Recent Task History" in block
    assert "Finished job" in block
    assert "kept-fact" in block
    assert "Current job" in block


def test_context_helper_truncates(sandbox, monkeypatch):
    ts.ensure_task_state_file()
    ts.update_task_state_section("task", "x" * 200)
    monkeypatch.setattr(ts, "MAX_STATE_CHARS", 100)
    block = ts.task_state_for_context()
    assert block is not None
    assert "[truncated]" in block


# --- ToolExecutor integration --------------------------------------------

def test_tool_methods_roundtrip(sandbox):
    from dev_agent.tool_executor import ToolExecutor
    ex = ToolExecutor()
    init = ex.task_state_init(
        task="Build X", architecture="modular", plan=PLAN_TEXT,
    )
    assert init.get("ok")
    assert init.get("step_ids") == ["step_1", "step_2"]
    assert init.get("thread_id") == "test_thread_123"
    read = ex.task_state_read()
    assert read.get("ok") and read.get("exists")
    assert read["sections"]["task"] == "Build X"
    mark = ex.task_state_mark_step(
        "step_1", status="done", verification="ok", context="ctx"
    )
    assert mark.get("ok")
    upd = ex.task_state_update("handoff", "fact-1")
    assert upd.get("ok") and upd.get("section") == "handoff"
    archived = ex.task_state_clear()
    assert archived.get("ok") and archived.get("archived")


def test_tool_init_archives_previous_task(sandbox):
    from dev_agent.tool_executor import ToolExecutor
    ex = ToolExecutor()
    ex.task_state_init(task="First", plan=PLAN_TEXT)
    ex.task_state_update("handoff", "first-fact")
    res = ex.task_state_init(task="Second", plan="### Step 1 - S\n")
    assert res.get("ok") and res.get("archived_previous")
    read = ex.task_state_read()
    assert read["sections"]["task"] == "Second"
    assert read["history"][0]["task"] == "First"
    assert "first-fact" in read["history"][0]["summary"]


def test_tool_catalog_lists_task_state_tools():
    from dev_agent.tool_executor import TOOL_CATALOG
    names = {e["name"] for e in TOOL_CATALOG}
    for expected in ("task_state_init", "task_state_read", "task_state_update",
                     "task_state_mark_step", "task_state_clear"):
        assert expected in names


# --- agent_loop wiring ----------------------------------------------------

def test_agent_loop_helpers_present():
    from dev_agent import agent_loop
    assert callable(agent_loop._maybe_task_state_context)
    assert callable(agent_loop._with_task_state)
    out = agent_loop._with_task_state([{"role": "user", "content": "hi"}])
    assert isinstance(out, list)
    assert out[0]["role"] == "user"
