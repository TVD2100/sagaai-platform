"""Tests of the bearer authentication of the HTTP API.

The token store is mocked at the settings boundary (``api_state`` and
``load_records``), which is exactly where the real code reads it. Everything
downstream - hashing, constant-time comparison, expiry, scope - runs for
real, so these tests cover the actual verification path, not a stand-in.

What is checked here and nowhere else:

- every data route refuses an unauthenticated call, including the ones that
  would otherwise fail validation first;
- a refusal carries the bearer challenge a client needs to recover;
- the scope of the token reaches the entity listing unchanged;
- an API that is switched off fails CLOSED with ``503``, never with a pass.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from core import api_token_store as store  # noqa: E402
from core.gateway import entities as gateway_entities  # noqa: E402
from server import app as server_app  # noqa: E402
from server import auth, middleware  # noqa: E402

#: A syntactically valid run id, for routes that validate the path first.
RUN_ID = "r_" + "c" * 28


# ─── Fixtures ───────────────────────────────────────────────────────────────


def _state(enabled: bool, available: bool) -> dict:
    return {"enabled": enabled, "available": available, "host": "127.0.0.1",
            "port": 8765, "allow_remote": False, "token_count": 1}


@pytest.fixture()
def client():
    return TestClient(server_app.create_app())


@pytest.fixture()
def api(monkeypatch):
    """A switched-on API with exactly one token scoped to ``dev_agent``."""
    generated = store.generate_token(slugs=["dev_agent"], note="tests")
    monkeypatch.setattr(store, "api_state", lambda: _state(True, True))
    monkeypatch.setattr(store, "load_records", lambda: [generated["record"]])
    auth.reset_cache()
    try:
        yield {"token": generated["token"], "record": generated["record"]}
    finally:
        auth.reset_cache()


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


# ─── The guard on every data route ─────────────────────────────────────────


DATA_CALLS = [
    ("get", "/v1/agents", None),
    ("get", "/v1/agents/dev_agent", None),
    ("get", "/v1/assistants", None),
    ("get", "/v1/assistants/a1", None),
    ("post", "/v1/dialogs", {"agent_slug": "dev_agent", "message": "hi"}),
    ("post", "/v1/dialogs/%s/messages" % RUN_ID, {"message": "hi"}),
    ("get", "/v1/runs/%s" % RUN_ID, None),
    ("get", "/v1/runs", None),
    ("post", "/v1/runs/%s/cancel" % RUN_ID, {"cascade": True}),
    ("post", "/v1/runs/%s/answer" % RUN_ID, {"answer": "yes"}),
    ("get", "/v1/threads", None),
    ("get", "/v1/threads/t1", None),
    ("get", "/v1/search?query=x", None),
]


@pytest.mark.parametrize("method,path,body", DATA_CALLS)
def test_every_data_route_refuses_a_call_without_a_token(client, api, method,
                                                        path, body):
    response = getattr(client, method)(path, json=body) if body is not None \
        else getattr(client, method)(path)
    assert response.status_code == 401, response.text
    payload = response.json()
    assert payload["error_code"] == "unauthorized"
    assert payload["ok"] is False


def test_a_refusal_carries_the_bearer_challenge(client, api):
    response = client.get("/v1/agents")
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_the_health_routes_need_no_token(client):
    assert client.get("/health").status_code == 200
    assert client.get("/version").status_code == 200


# ─── Token verification ─────────────────────────────────────────────────────


def test_a_valid_token_is_accepted(client, api, monkeypatch):
    monkeypatch.setattr(gateway_entities, "list_orchestrators",
                        lambda **kwargs: [])
    response = client.get("/v1/agents", headers=_auth(api["token"]))
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_an_unknown_token_is_refused_as_invalid(client, api):
    other = store.generate_token()["token"]
    response = client.get("/v1/agents", headers=_auth(other))
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_token"


def test_a_token_without_the_prefix_is_refused(client, api):
    """A bearer value that is not a SagaAI token is an invalid token.

    The distinction matters to a client: ``unauthorized`` means "send a
    header", ``invalid_token`` means "this value will never work". Both are
    401, but only the second tells the caller to stop retrying.
    """
    response = client.get("/v1/agents", headers=_auth("just-some-string"))
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_token"


def test_a_basic_header_is_not_accepted(client, api):
    response = client.get("/v1/agents", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"


def test_a_bare_token_is_not_accepted(client, api):
    response = client.get("/v1/agents", headers={"Authorization": "saga_values"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"


def test_a_token_in_the_query_string_is_ignored(client, api):
    response = client.get("/v1/agents?token=" + api["token"])
    assert response.status_code == 401


def test_an_expired_token_is_refused(client, monkeypatch):
    generated = store.generate_token(slugs=["dev_agent"],
                                     expires_at="2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(store, "api_state", lambda: _state(True, True))
    monkeypatch.setattr(store, "load_records", lambda: [generated["record"]])
    auth.reset_cache()
    try:
        response = client.get("/v1/agents", headers=_auth(generated["token"]))
        assert response.status_code == 401
        assert response.json()["error_code"] == "token_expired"
    finally:
        auth.reset_cache()


# ─── Fail closed ────────────────────────────────────────────────────────────


def test_a_switched_off_api_answers_503_not_401(client, monkeypatch):
    monkeypatch.setattr(store, "api_state", lambda: _state(False, False))
    monkeypatch.setattr(store, "load_records", lambda: [])
    auth.reset_cache()
    try:
        response = client.get("/v1/agents", headers=_auth("saga_whatever"))
        assert response.status_code == 503
        assert response.json()["error_code"] == "api_disabled"
    finally:
        auth.reset_cache()


def test_an_enabled_api_without_a_token_answers_503(client, monkeypatch):
    monkeypatch.setattr(store, "api_state", lambda: _state(True, False))
    monkeypatch.setattr(store, "load_records", lambda: [])
    auth.reset_cache()
    try:
        response = client.get("/v1/agents", headers=_auth("saga_whatever"))
        assert response.status_code == 503
        assert response.json()["error_code"] == "api_not_configured"
    finally:
        auth.reset_cache()


# ─── Scope ──────────────────────────────────────────────────────────────────


def test_the_token_scope_reaches_the_entity_listing(client, api, monkeypatch):
    seen = {}

    def fake_list(*, allowed_slugs=None):
        seen["slugs"] = allowed_slugs
        return []

    monkeypatch.setattr(gateway_entities, "list_orchestrators", fake_list)
    response = client.get("/v1/agents", headers=_auth(api["token"]))
    assert response.status_code == 200
    assert seen["slugs"] == ["dev_agent"]


def test_authenticate_returns_the_scope_and_never_the_token(monkeypatch):
    generated = store.generate_token(slugs=["dev_agent", "teacher_assistant"])
    monkeypatch.setattr(store, "api_state", lambda: _state(True, True))
    monkeypatch.setattr(store, "load_records", lambda: [generated["record"]])
    auth.reset_cache()
    try:
        identity = auth.authenticate(token=generated["token"])
        assert identity["slugs"] == ["dev_agent", "teacher_assistant"]
        assert identity["token_id"] == generated["record"]["id"]
        assert generated["token"] not in str(identity)
    finally:
        auth.reset_cache()


# ─── Throttling ─────────────────────────────────────────────────────────────


def test_repeated_failures_are_throttled(client, api):
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        response = client.get("/v1/agents", headers=_auth("saga_wrong"))
        assert response.status_code == 401
    blocked = client.get("/v1/agents", headers=_auth("saga_wrong"))
    assert blocked.status_code == 429
    assert blocked.json()["error_code"] == "too_many_attempts"


def test_a_success_clears_the_failure_counter(client, api):
    for _ in range(auth.MAX_FAILED_ATTEMPTS - 1):
        client.get("/v1/agents", headers=_auth("saga_wrong"))
    ok = client.get("/v1/agents", headers=_auth(api["token"]))
    assert ok.status_code == 200
    again = client.get("/v1/agents", headers=_auth("saga_wrong"))
    assert again.status_code == 401


# ─── Redaction ──────────────────────────────────────────────────────────────


def test_scrub_removes_a_bearer_value():
    cleaned = middleware.scrub("Authorization: Bearer saga_abcdefghijklmnopqrstuvwxyz")
    assert "saga_abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "<redacted>" in cleaned


def test_scrub_removes_a_bare_token_value():
    cleaned = middleware.scrub("token=saga_abcdefghijklmnopqrstuvwxyz1234 done")
    assert "saga_abcdefghijklmnopqrstuvwxyz1234" not in cleaned
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
