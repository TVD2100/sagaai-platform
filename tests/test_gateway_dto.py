"""Tests of the gateway transport types.

The types are dumb on purpose, and that is what these tests check: a frozen
dataclass, a ``to_dict`` that returns plain JSON-able values, and a request
validator that refuses the shapes a caller gets wrong most often.
"""

import json

import pytest

from core.gateway.dto import (
    AssistantCard,
    AssistantDetail,
    DialogHandle,
    DialogRequest,
    DialogSnapshot,
    DialogStepResult,
    EconomyOptions,
    HealthReport,
    MessageView,
    OrchestratorCard,
    OrchestratorDetail,
    PendingInput,
    RunSnapshot,
    SearchPage,
    ThreadPage,
    ThreadSummary,
    ThreadWindow,
)


def test_dialog_request_accepts_a_complete_request():
    request = DialogRequest(agent_slug="dev_agent", message="Do the thing",
                            workspace="/tmp/project", mode="auto")
    assert request.validate() is None


def test_dialog_request_requires_agent_and_message():
    assert DialogRequest(agent_slug="", message="x").validate() is not None
    assert DialogRequest(agent_slug="dev_agent", message="  ").validate() is not None


def test_dialog_request_rejects_an_unknown_mode():
    request = DialogRequest(agent_slug="dev_agent", message="x", mode="later")
    assert "mode" in str(request.validate())


def test_dialog_request_rejects_a_relative_workspace():
    request = DialogRequest(agent_slug="dev_agent", message="x",
                            workspace="project/sub")
    assert "absolute" in str(request.validate())


def test_dialog_request_defaults_leave_auto_apply_unset():
    """An unset auto_apply means "read the card", so it must stay None."""
    assert DialogRequest(agent_slug="a", message="b").auto_apply is None


def test_dialog_handle_is_frozen():
    handle = DialogHandle(thread_id="t1", run_id="r_1", agent_slug="dev_agent")
    with pytest.raises(Exception):
        handle.thread_id = "t2"  # type: ignore[misc]


def test_dialog_handle_round_trips_through_json():
    handle = DialogHandle(thread_id="t1", run_id="r_1", agent_slug="dev_agent",
                          created=True, mode="auto")
    payload = json.loads(json.dumps(handle.to_dict()))
    assert payload["thread_id"] == "t1"
    assert payload["run_id"] == "r_1"
    assert payload["created"] is True


def test_pending_input_keeps_its_details():
    pending = PendingInput(kind="confirmation", tool="run_code",
                           args={"code": "print(1)"}, reasons=["shell"])
    payload = pending.to_dict()
    assert payload["kind"] == "confirmation"
    assert payload["tool"] == "run_code"
    assert payload["args"] == {"code": "print(1)"}
    assert payload["reasons"] == ["shell"]


def test_message_view_serialises_events_and_tokens():
    message = MessageView(index=7, role="assistant", content="done",
                          events=[{"type": "step"}], tokens={"in": 5, "out": 3})
    payload = message.to_dict()
    assert payload["index"] == 7
    assert payload["events"] == [{"type": "step"}]
    assert payload["tokens"]["in"] == 5


def test_thread_page_and_window_serialise_nested_types():
    summary = ThreadSummary(thread_id="t1", title="Hello", message_count=4)
    page = ThreadPage(items=[summary], total=1, limit=50)
    window = ThreadWindow(thread=summary, messages=[MessageView(index=0)],
                          total=1, remaining=0)
    assert page.to_dict()["items"][0]["thread_id"] == "t1"
    nested = window.to_dict()
    assert nested["thread"]["title"] == "Hello"
    assert nested["messages"][0]["index"] == 0


def test_run_snapshot_knows_when_it_is_terminal():
    assert RunSnapshot(run_id="r", status="done").is_terminal
    assert RunSnapshot(run_id="r", status="failed").is_terminal
    assert RunSnapshot(run_id="r", status="cancelled").is_terminal
    assert not RunSnapshot(run_id="r", status="running").is_terminal
    assert not RunSnapshot(run_id="r", status="awaiting_input").is_terminal


def test_run_snapshot_carries_pending_input_into_its_dict():
    snapshot = RunSnapshot(run_id="r", status="awaiting_input",
                           pending_input=PendingInput(kind="question",
                                                      question={"text": "Which?"}))
    payload = snapshot.to_dict()
    assert payload["pending_input"]["kind"] == "question"
    assert payload["pending_input"]["question"]["text"] == "Which?"
    assert payload["terminal"] is False


def test_dialog_snapshot_serialises_a_missing_run():
    payload = DialogSnapshot().to_dict()
    assert payload["run"] is None
    assert payload["handle"] is None
    assert payload["can_cancel"] is False


def test_dialog_step_result_embeds_a_result_object():
    class _Result:
        def to_dict(self):
            return {"run_id": "r", "status": "done"}

    step = DialogStepResult(run_id="r", thread_id="t", result=_Result())
    payload = step.to_dict()
    assert payload["result"]["status"] == "done"


def test_entity_details_put_the_prompt_under_one_name():
    """An assistant prompt is stored as ``text``; the DTO says ``prompt_text``."""
    card = AssistantCard(assistant_id="a1", name="Helper")
    detail = AssistantDetail(card=card, prompt_text="You are helpful")
    payload = detail.to_dict()
    assert payload["prompt_text"] == "You are helpful"
    assert "text" not in payload

    orch_card = OrchestratorCard(slug="dev_agent", name="DevAgent")
    orch = OrchestratorDetail(card=orch_card, prompt_text="Rules",
                              config={"max_steps": 10})
    assert orch.to_dict()["prompt_text"] == "Rules"
    assert orch.to_dict()["config"]["max_steps"] == 10


def test_health_report_exposes_readiness():
    assert HealthReport(status="ok").ready is True
    assert HealthReport(status="not_ready").ready is False
    payload = HealthReport(status="not_ready", missing=["table:threads"],
                           hint="start the platform").to_dict()
    assert payload["missing"] == ["table:threads"]


def test_search_page_round_trips():
    page = SearchPage(hits=[{"thread_id": "t1"}], total=1, query="needle")
    payload = page.to_dict()
    assert payload["query"] == "needle"
    assert payload["hits"][0]["thread_id"] == "t1"


def test_economy_options_survive_a_round_trip():
    options = EconomyOptions(mode=True, cache_enabled=True,
                             cache_multiplier=2, tail_messages=40, anchor=12)
    payload = options.to_dict()
    assert payload["mode"] is True
    assert payload["cache_multiplier"] == 2
    assert payload["tail_messages"] == 40
    assert payload["anchor"] == 12
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
