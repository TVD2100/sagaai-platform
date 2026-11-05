"""The published JSON of the HTTP API, pinned by a snapshot.

The routes no longer build a dict and hand it back: they return a response
model and FastAPI serialises it once. That refactor is only safe if the
bytes on the wire do not change, and "the bytes look right on my machine"
is not an argument. This module turns it into a check.

Two properties are proved separately, because they fail for different
reasons:

1. **The snapshot.** Every endpoint is called against a deterministic
   backend and the resulting JSON is compared with ``tests/golden/
   api_contract.json``. A new field, a renamed one or a changed TYPE fails
   here. Volatile values (pid, uptime, timestamps) are replaced by a type
   tag rather than dropped, so a field that changes from ``int`` to
   ``str`` is still caught.

2. **The serialisation.** ``jsonable_encoder(model)`` is asserted equal to
   ``model.model_dump()`` for every response model. This is the one
   assumption the refactor rests on: that letting FastAPI serialise the
   model yields exactly the dict an explicit ``.model_dump()`` used to
   yield. It is a property of the models, not of one route, so it is
   checked once over the whole model layer.

Regenerate the snapshot after an INTENTIONAL contract change::

    UPDATE_GOLDEN=1 pytest tests/test_api_golden.py

The regenerated file is reviewed in the diff - that review is the point.
"""

import json
import os

import pytest

pytest.importorskip("fastapi")

