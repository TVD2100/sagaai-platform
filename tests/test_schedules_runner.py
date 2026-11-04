# -*- coding: utf-8 -*-
"""
tests.test_schedules_runner - one scheduled run becomes a real dialog.

The product requirement behind these tests is exact: a run fired by the
scheduler must appear in History as an ordinary dialog, with the task, the
assistant messages and the tool events stored exactly like a typed message
would be. A stub provider keeps the test offline and makes the loop's
behaviour deterministic.
"""
import json
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from core.schedules import runner, store  # noqa: E402
from core.schedules.model import Schedule  # noqa: E402


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Give the test its own databases and history folder."""
    import storage.db as db
    import core.paths as paths

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(db, "_devagent_engine", None)
    monkeypatch.setattr(db, "_DevAgentSessionLocal", None)
    monkeypatch.setattr(paths, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(paths, "DEVAGENT_DB_PATH", str(tmp_path / "devagent.db"))
    history = tmp_path / "history"
    history.mkdir()
    monkeypatch.setattr(paths, "HISTORY_DIR", str(history))
    for module in ("core.threads_devagent",):
        mod = sys.modules.get(module)
        if mod is not None:
            monkeypatch.setattr(mod, "HISTORY_DIR", str(history), raising=False)
    yield tmp_path
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(db, "_devagent_engine", None)
    monkeypatch.setattr(db, "_DevAgentSessionLocal", None)


def _rule(**overrides):
    base = Schedule(
        id="sch0001",
        title="Nightly digest",
        agent_slug="dev_agent",
        task_text="Summarize yesterday's commits",
        first_run_at=1772000000,
        next_run_at=1772000000,
        interval_value=1,
        interval_unit="days",
        max_steps=5,
    )
    return base.clone(**overrides)


class TestTaskText:
    def test_the_owner_task_comes_first_and_unchanged(self):
        schedule = _rule()
        text = runner.build_task_text(schedule, 1772000000)
        assert text.startswith("Summarize yesterday's commits")

    def test_the_framing_says_nobody_will_answer(self):
        text = runner.build_task_text(_rule(), 1772000000)
        assert "Пользователя в этом диалоге нет" in text
        assert "loop_status" in text

    def test_an_optional_brief_is_included(self):
        text = runner.build_task_text(_rule(brief_text="Write to reports/daily.md"), 1772000000)
        assert "Write to reports/daily.md" in text

    def test_the_title_names_the_schedule_and_the_moment(self):
        title = runner._thread_title(_rule(), 1772000000)
        assert title.startswith("\u23f0 Nightly digest")
        assert "." in title


class TestEffectiveConfig:
    def test_the_limits_come_from_the_rule(self):
        limits = runner._effective_config(_rule(max_steps=42, budget_tokens=9000,
                                              timeout_seconds=120, max_depth=0))
        assert limits["subagent_max_steps"] == 42
        assert limits["subagent_budget_tokens"] == 9000
        assert limits["subagent_timeout_seconds"] == 120
        assert limits["max_depth"] == 0

    def test_a_zero_budget_means_no_budget(self):
        assert runner._effective_config(_rule(budget_tokens=0))["subagent_budget_tokens"] == 0

    def test_delegation_is_closed_by_default(self):
        limits = runner._effective_config(_rule())
        assert limits["max_depth"] == 0
        assert limits["max_active_subagents"] == 1


class TestFinalStatus:
    @pytest.mark.parametrize("raw,expected", [
        ("done", "done"),
        ("applied", "done"),
        ("awaiting_user", "awaiting_input"),
        ("awaiting_confirmation", "awaiting_input"),
        ("stopped_by_user", "cancelled"),
        ("stopped_max_steps", "failed"),
        ("error", "failed"),
        ("", "failed"),
    ])
    def test_statuses_map_onto_the_history_vocabulary(self, raw, expected):
        class _State:
            final_status = raw
        assert runner._final_status(_State()) == expected


class TestDialogIsARealThread:
    def test_a_run_creates_an_ordinary_dialog(self, isolated_state, monkeypatch):
        from core.threads_devagent import list_devagent_threads

        store.create_schedule(_rule())
        monkeypatch.setattr(runner, "_make_dispatcher", lambda schedule, thread_id: (_StubDispatcher(), {}, {}))
        thread_id = runner._create_dialog(_rule(), 1772000000)
        threads = list_devagent_threads()
        assert [item["thread_id"] for item in threads] == [thread_id]
        assert threads[0]["parent_thread_id"] in (None, "")
        assert threads[0]["assistant_id"] == "dev_agent"
        assert threads[0]["title"].startswith("\u23f0 Nightly digest")

    def test_the_workspace_is_persisted_with_the_thread(self, isolated_state):
        import os
        from core.threads_devagent import list_devagent_threads

        thread_id = runner._create_dialog(_rule(workspace=str(isolated_state)), 1772000000)
        threads = {item["thread_id"]: item for item in list_devagent_threads()}
        assert threads[thread_id]["workspace"] == str(isolated_state)
        assert os.path.isdir(threads[thread_id]["workspace"])

    def test_a_missing_workspace_is_not_recorded(self, isolated_state):
        from core.threads_devagent import list_devagent_threads

        thread_id = runner._create_dialog(_rule(workspace="/definitely/not/here"), 1772000000)
        threads = {item["thread_id"]: item for item in list_devagent_threads()}
        assert threads[thread_id]["workspace"] in (None, "")


class _StubDispatcher:
    """Dispatcher stand-in: the loop is not exercised here."""


class TestMessageAppend:
    def test_messages_are_written_to_the_thread(self, isolated_state):
        from core.threads_devagent import load_thread_messages

        store.create_schedule(_rule())
        thread_id = runner._create_dialog(_rule(), 1772000000)
        messages = [
            {"role": "user", "content": "Do the thing"},
            {"role": "assistant", "content": "Done", "_events": [{"type": "step"}]},
        ]
        cursor = runner._append_messages(thread_id, messages, 0)
        assert cursor == 2
        stored = load_thread_messages(thread_id)
        assert [item["content"] for item in stored] == ["Do the thing", "Done"]
        assert stored[1].get("_events") == [{"type": "step"}]

    def test_repeated_appends_do_not_duplicate(self, isolated_state):
        from core.threads_devagent import load_thread_messages

        thread_id = runner._create_dialog(_rule(), 1772000000)
        messages = [{"role": "user", "content": "one"}]
        cursor = runner._append_messages(thread_id, messages, 0)
        messages.append({"role": "assistant", "content": "two"})
        cursor = runner._append_messages(thread_id, messages, cursor)
        assert cursor == 2
        assert len(load_thread_messages(thread_id)) == 2


class TestResultRecording:
    def test_a_successful_run_closes_the_history_row(self, isolated_state):
        store.create_schedule(_rule(next_run_at=1772000000))
        row = store.claim_due("sch0001", 1772000000, next_run_at=1772086400, now=1772000000)
        runner._record_result(_rule(), 1772000000, row["id"], {
            "status": "done", "thread_id": "t1", "run_id": "r1",
            "steps": 4, "tokens": {"in": 10, "out": 5, "cache": 0},
            "summary": "Report written", "error_code": "", "stop_reason": "",
        })
        stored = store.get_run_row(row["id"])
        assert stored["status"] == "done"
        assert stored["thread_id"] == "t1"
        assert stored["steps_used"] == 4
        rule = store.get_schedule("sch0001")
        assert rule.run_count == 1
        assert rule.consecutive_failures == 0
        assert rule.status == "active"

    def test_three_failures_hold_the_rule(self, isolated_state):
        store.create_schedule(_rule(consecutive_failures=2, fail_count=2, run_count=2))
        row = store.claim_due("sch0001", 1772000000, next_run_at=1772086400, now=1772000000)
        runner._record_result(_rule(consecutive_failures=2, fail_count=2, run_count=2),
                              1772000000, row["id"], {
            "status": "failed", "thread_id": "t1", "run_id": "r1",
            "steps": 1, "tokens": {}, "summary": "boom",
            "error_code": "run_failed", "stop_reason": "",
        })
        rule = store.get_schedule("sch0001")
        assert rule.consecutive_failures == 3
        assert rule.status == "error_hold"

    def test_a_success_resets_the_failure_streak(self, isolated_state):
        store.create_schedule(_rule(consecutive_failures=2, fail_count=2, run_count=2))
        row = store.claim_due("sch0001", 1772000000, next_run_at=1772086400, now=1772000000)
        runner._record_result(_rule(consecutive_failures=2),
                              1772000000, row["id"], {
            "status": "done", "thread_id": "t1", "run_id": "r1",
            "steps": 2, "tokens": {}, "summary": "ok",
            "error_code": "", "stop_reason": "",
        })
        assert store.get_schedule("sch0001").consecutive_failures == 0

    def test_reaching_max_runs_finishes_the_rule(self, isolated_state):
        store.create_schedule(_rule(max_runs=1))
        row = store.claim_due("sch0001", 1772000000, next_run_at=1772086400, now=1772000000)
        runner._record_result(_rule(max_runs=1), 1772000000, row["id"], {
            "status": "done", "thread_id": "t1", "run_id": "r1",
            "steps": 1, "tokens": {}, "summary": "ok",
            "error_code": "", "stop_reason": "",
        })
        assert store.get_schedule("sch0001").status == "finished"


class TestRunFailureIsRecorded:
    def test_a_broken_run_returns_failed_and_writes_history(self, isolated_state, monkeypatch):
        store.create_schedule(_rule(next_run_at=1772000000))
        row = store.claim_due("sch0001", 1772000000, next_run_at=1772086400, now=1772000000)

        def _boom(schedule, planned_at):
            raise RuntimeError("provider is down")

        monkeypatch.setattr(runner, "_create_dialog", _boom)
        report = runner.run_schedule(_rule(), 1772000000, run_row_id=row["id"])
        assert report["status"] == "failed"
        stored = store.get_run_row(row["id"])
        assert stored["status"] == "failed"
        assert "provider is down" in stored["summary"]

    def test_a_budget_stop_is_reported_as_failed_with_its_code(self, isolated_state, monkeypatch):
        store.create_schedule(_rule(next_run_at=1772000000, budget_tokens=1))
        row = store.claim_due("sch0001", 1772000000, next_run_at=1772086400, now=1772000000)

        class _State:
            phase = "calling_llm"
            total_tokens_in = 100
            total_tokens_out = 100
            total_tokens_cache = 0
            steps = 1
            history = []
            final_status = ""
            final_text = ""

        monkeypatch.setattr(runner, "_create_dialog", lambda schedule, planned_at: "thr1")
        monkeypatch.setattr(runner, "_make_dispatcher", lambda schedule, thread_id: (_StubDispatcher(), {}, {}))
        monkeypatch.setattr("dev_agent.agent_loop.AgentLoopState", lambda **kwargs: _State())
        report = runner.run_schedule(_rule(budget_tokens=1), 1772000000, run_row_id=row["id"])
        assert report["status"] == "failed"
        assert report["error_code"] == "budget_exhausted"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
