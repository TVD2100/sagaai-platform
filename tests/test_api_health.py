"""Tests of the HTTP health probes and of the run bookkeeping.

The probes are the only unauthenticated surface of the API, so the tests
check both what they answer and what they refuse to answer: a liveness probe
must not depend on the database, and a readiness probe must not list the
entities of the installation.

The application is built WITHOUT its lifespan: the lifespan takes the real
instance lock and runs the cold start of the platform, neither of which a
unit test should do. That is why ``create_app`` keeps the lock out of the
constructor and puts it in the lifespan.
"""

import os
import sys

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from core.gateway import health as gateway_health  # noqa: E402
from server import app as server_app  # noqa: E402


@pytest.fixture()
def client():
    """Return a test client of the application without running its lifespan."""
    application = server_app.create_app()
    return TestClient(application)


# ─── /health ────────────────────────────────────────────────────────────────


def test_health_answers_without_a_token(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["pid"] == os.getpid()
    assert isinstance(body["uptime_seconds"], float)


def test_health_does_not_report_the_database_state(client):
    """A liveness probe must never become a schema report."""
    body = client.get("/health").json()
    assert "missing" not in body
    assert "checks" not in body


def test_health_carries_a_request_id_header(client):
    response = client.get("/health")
    assert response.headers.get("X-Request-Id", "").startswith("req_")


def test_a_supplied_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-Id": "trace-42"})
    assert response.headers["X-Request-Id"] == "trace-42"


def test_a_malformed_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-Id": "bad id\nwith newline"})
    assert response.headers["X-Request-Id"].startswith("req_")


# ─── /version ───────────────────────────────────────────────────────────────


def test_version_reports_the_contract_version(client):
    body = client.get("/version").json()
    assert body["api_version"] == server_app.API_VERSION
    assert body["version"] == gateway_health.version()


# ─── /ready ─────────────────────────────────────────────────────────────────


def test_ready_answers_200_when_the_schema_is_in_place(client, monkeypatch):
    monkeypatch.setattr(gateway_health, "probe_schema", lambda *a, **k: [])
    gateway_health._PROBE_STATE.update({"ready": False, "checked_at": 0.0})
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["missing"] == []


def test_ready_answers_503_and_names_what_is_missing(client, monkeypatch):
    monkeypatch.setattr(gateway_health, "probe_schema",
                        lambda *a, **k: ["table:threads"])
    gateway_health._PROBE_STATE.update({"ready": False, "checked_at": 0.0})
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["missing"] == ["table:threads"]
    assert body["hint"]


def test_ready_carries_the_version_for_the_operator(client, monkeypatch):
    monkeypatch.setattr(gateway_health, "probe_schema", lambda *a, **k: [])
    gateway_health._PROBE_STATE.update({"ready": False, "checked_at": 0.0})
    body = client.get("/ready").json()
    assert body["version"] == gateway_health.version()


# ─── Unknown paths and methods ──────────────────────────────────────────────


def test_an_unknown_path_answers_in_the_error_envelope(client):
    response = client.get("/v1/nowhere")
    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "not_found"
    assert body["request_id"].startswith("req_")


def test_a_wrong_method_answers_405_in_the_envelope(client):
    response = client.post("/health")
    assert response.status_code == 405
    assert response.json()["error_code"] == "method_not_allowed"


# ─── Admission bookkeeping ──────────────────────────────────────────────────


def test_an_unknown_run_does_not_hold_a_slot():
    assert server_app.is_active_run("r_" + "0" * 28) is False


def test_a_noted_run_holds_a_slot_until_it_is_forgotten():
    run_id = "r_" + "a" * 28
    server_app.note_run(run_id)
    try:
        assert server_app.is_active_run(run_id) is True
    finally:
        server_app.forget_run(run_id)
    assert server_app.is_active_run(run_id) is False


def test_noting_the_same_run_twice_keeps_one_entry():
    run_id = "r_" + "b" * 28
    server_app.note_run(run_id)
    server_app.note_run(run_id)
    try:
        assert server_app.is_active_run(run_id) is True
    finally:
        server_app.forget_run(run_id)


def test_the_limit_is_a_positive_number():
    assert server_app.server_app_limit() >= 1
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