from fastapi.encoders import jsonable_encoder  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import api_token_store as store  # noqa: E402
from core.gateway.dto import (  # noqa: E402
    AssistantCard,
    AssistantDetail,
    CancelOutcome,
    DialogHandle,
    DialogStepResult,
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
from server import app as server_app  # noqa: E402
from server import auth, schemas  # noqa: E402
from server.routes import agents as route_agents  # noqa: E402
from server.routes import dialogs as route_dialogs  # noqa: E402
from server.routes import health as route_health  # noqa: E402
from server.routes import runs as route_runs  # noqa: E402
from server.routes import threads as route_threads  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_PATH = os.path.join(ROOT, "tests", "golden", "api_contract.json")

RUN_ID = "r_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4"
THREAD_ID = "20260101_120000_deadbeef"
ASSISTANT_ID = "6b0641b3"


# ─── Deterministic cards ────────────────────────────────────────────────────


def _employee() -> OrchestratorCard:
    return OrchestratorCard(
        slug="dev_agent", name="DevAgent", description="Software work",
        is_builtin=True, tools=["read_file", "run_test"], max_steps=60,
        auto_apply=True, created_at="2026-01-01", updated_at="2026-01-02",
    )


def _assistant() -> AssistantCard:
    return AssistantCard(
        assistant_id=ASSISTANT_ID, slug="korrektor", name="Корректор",
        description="Text fixes", service="openai", model="gpt-4o",
        temperature=0.2, tools=["read_file"], max_tool_calls=5,
        max_tokens=2000, reasoning_effort="low", created_at="2026-01-01",
    )


def _summary() -> ThreadSummary:
    return ThreadSummary(
        thread_id=THREAD_ID, title="Отчёт", agent_slug="dev_agent",
        agent_name="DevAgent", workspace="/tmp/project", target_file="",
        kind="dialog", created_at="2026-01-01", updated_at="2026-01-02",
        message_count=2,
    )


def _message(index: int, role: str, content: str) -> MessageView:
    return MessageView(index=index, role=role, content=content,
                       ts="2026-01-01T12:00:00+00:00")


def _running() -> RunSnapshot:
    return RunSnapshot(
        run_id=RUN_ID, thread_id=THREAD_ID, agent_slug="dev_agent",
        status="running", live=True, heartbeat_age_s=0.5, created_at=1767225600.0,
    )


def _parked() -> RunSnapshot:
    return RunSnapshot(
        run_id=RUN_ID, thread_id=THREAD_ID, agent_slug="dev_agent",
        status="awaiting_input", live=True, heartbeat_age_s=0.5,
        steps_used=2, created_at=1767225600.0,
        pending_input=PendingInput(
            kind="question",
            question={"text": "Which folder should I use?", "options": []},
        ),
    )


# ─── The backend every route reads from ─────────────────────────────────────


class ContractBackend:
    """Fixed answers, so the snapshot describes the CONTRACT and not a run."""

    def start(self, request, **kwargs):
        return DialogHandle(thread_id=THREAD_ID, run_id=RUN_ID,
                            agent_slug=request.agent_slug,
                            workspace=request.workspace, mode=request.mode,
                            created=True, status="running")

    def submit(self, run_id, message, **kwargs):
        return DialogStepResult(run_id=run_id, thread_id=THREAD_ID,
                                phase="run", final_status="running")

    def get(self, run_id):
        return _running()

    def wait(self, run_id, *, wait_seconds=0.0):
        return _running()

    def answer(self, run_id, text):
        return _running()

    def cancel(self, run_id, *, cascade=True):
        return CancelOutcome(run_id=run_id, cancelled=True,
                             previous_status="running", reason="requested",
                             children=[], marker_written=True)

    def all_runs(self, **kwargs):
        return [_running()]

    def page(self, **kwargs):
        return ThreadPage(items=[_summary()], total=1, offset=0, limit=50,
                          truncated=False)

    def window(self, thread_id, *, offset=0, limit=50, **kwargs):
        return ThreadWindow(
            thread=_summary(), offset=offset, limit=limit, total=2,
            remaining=0,
            messages=[_message(0, "user", "Сделай отчёт"),
                      _message(1, "assistant", "Отчёт готов")],
        )

    def search(self, query, **kwargs):
        hits = [{"thread_id": THREAD_ID, "message_index": 0,
                 "content": "Сделай отчёт"}]
        return SearchPage(hits=hits, total=1, query=query)


@pytest.fixture()
def backend(monkeypatch):
    engine = ContractBackend()
    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog", engine.start)
    monkeypatch.setattr(route_dialogs.gateway_dialog, "submit_message", engine.submit)
    monkeypatch.setattr(route_runs.gateway_runs, "get_run", engine.get)
    monkeypatch.setattr(route_runs.gateway_runs, "wait_run", engine.wait)
    monkeypatch.setattr(route_runs.gateway_runs, "answer_run", engine.answer)
    monkeypatch.setattr(route_runs.gateway_runs, "cancel_run", engine.cancel)
    monkeypatch.setattr(route_runs.gateway_runs, "list_runs", engine.all_runs)
    monkeypatch.setattr(route_threads.gateway_threads, "list_threads", engine.page)
    monkeypatch.setattr(route_threads.gateway_threads, "read_messages", engine.window)
    monkeypatch.setattr(route_threads.gateway_threads, "search_messages", engine.search)

    monkeypatch.setattr(route_agents.gateway_entities, "list_orchestrators",
                        lambda **kw: [_employee()])
    monkeypatch.setattr(route_agents.gateway_entities, "get_orchestrator",
                        lambda slug, **kw: OrchestratorDetail(
                            card=_employee(), prompt_text="You are DevAgent.",
                            config={"max_steps": 60}))
    monkeypatch.setattr(route_agents.gateway_entities, "list_assistants",
                        lambda: [_assistant()])
    monkeypatch.setattr(route_agents.gateway_entities, "get_assistant",
                        lambda aid, **kw: AssistantDetail(
                            card=_assistant(), prompt_text="Fix the text."))

    monkeypatch.setattr(route_health.gateway_health, "probe_schema",
                        lambda *a, **k: [])
    monkeypatch.setattr(store, "load_settings", lambda: {"api_enabled": True})
    route_health.gateway_health.reset_cache()
    return engine


@pytest.fixture()
def client(backend):
    return TestClient(server_app.create_app(), raise_server_exceptions=False)


@pytest.fixture()
def token(monkeypatch):
    generated = store.generate_token(slugs=["dev_agent"], note="golden")
    monkeypatch.setattr(store, "api_state", lambda: {
        "enabled": True, "available": True, "host": "127.0.0.1",
        "port": 8765, "allow_remote": False, "token_count": 1})
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


# ─── Normalisation ──────────────────────────────────────────────────────────

#: Values that differ between two correct runs. They are replaced by a TYPE
#: TAG, not removed: a field that changes type must still fail the snapshot.
VOLATILE = {
    "pid": "int",
    "uptime_seconds": "float",
    "created_at": "timestamp",
    "heartbeat_age_s": "float-or-null",
    "request_id": "request-id",
    "version": "version",
    "waited_ms": "int-or-null",
}


def _normalise(value):
    """Return *value* with volatile scalars replaced by a type tag."""
    if isinstance(value, dict):
        result = {}
        for key in sorted(value):
            if key in VOLATILE:
                seen = value[key]
                if seen is None:
                    result[key] = None
                else:
                    result[key] = "<" + VOLATILE[key] + ":" + type(seen).__name__ + ">"
            else:
                result[key] = _normalise(value[key])
        return result
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


# ─── The capture ────────────────────────────────────────────────────────────


def _capture(client, token) -> dict:
    """Call every endpoint once and return the normalised responses."""
    headers = _auth(token)
    snapshot = {}

    def record(label, response, expect):
        assert response.status_code == expect, (
            label, response.status_code, response.text[:400])
        snapshot[label] = {"status": response.status_code,
                           "body": _normalise(response.json())}

    record("health", client.get("/health"), 200)
    record("ready", client.get("/ready"), 200)
    record("version", client.get("/version"), 200)

    record("agents.list", client.get("/v1/agents", headers=headers), 200)
    record("agents.detail", client.get("/v1/agents/dev_agent?with_prompt=1",
                                       headers=headers), 200)
    record("assistants.list", client.get("/v1/assistants", headers=headers), 200)
    record("assistants.detail",
           client.get("/v1/assistants/" + ASSISTANT_ID, headers=headers), 200)

    launched = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent",
                                 "message": "Сделай отчёт",
                                 "workspace": "/tmp/project"},
                           headers=headers)
    record("dialogs.start", launched, 202)
    snapshot["dialogs.start"]["location"] = launched.headers.get("Location")

    record("dialogs.message",
           client.post("/v1/dialogs/" + RUN_ID + "/messages",
                       json={"message": "Продолжай"}, headers=headers), 202)

    record("runs.get", client.get("/v1/runs/" + RUN_ID, headers=headers), 200)
    record("runs.wait", client.get("/v1/runs/" + RUN_ID + "?wait=1",
                                   headers=headers), 200)
    record("runs.list", client.get("/v1/runs", headers=headers), 200)
    record("runs.cancel", client.post("/v1/runs/" + RUN_ID + "/cancel",
                                      headers=headers), 202)
    record("runs.answer", client.post("/v1/runs/" + RUN_ID + "/answer",
                                      json={"answer": "/tmp/project"},
                                      headers=headers), 202)

    record("threads.list", client.get("/v1/threads", headers=headers), 200)
    record("threads.read", client.get("/v1/threads/" + THREAD_ID,
                                      headers=headers), 200)
    record("search", client.get("/v1/search?query=отчёт", headers=headers), 200)

    record("errors.malformed_run", client.get("/v1/runs/not-a-run",
                                              headers=headers), 400)
    record("errors.unauthorized", client.get("/v1/agents"), 401)
    record("errors.unknown_path", client.get("/v1/nothing-here",
                                             headers=headers), 404)
    return snapshot


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_the_public_json_matches_the_golden_snapshot(client, token):
    """Every endpoint answers exactly the shape the snapshot recorded."""
    current = _capture(client, token)

    if os.environ.get("UPDATE_GOLDEN") == "1":  # pragma: no cover - manual
        os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
        with open(GOLDEN_PATH, "w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        pytest.skip("golden snapshot regenerated")

    assert os.path.exists(GOLDEN_PATH), (
        "no golden snapshot; run UPDATE_GOLDEN=1 pytest %s" % __file__)
    with open(GOLDEN_PATH, encoding="utf-8") as handle:
        expected = json.load(handle)

    assert sorted(current) == sorted(expected), "the endpoint set changed"
    changed = [label for label in sorted(expected)
               if current[label] != expected[label]]
    assert changed == [], (
        "the public JSON changed for: %s\nfirst difference: %s" % (
            changed,
            json.dumps({"expected": expected.get(changed[0]),
                        "actual": current.get(changed[0])},
                       ensure_ascii=False, indent=2)[:3000]))


#: The capture label of every data operation the schema documents. Kept
#: as a mapping rather than a count so a NEW route fails with the operation
#: that has no snapshot entry, not merely with a number.
COVERAGE = {
    "agents.list": ("/v1/agents", "get"),
    "agents.detail": ("/v1/agents/{slug}", "get"),
    "assistants.list": ("/v1/assistants", "get"),
    "assistants.detail": ("/v1/assistants/{assistant_id}", "get"),
    "dialogs.start": ("/v1/dialogs", "post"),
    "dialogs.message": ("/v1/dialogs/{run_id}/messages", "post"),
    "runs.get": ("/v1/runs/{run_id}", "get"),
    "runs.wait": ("/v1/runs/{run_id}", "get"),
    "runs.list": ("/v1/runs", "get"),
    "runs.cancel": ("/v1/runs/{run_id}/cancel", "post"),
    "runs.answer": ("/v1/runs/{run_id}/answer", "post"),
    "threads.list": ("/v1/threads", "get"),
    "threads.read": ("/v1/threads/{thread_id}", "get"),
    "search": ("/v1/search", "get"),
}

#: The three probes are open, so they carry no snapshot of their own shape
#: beyond the fields the capture records.
OPEN_OPERATIONS = {("/health", "get"), ("/ready", "get"),
                   ("/version", "get")}


def test_every_documented_data_route_has_a_snapshot_entry():
    """A route added without a snapshot entry would go unnoticed.

    The set is read from the generated OpenAPI document, not from the route
    table: the routers are included as sub-applications, so ``app.routes``
    lists five included routers and no operations at all. The schema is
    also the artefact a client generates from, which makes it the right
    definition of "a documented route".
    """
    spec = server_app.create_app().openapi()
    documented = {(path, method)
                  for path, operations in spec["paths"].items()
                  for method in operations}
    data_routes = documented - OPEN_OPERATIONS
    assert set(COVERAGE.values()) == data_routes, (
        "the snapshot is out of step with the schema",
        sorted(data_routes - set(COVERAGE.values())),
        sorted(set(COVERAGE.values()) - data_routes))
    assert documented, "the schema is empty"


#: Models a route may answer with. Listed explicitly so a NEW response model
#: is a conscious addition here and its serialisation is proved too.
RESPONSE_MODELS = [
    schemas.HealthOut, schemas.ReadyOut, schemas.VersionOut,
    schemas.AgentOut, schemas.AgentDetailOut, schemas.AgentListOut,
    schemas.AssistantOut, schemas.AssistantDetailOut, schemas.AssistantListOut,
    schemas.DialogAcceptedOut, schemas.MessageOut, schemas.PendingInputOut,
    schemas.RunOut, schemas.RunListOut, schemas.CancelOut,
    schemas.ThreadOut, schemas.ThreadListOut, schemas.ThreadWindowOut,
    schemas.SearchOut, schemas.ErrorEnvelope,
]


def _sample_for(model):
    """Build one populated instance of *model* from the fixture data."""
    built = {
        schemas.HealthOut: schemas.HealthOut(status="ok", pid=1,
                                             uptime_seconds=1.5,
                                             api_enabled=True),
        schemas.ReadyOut: schemas.ReadyOut(status="ok", version="1.0.0",
                                           checks={"db": "ready"},
                                           missing=[], hint=""),
        schemas.VersionOut: schemas.VersionOut(version="1.0.0", api_version=1),
        schemas.DialogAcceptedOut: schemas.DialogAcceptedOut(
            run_id=RUN_ID, thread_id=THREAD_ID,
            links=schemas.RunLinksOut(self="/v1/runs/" + RUN_ID,
                                      cancel="/v1/runs/" + RUN_ID + "/cancel")),
        schemas.MessageOut: schemas.message_out(_message(0, "user", "x")),
        schemas.PendingInputOut: schemas.PendingInputOut(
            kind="question", tool="", args={}, reasons=[], code="",
            question={"text": "Which folder?", "options": []}, plan=""),
        schemas.RunOut: schemas.run_out(_parked(), waited_ms=12),
        schemas.RunListOut: schemas.RunListOut(items=[schemas.run_out(_running())],
                                               total=1),
        schemas.CancelOut: schemas.CancelOut(run_id=RUN_ID, cancelled=True,
                                             previous_status="running",
                                             reason="requested", children=[],
                                             marker_written=True),
        schemas.ThreadOut: schemas.thread_out(_summary()),
        schemas.ThreadListOut: schemas.ThreadListOut(items=[schemas.thread_out(_summary())],
                                                     total=1),
        schemas.ThreadWindowOut: schemas.ThreadWindowOut(
            thread=schemas.thread_out(_summary()),
            messages=[schemas.message_out(_message(1, "assistant", "y"))],
            total=2),
        schemas.SearchOut: schemas.SearchOut(hits=[{"thread_id": THREAD_ID}],
                                             total=1, query="x"),
        schemas.AgentOut: schemas.agent_out(_employee()),
        schemas.AgentDetailOut: schemas.agent_detail_out(
            OrchestratorDetail(card=_employee(), prompt_text="p", config={"a": 1})),
        schemas.AgentListOut: schemas.AgentListOut(
            items=[schemas.agent_out(_employee())], total=1),
        schemas.AssistantOut: schemas.assistant_out(_assistant()),
        schemas.AssistantDetailOut: schemas.assistant_detail_out(
            AssistantDetail(card=_assistant(), prompt_text="p")),
        schemas.AssistantListOut: schemas.AssistantListOut(
            items=[schemas.assistant_out(_assistant())], total=1),
    }
    if model not in built:  # pragma: no cover - guards the list above
        raise AssertionError("no sample for %s" % model.__name__)
    return built[model]


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda m: m.__name__)
def test_serialising_a_model_equals_dumping_it(model):
    """The refactor rests on this identity; it is proved once for all models.

    Old route: ``Model(**payload).model_dump()`` returned to FastAPI, which
    encoded that dict. New route: the model itself, encoded by FastAPI.
    The two outputs are the same exactly when the assertion below holds.
    """
    sample = _sample_for(model) if model is not schemas.ErrorEnvelope else None
    if model is schemas.ErrorEnvelope:
        sample = model(error="x", error_code="invalid_request", ok=False,
                       request_id="req_1", retryable=True, field="run_id")
    assert jsonable_encoder(sample) == sample.model_dump(), model.__name__


def test_the_error_envelope_has_one_shape_everywhere(client, token):
    """A client parses one error shape, whichever handler produced it."""
    headers = _auth(token)
    bodies = [
        client.get("/v1/runs/not-a-run", headers=headers).json(),
        client.get("/v1/agents").json(),
        client.get("/v1/nothing-here", headers=headers).json(),
    ]
    required = {"ok", "error", "error_code"}
    for body in bodies:
        assert required.issubset(body), body
        assert body["ok"] is False
        assert isinstance(body["error_code"], str) and body["error_code"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
