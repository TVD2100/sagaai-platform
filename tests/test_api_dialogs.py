"""Tests of the dialog routes.

The launch endpoint is the one place where a client can get a run id it
cannot use, so the tests walk the whole admission sequence: a refused agent
never creates a thread, an exhausted pool answers ``429`` and not a silent
queue, a schema that is not ready answers ``503`` before anything is
written, and an accepted request answers ``202`` with a ``Location``.

``gateway_dialog`` is replaced as a module: the launch path is about the
ORDER of the checks and the shape of the answer, not about what a model
does afterwards.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from core import api_token_store as store  # noqa: E402
from core.gateway import errors as gateway_errors  # noqa: E402
from core.gateway import health as gateway_health  # noqa: E402
from core.gateway.dto import DialogHandle  # noqa: E402
from server import app as server_app  # noqa: E402
from server import auth  # noqa: E402
from server.routes import dialogs as route_dialogs  # noqa: E402

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
    """Return every admission slot the test took."""
    yield
    while server_app.STATE.active_runs > 0:
        server_app.STATE.release()
    server_app._ACTIVE_RUN_IDS.clear()


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


@pytest.fixture()
def ready(monkeypatch):
    monkeypatch.setattr(gateway_health, "probe_schema", lambda *a, **k: [])
    gateway_health._PROBE_STATE.update({"ready": True, "checked_at": 0.0})


@pytest.fixture()
def not_ready(monkeypatch):
    monkeypatch.setattr(gateway_health, "probe_schema",
                        lambda *a, **k: ["table:threads"])
    gateway_health._PROBE_STATE.update({"ready": False, "checked_at": 0.0})


def _handle(run_id="r_" + "d" * 28, thread_id="20261104_120000_abcd1234") -> DialogHandle:
    return DialogHandle(thread_id=thread_id, run_id=run_id,
                        agent_slug="dev_agent", workspace="/tmp/project",
                        mode="auto", created=True, status="running")


# ─── Body validation ────────────────────────────────────────────────────────


def test_a_body_without_a_message_answers_400(client, token):
    response = client.post("/v1/dialogs", json={"agent_slug": "dev_agent"},
                           headers=_auth(token))
    assert response.status_code == 400
    payload = response.json()
    assert payload["error_code"] == "invalid_request"
    # Error details sit at the top level of the envelope, not under a nested
    # key: a client reads ``payload["issues"]`` directly.
    assert payload["issues"]


def test_the_validation_answer_never_echoes_the_input(client, token):
    """FastAPI echoes the offending value by default; a token would leak."""
    response = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent",
                                 "message": "x",
                                 "deadline_seconds": "saga_secret_value"},
                           headers=_auth(token))
    assert response.status_code == 400
    assert "saga_secret_value" not in response.text


def test_an_unknown_mode_is_refused_by_the_gateway(client, token, ready):
    response = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent", "message": "x",
                                 "mode": "later"},
                           headers=_auth(token))
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"


def test_a_relative_workspace_is_refused(client, token, ready):
    response = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent", "message": "x",
                                 "workspace": "project/sub"},
                           headers=_auth(token))
    assert response.status_code == 400


# ─── Launch ─────────────────────────────────────────────────────────────────


def test_a_valid_launch_answers_202_with_a_location(client, token, ready,
                                                    monkeypatch):
    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog",
                        lambda *a, **k: _handle())
    response = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent", "message": "do it"},
                           headers=_auth(token))
    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] == "r_" + "d" * 28
    assert body["thread_id"] == "20261104_120000_abcd1234"
    assert body["poll_after_ms"] == route_dialogs.POLL_AFTER_MS
    assert body["links"]["self"] == "/v1/runs/" + body["run_id"]
    assert response.headers["Location"] == "/v1/runs/" + body["run_id"]


def test_a_launch_takes_exactly_one_admission_slot(client, token, ready,
                                                   monkeypatch):
    before = server_app.STATE.active_runs
    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog",
                        lambda *a, **k: _handle())
    client.post("/v1/dialogs", json={"agent_slug": "d_agent", "message": "x"},
                headers=_auth(token))
    assert server_app.STATE.active_runs == before + 1


def test_a_refused_launch_returns_its_slot(client, token, ready, monkeypatch):
    """A refusal must not leak the slot it took before the check."""
    before = server_app.STATE.active_runs

    def _explode(*args, **kwargs):
        raise gateway_errors.invalid_request("nope")

    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog", _explode)
    response = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent", "message": "x"},
                           headers=_auth(token))
    assert response.status_code == 400
    assert server_app.STATE.active_runs == before


def test_an_exhausted_pool_answers_429(client, token, ready):
    taken = 0
    try:
        while server_app.STATE.try_admit():
            taken += 1
        response = client.post("/v1/dialogs",
                               json={"agent_slug": "dev_agent", "message": "x"},
                               headers=_auth(token))
        assert response.status_code == 429
        payload = response.json()
        assert payload["error_code"] == "too_many_runs"
        assert payload["retryable"] is True
        assert payload["limit"] == server_app.server_app_limit()
    finally:
        for _ in range(taken):
            server_app.STATE.release()


def test_a_schema_that_is_not_ready_answers_503_before_creating(client, token,
                                                               not_ready,
                                                               monkeypatch):
    called = []
    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog",
                        lambda *a, **k: called.append(1) or _handle())
    response = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent", "message": "x"},
                           headers=_auth(token))
    assert response.status_code == 503
    assert response.json()["error_code"] == "db_not_ready"
    assert called == []


def test_the_token_scope_reaches_the_launch(client, token, ready, monkeypatch):
    seen = {}

    def _start(req, **kwargs):
        seen["req"] = req
        seen["slugs"] = kwargs.get("allowed_slugs")
        return _handle()

    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog", _start)
    client.post("/v1/dialogs",
                json={"agent_slug": "dev_agent", "message": "hello",
                      "web_search": True, "max_steps": 7},
                headers=_auth(token))
    assert seen["slugs"] == ["dev_agent"]
    assert seen["req"].message == "hello"
    assert seen["req"].web_search is True
    assert seen["req"].max_steps == 7


def test_auto_apply_defaults_to_none_so_the_card_decides(client, token, ready,
                                                        monkeypatch):
    seen = {}
    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog",
                        lambda req, **k: seen.update({"req": req}) or _handle())
    client.post("/v1/dialogs",
                json={"agent_slug": "dev_agent", "message": "x"},
                headers=_auth(token))
    assert seen["req"].auto_apply is None


def test_an_explicit_auto_apply_is_kept(client, token, ready, monkeypatch):
    seen = {}
    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog",
                        lambda req, **k: seen.update({"req": req}) or _handle())
    client.post("/v1/dialogs",
                json={"agent_slug": "dev_agent", "message": "x",
                      "auto_apply": False},
                headers=_auth(token))
    assert seen["req"].auto_apply is False


# ─── Follow-up message ──────────────────────────────────────────────────────


def test_a_message_is_appended_and_answers_202(client, token, ready,
                                              monkeypatch):
    run_id = "r_" + "e" * 28

    class _Result:
        thread_id = "t1"

    seen = {}

    def _submit(rid, text, **kwargs):
        seen["rid"] = rid
        seen["text"] = text
        return _Result()

    monkeypatch.setattr(route_dialogs.gateway_dialog, "submit_message", _submit)
    response = client.post("/v1/dialogs/%s/messages" % run_id,
                           json={"message": "next"}, headers=_auth(token))
    assert response.status_code == 202
    assert seen == {"rid": run_id, "text": "next"}
    assert response.headers["Location"] == "/v1/runs/" + run_id


def test_a_blank_message_is_refused(client, token):
    run_id = "r_" + "e" * 28
    response = client.post("/v1/dialogs/%s/messages" % run_id,
                           json={"message": "   "}, headers=_auth(token))
    assert response.status_code == 400


def test_a_malformed_run_id_is_refused_before_the_gateway(client, token):
    response = client.post("/v1/dialogs/not-a-run/messages",
                           json={"message": "x"}, headers=_auth(token))
    assert response.status_code == 400
    assert response.json()["field"] == "run_id"


def test_an_unknown_run_answers_404(client, token, ready, monkeypatch):
    run_id = "r_" + "e" * 28

    def _missing(*args, **kwargs):
        raise gateway_errors.run_not_found(run_id)

    monkeypatch.setattr(route_dialogs.gateway_dialog, "submit_message", _missing)
    response = client.post("/v1/dialogs/%s/messages" % run_id,
                           json={"message": "x"}, headers=_auth(token))
    assert response.status_code == 404
    assert response.json()["error_code"] == "run_not_found"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
