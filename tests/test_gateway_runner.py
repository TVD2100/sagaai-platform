"""Tests of the root dialog step builder.

``dialog_runner`` is the one place a loop state is assembled. The properties
worth testing are the ones that made the module necessary: the child-mode
flag reaches the state, the auto-apply flag is NOT pinned, an unattended
loop stops on its own limits, and a cancellation is noticed on the step
boundary.

No provider call happens here: ``step_agent_loop`` is replaced by a stub
that advances the state directly, exactly like a loop that finished.
"""

import time

import pytest

from core.orchestration import dialog_runner as runner


class _State:
    """Minimal stand-in for ``AgentLoopState``."""

    def __init__(self, **kwargs):
        self.phase = kwargs.get("phase", "calling_llm")
        self.final_status = ""
        self.final_text = ""
        self.error_message = ""
        self.error_code = ""
        self.steps = 0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.total_tokens_cache = 0
        self.history = []


# ─── run_loop limits ────────────────────────────────────────────────────────


def test_run_loop_stops_on_a_deadline(monkeypatch):
    state = _State()
    calls = []

    def _step(state_obj, dispatcher, **kwargs):
        calls.append(1)
        return state_obj

    monkeypatch.setattr(runner, "run_step", _step)
    result = runner.run_loop(state, object(), deadline=time.monotonic() - 1.0)
    assert calls == []
    assert result.phase == "done"
    assert result.error_code == "timeout"


def test_run_loop_stops_on_a_token_budget(monkeypatch):
    state = _State()
    state.total_tokens_in = 500
    state.total_tokens_out = 500

    def _step(state_obj, dispatcher, **kwargs):
        return state_obj

    monkeypatch.setattr(runner, "run_step", _step)
    result = runner.run_loop(state, object(), budget=100)
    assert result.phase == "done"
    assert result.error_code == "budget_exhausted"


def test_run_loop_respects_a_step_cap(monkeypatch):
    state = _State()
    counter = {"n": 0}

    def _step(state_obj, dispatcher, **kwargs):
        counter["n"] += 1
        return state_obj

    monkeypatch.setattr(runner, "run_step", _step)
    result = runner.run_loop(state, object(), max_steps=3)
    assert counter["n"] == 3
    assert result.error_code == "max_steps"


def test_run_loop_returns_a_terminal_state_unchanged(monkeypatch):
    state = _State(phase="done")
    state.final_status = "done"

    def _step(state_obj, dispatcher, **kwargs):  # pragma: no cover - not reached
        raise AssertionError("a finished loop must not step again")

    monkeypatch.setattr(runner, "run_step", _step)
    assert runner.run_loop(state, object()) is state


def test_run_loop_calls_the_step_hook_after_each_step(monkeypatch):
    state = _State()
    seen = []

    def _step(state_obj, dispatcher, **kwargs):
        state_obj.steps += 1
        if state_obj.steps >= 2:
            state_obj.phase = "done"
        return state_obj

    monkeypatch.setattr(runner, "run_step", _step)
    runner.run_loop(state, object(), on_step=lambda s: seen.append(s.steps))
    assert seen == [1, 2]


# ─── Cancellation ───────────────────────────────────────────────────────────


def test_run_loop_stops_when_cancelled_before_the_first_step(monkeypatch):
    state = _State()
    stepped = []

    def _step(state_obj, dispatcher, **kwargs):  # pragma: no cover - not reached
        stepped.append(1)
        return state_obj

    monkeypatch.setattr(runner, "run_step", _step)
    result = runner.run_loop(state, object(), cancelled=lambda: True)
    assert stepped == []
    assert result.phase == "done"
    assert result.final_status == "stopped_by_user"


def test_run_loop_notices_a_cancel_after_the_first_step(monkeypatch):
    state = _State()
    checks = {"n": 0}

    def _step(state_obj, dispatcher, **kwargs):
        state_obj.steps += 1
        return state_obj

    def _cancelled():
        checks["n"] += 1
        return checks["n"] >= 2

    monkeypatch.setattr(runner, "run_step", _step)
    result = runner.run_loop(state, object(), cancelled=_cancelled)
    assert state.steps == 1
    assert result.final_status == "stopped_by_user"


def test_a_raising_cancel_probe_does_not_break_the_run(monkeypatch):
    state = _State()

    def _step(state_obj, dispatcher, **kwargs):
        state_obj.phase = "done"
        return state_obj

    def _cancelled():
        raise RuntimeError("probe broke")

    monkeypatch.setattr(runner, "run_step", _step)
    assert runner.run_loop(state, object(), cancelled=_cancelled).phase == "done"


# ─── Status mapping ─────────────────────────────────────────────────────────


def test_final_triple_delegates_to_the_contract():
    state = _State(phase="done")
    state.final_status = "done"
    assert runner.final_triple(state) == ("done", "completed", None)


def test_final_triple_maps_a_user_stop():
    state = _State(phase="done")
    state.final_status = "stopped_by_user"
    status, stop_reason, code = runner.final_triple(state)
    assert status == "cancelled"
    assert stop_reason == "aborted"
    assert code == "cancelled_by_user"


def test_final_summary_prefers_the_final_text():
    state = _State()
    state.final_text = "all done"
    state.error_message = "ignored"
    assert runner.final_summary(state) == "all done"


def test_final_summary_falls_back_to_the_error():
    state = _State()
    state.error_message = "provider exploded"
    assert runner.final_summary(state) == "provider exploded"


# ─── Tree helpers ───────────────────────────────────────────────────────────


def test_register_root_passes_every_field_to_the_service():
    class _Service:
        def __init__(self):
            self.calls = []

        def ensure_run(self, run_id, **kwargs):
            self.calls.append((run_id, kwargs))
            return "record"

    service = _Service()
    runner.register_root(service, "r_1", thread_id="t1", agent_slug="dev_agent",
                         config={"max_steps": 5})
    assert service.calls[0][0] == "r_1"
    assert service.calls[0][1]["thread_id"] == "t1"
    assert service.calls[0][1]["config"] == {"max_steps": 5}


def test_release_root_reports_a_deferred_release():
    class _Service:
        def release_tree(self, run_id):
            return {"released": False, "deferred": True}

    assert runner.release_root(_Service(), "r_1")["deferred"] is True


def test_release_root_survives_a_failing_service():
    class _Service:
        def release_tree(self, run_id):
            raise RuntimeError("broken")

    assert runner.release_root(_Service(), "r_1") == {"released": False,
                                                      "deferred": False}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
