"""End-to-end scenarios of the HTTP API.

Each scenario walks the API the way a client does: a request, a status read,
a decision on what came back, the next request. The gateway is replaced by
an in-memory backend that behaves like a run really behaves - it starts
running, parks on a question, resumes, can be cancelled and ends terminal -
so the scenario exercises the CONTRACT between the routes rather than one
route in isolation.

Given/when/then, one scenario per user-visible behaviour:

1. a client without a token is refused, and gets in with one;
2. a full dialog: launch, poll, answer a question, read the result;
3. a cancellation mid-flight, then a terminal status that stays terminal;
4. reading the transcript and searching it;
5. the failure path: an unknown run, a malformed id, an exhausted pool.
"""

import uuid

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from core import api_token_store as store  # noqa: E402
from core.gateway import errors as gateway_errors  # noqa: E402
from core.gateway.dto import (  # noqa: E402
    DialogHandle,
    MessageView,
    PendingInput,
    RunSnapshot,
    SearchPage,
    ThreadSummary,
    ThreadWindow,
)
from server import app as server_app  # noqa: E402
from server import auth  # noqa: E402
from server.routes import dialogs as route_dialogs  # noqa: E402
from server.routes import runs as route_runs  # noqa: E402
from server.routes import threads as route_threads  # noqa: E402

# ─── The fake backend ───────────────────────────────────────────────────────


class FakeBackend:
    """A run store that behaves like the real one, in memory.

    Only the states a client can observe are modelled. The point is that a
    scenario can move a run from ``running`` to ``awaiting_input`` to
    ``cancelled`` and every route sees the change, exactly as it would with
    a real driver.
    """

    def __init__(self) -> None:
        self.runs = {}
        self.messages = {}
        self.titles = {}

    # ── Launch ─────────────────────────────────────────────────────────

    def start(self, request, **kwargs):
        # The real ``create_dialog`` validates the request before it touches
        # the thread store; the fake does the same so a scenario cannot get
        # a thread for a request the platform would refuse.
        problem = request.validate()
        if problem:
            raise gateway_errors.invalid_request(problem)
        run_id = "r_" + uuid.uuid4().hex[:28]
        thread_id = "20261104_200000_" + uuid.uuid4().hex[:8]
        self.runs[run_id] = {
            "run_id": run_id, "thread_id": thread_id,
            "agent_slug": request.agent_slug, "status": "running",
            "live": True, "steps_used": 0, "created_at": 1.0,
        }
        self.messages[thread_id] = [
            {"role": "user", "content": request.message, "index": 0},
        ]
        self.titles[thread_id] = request.title or request.message[:40]
        return DialogHandle(thread_id=thread_id, run_id=run_id,
                            agent_slug=request.agent_slug,
                            workspace=request.workspace, mode=request.mode,
                            created=True, status="running")

    # ── State ──────────────────────────────────────────────────────────

    def get(self, run_id):
        state = self.runs.get(run_id)
        if state is None:
            raise gateway_errors.run_not_found(run_id)
        return RunSnapshot(**state)

    def wait(self, run_id, *, wait_seconds=0.0):
        return self.get(run_id)

    def advance_to_question(self, run_id, text):
        """Move a running run to a parked question."""
        self.runs[run_id].update({
            "status": "awaiting_input", "steps_used": 2,
            "pending_input": PendingInput(kind="question",
                                          question={"text": text}),
        })

    def resume(self, run_id):
        """Record the answer and continue."""
        self.runs[run_id].update({"status": "running", "pending_input": None,
                                  "steps_used": 3})

    def finish(self, run_id, summary):
        self.runs[run_id].update({
            "status": "done", "summary": summary, "live": False,
            "steps_used": 4,
        })
        thread_id = self.runs[run_id]["thread_id"]
        self.messages[thread_id].append(
            {"role": "assistant", "content": summary,
             "index": len(self.messages[thread_id])})

    # ── Cancel ─────────────────────────────────────────────────────────

    def cancel(self, run_id, *, cascade=True):
        state = self.runs.get(run_id)
        if state is None:
            raise gateway_errors.run_not_found(run_id)
        previous = state["status"]
        state.update({"status": "cancelled", "live": False,
                      "stop_reason": "aborted"})
        return {"run_id": run_id, "cancelled": True,
                "previous_status": previous, "reason": "requested",
                "children": [], "marker_written": True}

    # ── Threads ────────────────────────────────────────────────────────

    def window(self, thread_id, *, offset=0, limit=50, **kwargs):
        rows = self.messages.get(thread_id)
        if rows is None:
            raise gateway_errors.thread_not_found(thread_id)
        page = rows[offset:offset + limit]
        summary = ThreadSummary(thread_id=thread_id,
                                title=self.titles.get(thread_id, ""),
                                message_count=len(rows))
        return ThreadWindow(
            thread=summary, offset=offset, limit=limit, total=len(rows),
            remaining=max(0, len(rows) - offset - len(page)),
            messages=[MessageView(index=row["index"], role=row["role"],
                                  content=row["content"]) for row in page],
        )

    def search(self, query, **kwargs):
        hits = []
        for thread_id, rows in self.messages.items():
            for row in rows:
                if query.lower() in row["content"].lower():
                    hits.append({"thread_id": thread_id,
                                 "message_index": row["index"],
                                 "content": row["content"]})
        return SearchPage(hits=hits, total=len(hits), query=query)


