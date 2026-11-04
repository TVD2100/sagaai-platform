"""Tests of the thread routes.

The properties worth testing are the ones a client cannot see from a happy
path: a thread outside the token scope answers ``404`` and not ``403`` (a
``403`` would confirm that the id exists), the window carries ABSOLUTE
message indices so a client can keep paging, and the search never widens the
scope it was given.

``core.gateway.threads`` is replaced per test; the routes are a translation
layer and the translation is what is under test.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from core import api_token_store as store  # noqa: E402
from core.gateway import errors as gateway_errors  # noqa: E402
from core.gateway.dto import (  # noqa: E402
    MessageView,
    SearchPage,
    ThreadPage,
    ThreadSummary,
    ThreadWindow,
)
from server import app as server_app  # noqa: E402
from server import auth  # noqa: E402
from server.routes import threads as route_threads  # noqa: E402

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


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def _summary(thread_id="t1", title="Hello") -> ThreadSummary:
    return ThreadSummary(thread_id=thread_id, title=title, agent_slug="dev_agent",
                         message_count=4, updated_at="2026-11-04T20:00:00+00:00")


# ─── Listing ────────────────────────────────────────────────────────────────


def test_a_page_of_threads_is_returned(client, token, monkeypatch):
    monkeypatch.setattr(route_threads.gateway_threads, "list_threads",
                        lambda **kwargs: ThreadPage(items=[_summary()], total=1,
                                                    offset=0, limit=50))
    response = client.get("/v1/threads", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["thread_id"] == "t1"
    assert body["items"][0]["message_count"] == 4


def test_the_token_scope_reaches_the_listing(client, token, monkeypatch):
    seen = {}

    def _list(**kwargs):
        seen.update(kwargs)
        return ThreadPage()

    monkeypatch.setattr(route_threads.gateway_threads, "list_threads", _list)
    client.get("/v1/threads", headers=_auth(token))
    assert seen["allowed_slugs"] == ["dev_agent"]


def test_a_slug_filter_is_parsed_from_the_comma_list(client, token, monkeypatch):
    seen = {}

    def _list(**kwargs):
        seen.update(kwargs)
        return ThreadPage()

    monkeypatch.setattr(route_threads.gateway_threads, "list_threads", _list)
    client.get("/v1/threads?slugs=dev_agent,%20teacher_assistant",
               headers=_auth(token))
    assert seen["slugs"] == ["dev_agent", "teacher_assistant"]


def test_an_empty_slug_filter_means_no_filter(client, token, monkeypatch):
    seen = {}

    def _list(**kwargs):
        seen.update(kwargs)
        return ThreadPage()

    monkeypatch.setattr(route_threads.gateway_threads, "list_threads", _list)
    client.get("/v1/threads", headers=_auth(token))
    assert seen["slugs"] is None


def test_paging_parameters_reach_the_folder(client, token, monkeypatch):
    seen = {}

    def _list(**kwargs):
        seen.update(kwargs)
        return ThreadPage()

    monkeypatch.setattr(route_threads.gateway_threads, "list_threads", _list)
    client.get("/v1/threads?offset=20&limit=10&order=asc", headers=_auth(token))
    assert seen["offset"] == 20
    assert seen["limit"] == 10
    assert seen["order"] == "asc"


def test_a_limit_above_the_ceiling_is_refused(client, token):
    response = client.get("/v1/threads?limit=500", headers=_auth(token))
    assert response.status_code == 400


def test_a_negative_offset_is_refused(client, token):
    response = client.get("/v1/threads?offset=-1", headers=_auth(token))
    assert response.status_code == 400


# ─── Reading one dialog ─────────────────────────────────────────────────────


def test_a_window_carries_absolute_indices(client, token, monkeypatch):
    window = ThreadWindow(thread=_summary(), offset=10, limit=2, total=30,
                          remaining=18,
                          messages=[MessageView(index=10, role="user", content="a"),
                                    MessageView(index=11, role="assistant", content="b")])
    monkeypatch.setattr(route_threads.gateway_threads, "read_messages",
                        lambda *a, **k: window)
    body = client.get("/v1/threads/t1?offset=10&limit=2",
                      headers=_auth(token)).json()
    assert [m["index"] for m in body["messages"]] == [10, 11]
    assert body["remaining"] == 18
    assert body["thread"]["thread_id"] == "t1"


def test_the_scope_reaches_the_window_read(client, token, monkeypatch):
    seen = {}

    def _read(thread_id, **kwargs):
        seen["thread_id"] = thread_id
        seen.update(kwargs)
        return ThreadWindow(thread=_summary())

    monkeypatch.setattr(route_threads.gateway_threads, "read_messages", _read)
    client.get("/v1/threads/t7?include_tool_results=true", headers=_auth(token))
    assert seen["thread_id"] == "t7"
    assert seen["include_tool_results"] is True
    assert seen["allowed_slugs"] == ["dev_agent"]


def test_a_thread_outside_the_scope_answers_404_and_not_403(client, token,
                                                           monkeypatch):
    """A ``403`` would confirm that the id is real."""
    def _hidden(thread_id, **kwargs):
        raise gateway_errors.thread_not_found(thread_id)

    monkeypatch.setattr(route_threads.gateway_threads, "read_messages", _hidden)
    response = client.get("/v1/threads/t_secret", headers=_auth(token))
    assert response.status_code == 404
    assert response.json()["error_code"] == "thread_not_found"


def test_an_empty_thread_id_is_refused(client, token):
    response = client.get("/v1/threads/%20", headers=_auth(token))
    assert response.status_code in (400, 404)


# ─── Search ─────────────────────────────────────────────────────────────────


def test_a_search_returns_its_hits(client, token, monkeypatch):
    monkeypatch.setattr(route_threads.gateway_threads, "search_messages",
                        lambda query, **kwargs: SearchPage(
                            hits=[{"thread_id": "t1", "message_index": 4}],
                            total=1, query=query))
    body = client.get("/v1/search?query=needle", headers=_auth(token)).json()
    assert body["total"] == 1
    assert body["query"] == "needle"
    assert body["hits"][0]["message_index"] == 4


def test_the_search_never_widens_the_scope(client, token, monkeypatch):
    seen = {}

    def _search(query, **kwargs):
        seen["query"] = query
        seen.update(kwargs)
        return SearchPage(query=query)

    monkeypatch.setattr(route_threads.gateway_threads, "search_messages", _search)
    client.get("/v1/search?query=x&max_results=5", headers=_auth(token))
    assert seen["allowed_slugs"] == ["dev_agent"]
    assert seen["max_results"] == 5


def test_an_empty_query_is_refused_by_validation(client, token):
    response = client.get("/v1/search?query=", headers=_auth(token))
    assert response.status_code == 400


def test_a_query_that_is_nothing_but_spaces_is_refused(client, token, monkeypatch):
    def _refuse(query, **kwargs):
        raise gateway_errors.invalid_request("query is required", field="query")

    monkeypatch.setattr(route_threads.gateway_threads, "search_messages", _refuse)
    response = client.get("/v1/search?query=%20%20", headers=_auth(token))
    assert response.status_code == 400
    assert response.json()["field"] == "query"


def test_a_regex_search_flag_is_forwarded(client, token, monkeypatch):
    seen = {}

    def _search(query, **kwargs):
        seen.update(kwargs)
        return SearchPage(query=query)

    monkeypatch.setattr(route_threads.gateway_threads, "search_messages", _search)
    client.get("/v1/search?query=a.*b&regex=true", headers=_auth(token))
    assert seen["regex"] is True
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
