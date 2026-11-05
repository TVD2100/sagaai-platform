"""Tests of the run routes.

A run is the only object of the API whose state changes without a request,
so the tests check the three things a client depends on: a terminal status
can be read repeatedly, a run that belongs to another process is reported as
a FACT rather than as an error, and a cancellation is accepted
asynchronously with ``202``.

``core.gateway.runs`` is replaced per test: these routes are a translation
layer between HTTP and the store, and the translation is what is under test.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from core import api_token_store as store  # noqa: E402
from core.gateway import errors as gateway_errors  # noqa: E402
from core.gateway.dto import PendingInput, RunSnapshot  # noqa: E402
from server import app as server_app  # noqa: E402
from server import auth  # noqa: E402
from server.routes import runs as route_runs  # noqa: E402

RUN_ID = "r_" + "f" * 28


# ─── Fixtures ───────────────────────────────────────────────────────────────


def _state() -> dict:
    return {"enabled": True, "available": True, "host": "127.0.0.1",
            "port": 8765, "allow_remote": False, "token_count": 1}


@pytest.fixture()
def client():
    return TestClient(server_app.create_app())


@pytest.fixture()
def token(monkeypatch):
    generated = store.generate_token(slugs=["dev_agent"])
    monkeypatch.setattr(store, "api_state", _state)
    monkeypatch.setattr(store, "load_records", lambda: [generated["record"]])
    auth.reset_cache()
    try:
        yield generated["token"]
    finally:
        auth.reset_cache()


@pytest.fixture(autouse=True)
def _restore_slots():
    yield
    while server_app.STATE.active_runs > 0:
        server_app.STATE.release()
    server_app._ACTIVE_RUN_IDS.clear()


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def _snapshot(**kwargs) -> RunSnapshot:
    base = {"run_id": RUN_ID, "thread_id": "t1", "agent_slug": "dev_agent",
            "status": "running", "live": True, "created_at": 1.0}
    base.update(kwargs)
    return RunSnapshot(**base)


# ─── Status ─────────────────────────────────────────────────────────────────


def test_a_running_run_is_reported_with_its_state(client, token, monkeypatch):
    monkeypatch.setattr(route_runs.gateway_runs, "get_run",
                        lambda rid: _snapshot(steps_used=3, summary="working"))
    response = client.get("/v1/runs/%s" % RUN_ID, headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["steps_used"] == 3
    assert body["terminal"] is False
    assert body["live"] is True


def test_a_terminal_run_is_marked_terminal(client, token, monkeypatch):
    monkeypatch.setattr(route_runs.gateway_runs, "get_run",
                        lambda rid: _snapshot(status="done", summary="finished"))
    body = client.get("/v1/runs/%s" % RUN_ID, headers=_auth(token)).json()
    assert body["status"] == "done"
    assert body["terminal"] is True
    assert body["summary"] == "finished"


def test_a_run_of_another_process_is_a_fact_not_an_error(client, token,
                                                        monkeypatch):
    """``live: false`` with a fresh heartbeat is a normal answer."""
    monkeypatch.setattr(route_runs.gateway_runs, "get_run",
                        lambda rid: _snapshot(live=False, heartbeat_age_s=2.5,
                                              stale_reason="other_process"))
    response = client.get("/v1/runs/%s" % RUN_ID, headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["live"] is False
    assert body["heartbeat_age_s"] == 2.5
    assert body["stale_reason"] == "other_process"


def test_an_unknown_run_answers_404(client, token, monkeypatch):
    def _missing(rid):
        raise gateway_errors.run_not_found(rid)

    monkeypatch.setattr(route_runs.gateway_runs, "get_run", _missing)
    response = client.get("/v1/runs/%s" % RUN_ID, headers=_auth(token))
    assert response.status_code == 404
    assert response.json()["error_code"] == "run_not_found"


def test_a_malformed_run_id_answers_400(client, token):
    response = client.get("/v1/runs/nope", headers=_auth(token))
    assert response.status_code == 400
    assert response.json()["field"] == "run_id"


def test_pending_input_travels_with_the_status(client, token, monkeypatch):
    pending = PendingInput(kind="question", question={"text": "Which folder?"})
    monkeypatch.setattr(route_runs.gateway_runs, "get_run",
                        lambda rid: _snapshot(status="awaiting_input",
                                              pending_input=pending))
    body = client.get("/v1/runs/%s" % RUN_ID, headers=_auth(token)).json()
    assert body["pending_input"]["kind"] == "question"
    assert body["pending_input"]["question"]["text"] == "Which folder?"


def test_a_wait_request_reports_how_long_it_waited(client, token, monkeypatch):
    monkeypatch.setattr(route_runs.gateway_runs, "wait_run",
                        lambda rid, wait_seconds=0.0: _snapshot())
    body = client.get("/v1/runs/%s?wait=1" % RUN_ID,
                      headers=_auth(token)).json()
    assert body["waited_ms"] is not None
    assert body["waited_ms"] >= 0


def test_a_plain_read_carries_no_wait_time(client, token, monkeypatch):
    monkeypatch.setattr(route_runs.gateway_runs, "get_run",
                        lambda rid: _snapshot())
    body = client.get("/v1/runs/%s" % RUN_ID, headers=_auth(token)).json()
    assert body["waited_ms"] is None


def test_reading_a_terminal_run_does_not_release_a_slot_twice(client, token,
                                                            monkeypatch):
    """The listener releases the slot; a read must not push the counter up."""
    server_app.note_run(RUN_ID)
    monkeypatch.setattr(route_runs.gateway_runs, "get_run",
                        lambda rid: _snapshot(status="done"))
    client.get("/v1/runs/%s" % RUN_ID, headers=_auth(token))
    assert server_app.is_active_run(RUN_ID) is False
    assert server_app.STATE.active_runs == 0


# ─── Listing ────────────────────────────────────────────────────────────────


def test_the_list_route_maps_every_row(client, token, monkeypatch):
    monkeypatch.setattr(route_runs.gateway_runs, "list_runs",
                        lambda **kwargs: [_snapshot(), _snapshot(run_id="r_" + "1" * 28,
                                                               status="done")])
    body = client.get("/v1/runs", headers=_auth(token)).json()
    assert body["total"] == 2
    assert body["items"][1]["status"] == "done"


def test_the_list_route_passes_the_filters(client, token, monkeypatch):
    seen = {}

    def _list(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(route_runs.gateway_runs, "list_runs", _list)
    client.get("/v1/runs?thread_id=t9&root=%s&limit=5" % RUN_ID,
               headers=_auth(token))
    assert seen["thread_id"] == "t9"
    assert seen["root_run_id"] == RUN_ID
    assert seen["limit"] == 5


def test_a_limit_above_the_ceiling_is_refused(client, token):
    response = client.get("/v1/runs?limit=9999", headers=_auth(token))
    assert response.status_code == 400


# ─── Cancel ─────────────────────────────────────────────────────────────────


def test_a_cancel_is_accepted_with_202(client, token, monkeypatch):
    monkeypatch.setattr(route_runs.gateway_runs, "cancel_run",
                        lambda rid, cascade=True: {
                            "run_id": rid, "cancelled": True,
                            "previous_status": "running", "reason": "requested",
                            "children": [], "marker_written": True})
    response = client.post("/v1/runs/%s/cancel" % RUN_ID, headers=_auth(token))
    assert response.status_code == 202
    body = response.json()
    assert body["cancelled"] is True
    assert body["marker_written"] is True


def test_a_cancel_body_is_optional(client, token, monkeypatch):
    seen = {}

    def _cancel(rid, cascade=True):
        seen["cascade"] = cascade
        return {"run_id": rid, "cancelled": True}

    monkeypatch.setattr(route_runs.gateway_runs, "cancel_run", _cancel)
    response = client.post("/v1/runs/%s/cancel" % RUN_ID, headers=_auth(token))
    assert response.status_code == 202
    assert seen["cascade"] is True


def test_cascade_false_is_forwarded(client, token, monkeypatch):
    seen = {}

    def _cancel(rid, cascade=True):
        seen["cascade"] = cascade
        return {"run_id": rid, "cancelled": True}

    monkeypatch.setattr(route_runs.gateway_runs, "cancel_run", _cancel)
    client.post("/v1/runs/%s/cancel" % RUN_ID, json={"cascade": False},
                headers=_auth(token))
    assert seen["cascade"] is False


def test_cancelling_an_unknown_run_answers_404(client, token, monkeypatch):
    def _missing(rid, cascade=True):
        raise gateway_errors.run_not_found(rid)

    monkeypatch.setattr(route_runs.gateway_runs, "cancel_run", _missing)
    response = client.post("/v1/runs/%s/cancel" % RUN_ID, headers=_auth(token))
    assert response.status_code == 404


def test_a_broken_cancel_is_reported_as_an_internal_error(client, token,
                                                        monkeypatch):
    def _broken(rid, cascade=True):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(route_runs.gateway_runs, "cancel_run", _broken)
    response = client.post("/v1/runs/%s/cancel" % RUN_ID, headers=_auth(token))
    assert response.status_code == 500
    assert response.json()["error_code"] == "internal_error"
    assert "store exploded" not in response.text


# ─── Answer ─────────────────────────────────────────────────────────────────


def test_an_answer_is_delivered_and_the_state_is_returned(client, token,
                                                         monkeypatch):
    calls = []
    monkeypatch.setattr(route_runs.gateway_runs, "answer_run",
                        lambda rid, text: calls.append((rid, text))
                        or _snapshot(status="running"))
    response = client.post("/v1/runs/%s/answer" % RUN_ID,
                           json={"answer": "the first one"},
                           headers=_auth(token))
    assert response.status_code == 202
    assert calls == [(RUN_ID, "the first one")]
    assert response.json()["status"] == "running"


def test_a_blank_answer_is_refused(client, token):
    response = client.post("/v1/runs/%s/answer" % RUN_ID,
                           json={"answer": "  "}, headers=_auth(token))
    assert response.status_code == 400


def test_answering_a_run_that_is_not_waiting_is_refused(client, token,
                                                       monkeypatch):
    def _refuse(rid, text):
        raise gateway_errors.GatewayError(
            "nothing_to_answer", "The run is not waiting for an answer")

    monkeypatch.setattr(route_runs.gateway_runs, "answer_run", _refuse)
    response = client.post("/v1/runs/%s/answer" % RUN_ID,
                           json={"answer": "x"}, headers=_auth(token))
    assert response.status_code == 409
    assert response.json()["error_code"] == "nothing_to_answer"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