# ─── Fixtures ───────────────────────────────────────────────────────────────


def _state() -> dict:
    return {"enabled": True, "available": True, "host": "127.0.0.1",
            "port": 8765, "allow_remote": False, "token_count": 1}


@pytest.fixture()
def backend(monkeypatch):
    """Wire an in-memory backend into every route that needs one."""
    engine = FakeBackend()
    monkeypatch.setattr(route_dialogs.gateway_dialog, "start_dialog", engine.start)
    monkeypatch.setattr(route_runs.gateway_runs, "get_run", engine.get)
    monkeypatch.setattr(route_runs.gateway_runs, "wait_run", engine.wait)
    monkeypatch.setattr(route_runs.gateway_runs, "cancel_run", engine.cancel)
    monkeypatch.setattr(route_runs.gateway_runs, "answer_run",
                        lambda run_id, text: engine.resume(run_id))
    monkeypatch.setattr(route_threads.gateway_threads, "read_messages",
                        engine.window)
    monkeypatch.setattr(route_threads.gateway_threads, "search_messages",
                        engine.search)
    from core.gateway import health as gateway_health
    monkeypatch.setattr(gateway_health, "probe_schema", lambda *a, **k: [])
    gateway_health._PROBE_STATE.update({"ready": True, "checked_at": 0.0})
    return engine


@pytest.fixture()
def client(backend):
    """A client that RETURNS a 500 instead of re-raising it.

    Starlette re-raises a server exception after the handler has produced a
    response, and ``TestClient`` does the same by default. A scenario about
    what a CLIENT sees has to read the response, so the re-raise is off.
    """
    return TestClient(server_app.create_app(), raise_server_exceptions=False)


@pytest.fixture()
def token(monkeypatch):
    generated = store.generate_token(slugs=["dev_agent"], note="scenario")
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


# ─── Scenario 1: getting in ─────────────────────────────────────────────────


def test_scenario_a_client_gets_in_with_a_token(client, token):
    """Given no header, the API refuses; given the token, it answers."""
    refused = client.get("/v1/agents")
    assert refused.status_code == 401
    assert refused.headers["WWW-Authenticate"].startswith("Bearer")

    allowed = client.get("/v1/agents", headers=_auth(token))
    assert allowed.status_code == 200


def test_scenario_health_is_open_and_version_is_reported(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["status"] == "ok"
    assert client.get("/version").json()["api_version"] == 1


# ─── Scenario 2: a full dialog ──────────────────────────────────────────────


def test_scenario_launch_poll_answer_and_read_the_result(client, token, backend):
    """The happy path a real client walks, end to end."""
    # Given a launched dialog.
    launched = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent",
                                 "message": "Сделай отчёт",
                                 "workspace": "/tmp/project"},
                           headers=_auth(token))
    assert launched.status_code == 202
    accepted = launched.json()
    run_id = accepted["run_id"]
    thread_id = accepted["thread_id"]
    assert launched.headers["Location"] == "/v1/runs/" + run_id

    # When the client polls, the run is running.
    first = client.get("/v1/runs/" + run_id, headers=_auth(token)).json()
    assert first["status"] == "running"
    assert first["terminal"] is False
    assert first["steps_used"] == 0

    # And when the run parks on a question, the question travels back.
    backend.advance_to_question(run_id, "Which folder should I use?")
    parked = client.get("/v1/runs/" + run_id, headers=_auth(token)).json()
    assert parked["status"] == "awaiting_input"
    assert parked["pending_input"]["kind"] == "question"
    assert "folder" in parked["pending_input"]["question"]["text"]

    # The client answers, and the run continues.
    answered = client.post("/v1/runs/" + run_id + "/answer",
                           json={"answer": "/tmp/project"},
                           headers=_auth(token))
    assert answered.status_code == 202
    assert answered.json()["status"] == "running"
    assert answered.json()["pending_input"] is None

    # When the run finishes, the summary is readable and stays readable.
    backend.finish(run_id, "Отчёт готов")
    done = client.get("/v1/runs/" + run_id, headers=_auth(token)).json()
    assert done["status"] == "done"
    assert done["terminal"] is True
    assert done["summary"] == "Отчёт готов"
    again = client.get("/v1/runs/" + run_id, headers=_auth(token)).json()
    assert again["summary"] == "Отчёт готов"

    # Then the transcript holds the task and the answer.
    window = client.get("/v1/threads/" + thread_id,
                        headers=_auth(token)).json()
    contents = [item["content"] for item in window["messages"]]
    assert "Сделай отчёт" in contents
    assert "Отчёт готов" in contents
    assert window["thread"]["message_count"] == 2


# ─── Scenario 3: cancellation ───────────────────────────────────────────────


def test_scenario_a_cancel_is_accepted_then_the_run_stops(client, token, backend):
    """A client cancels mid-flight and sees a terminal status afterwards."""
    accepted = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent", "message": "долгая задача"},
                           headers=_auth(token)).json()
    run_id = accepted["run_id"]

    cancel = client.post("/v1/runs/" + run_id + "/cancel",
                         headers=_auth(token))
    assert cancel.status_code == 202
    assert cancel.json()["cancelled"] is True
    assert cancel.json()["previous_status"] == "running"
    assert cancel.json()["marker_written"] is True

    stopped = client.get("/v1/runs/" + run_id, headers=_auth(token)).json()
    assert stopped["status"] == "cancelled"
    assert stopped["terminal"] is True
    assert stopped["stop_reason"] == "aborted"


def test_scenario_cancelling_twice_is_not_an_error(client, token, backend):
    """A client that retries a cancel gets a truthful answer, not a failure."""
    accepted = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent", "message": "x"},
                           headers=_auth(token)).json()
    run_id = accepted["run_id"]
    first = client.post("/v1/runs/" + run_id + "/cancel", headers=_auth(token))
    second = client.post("/v1/runs/" + run_id + "/cancel", headers=_auth(token))
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["previous_status"] == "cancelled"


# ─── Scenario 4: the transcript ─────────────────────────────────────────────


def test_scenario_the_transcript_pages_and_is_searchable(client, token, backend):
    """A client reads a page, then finds a message by text."""
    accepted = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent",
                                 "message": "первое сообщение про бюджет"},
                           headers=_auth(token)).json()
    run_id, thread_id = accepted["run_id"], accepted["thread_id"]
    backend.finish(run_id, "второе сообщение про сроки")

    first_page = client.get("/v1/threads/" + thread_id + "?limit=1",
                            headers=_auth(token)).json()
    assert len(first_page["messages"]) == 1
    assert first_page["messages"][0]["index"] == 0
    assert first_page["remaining"] == 1

    second_page = client.get("/v1/threads/" + thread_id + "?offset=1&limit=1",
                             headers=_auth(token)).json()
    assert second_page["messages"][0]["index"] == 1
    assert second_page["remaining"] == 0

    found = client.get("/v1/search?query=бюджет", headers=_auth(token)).json()
    assert found["total"] == 1
    assert found["hits"][0]["thread_id"] == thread_id

    nothing = client.get("/v1/search?query=нетакого", headers=_auth(token)).json()
    assert nothing["total"] == 0


# ─── Scenario 5: failures ───────────────────────────────────────────────────


def test_scenario_an_unknown_run_is_404_and_a_malformed_one_is_400(client, token):
    unknown = client.get("/v1/runs/r_" + "9" * 28, headers=_auth(token))
    assert unknown.status_code == 404
    assert unknown.json()["error_code"] == "run_not_found"

    malformed = client.get("/v1/runs/not-a-run", headers=_auth(token))
    assert malformed.status_code == 400
    assert malformed.json()["field"] == "run_id"


def test_scenario_an_exhausted_pool_tells_the_client_to_come_back(client, token):
    """Given every slot is taken, the launch is refused with a retry hint."""
    taken = 0
    try:
        while server_app.STATE.try_admit():
            taken += 1
        response = client.post("/v1/dialogs",
                               json={"agent_slug": "dev_agent", "message": "x"},
                               headers=_auth(token))
        assert response.status_code == 429
        body = response.json()
        assert body["retryable"] is True
        assert body["limit"] == server_app.server_app_limit()
        assert body["request_id"].startswith("req_")
    finally:
        for _ in range(taken):
            server_app.STATE.release()


def test_scenario_a_refused_launch_leaves_no_thread_and_no_slot(client, token,
                                                              backend):
    """A client that is refused cannot see a thread it did not get."""
    before_threads = dict(backend.messages)
    response = client.post("/v1/dialogs",
                           json={"agent_slug": "dev_agent", "message": "  "},
                           headers=_auth(token))
    assert response.status_code in (400, 422)
    assert backend.messages.keys() == before_threads.keys()
    assert server_app.STATE.active_runs == 0


def test_scenario_a_broken_backend_does_not_leak_its_message(client, token,
                                                           monkeypatch):
    """An internal failure answers a code, never a traceback."""
    def _broken(run_id):
        raise RuntimeError("secret path /etc/passwd")

    monkeypatch.setattr(route_runs.gateway_runs, "get_run", _broken)
    response = client.get("/v1/runs/r_" + "8" * 28, headers=_auth(token))
    assert response.status_code == 500
    assert response.json()["error_code"] == "internal_error"
    assert "/etc/passwd" not in response.text
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
