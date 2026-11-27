"""
Unit tests for provider-declared HTTP headers (core.api_chat.extra_headers_for).

A provider may bind its server-side prompt cache to a conversation identity:
without that identity every request starts cold, so a long dialogue pays the
full prompt price on every turn. The service catalog declares the header, the
dialogue supplies the value, and the transport attaches it without ever letting
the catalog touch the credentials.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import json
from pathlib import Path

import pytest

from core.api_chat import extra_headers_for

# ─── extra_headers_for ──────────────────────────────────────────────────────


def test_template_is_filled_with_the_session_id():
    service = {"extra_headers": {"X-Session-ID": "{session_id}"}}
    assert extra_headers_for(service, "thread-42") == {"X-Session-ID": "thread-42"}


def test_template_without_a_session_is_dropped():
    service = {"extra_headers": {"X-Session-ID": "{session_id}"}}
    assert extra_headers_for(service, "") == {}


def test_blank_session_counts_as_missing():
    service = {"extra_headers": {"X-Session-ID": "{session_id}"}}
    assert extra_headers_for(service, "   ") == {}


def test_static_header_needs_no_session():
    service = {"extra_headers": {"X-Trace": "on"}}
    assert extra_headers_for(service, "") == {"X-Trace": "on"}


def test_placeholder_inside_a_longer_template_is_replaced():
    service = {"extra_headers": {"X-Cache-Key": "sagaai:{session_id}:v1"}}
    assert extra_headers_for(service, "abc") == {"X-Cache-Key": "sagaai:abc:v1"}


def test_service_without_declaration_yields_nothing():
    assert extra_headers_for({}, "thread-42") == {}
    assert extra_headers_for(None, "thread-42") == {}


def test_blank_header_name_is_skipped():
    service = {"extra_headers": {"   ": "v", "X-Ok": "1"}}
    assert extra_headers_for(service, "") == {"X-Ok": "1"}


def test_non_dict_declaration_is_ignored():
    assert extra_headers_for({"extra_headers": ["X-A", "X-B"]}, "tid") == {}


def test_none_value_becomes_an_empty_string():
    service = {"extra_headers": {"X-Empty": None}}
    assert extra_headers_for(service, "") == {"X-Empty": ""}


# ─── transport: the headers reach the wire ──────────────────────────────────


class _FakeResponse:
    """Minimal stand-in for a chat/completions response."""

    status_code = 200

    def __init__(self):
        self._body = {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                      "prompt_tokens_details": {"cached_tokens": 4}},
        }

    def json(self):
        return self._body


class _CapturingPost:
    """Record what the transport actually sends."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse()


def test_bearer_request_sends_declared_headers(monkeypatch):
    import core.api_bearer as bearer

    post = _CapturingPost()
    monkeypatch.setattr(bearer.requests, "post", post)
    bearer.bearer_request_impl(
        "https://example.test/v1/chat/completions", "secret-key", "m-1",
        [{"role": "user", "content": "hi"}], 0.0,
        extra_headers={"X-Session-ID": "thread-42"},
    )
    headers = post.calls[0]["headers"]
    assert headers["X-Session-ID"] == "thread-42"
    assert headers["Authorization"] == "Bearer secret-key"


def test_declared_headers_cannot_override_credentials(monkeypatch):
    import core.api_bearer as bearer

    post = _CapturingPost()
    monkeypatch.setattr(bearer.requests, "post", post)
    bearer.bearer_request_impl(
        "https://example.test/v1/chat/completions", "secret-key", "m-1",
        [{"role": "user", "content": "hi"}], 0.0,
        extra_headers={"Authorization": "Bearer stolen",
                       "Content-Type": "text/plain"},
    )
    headers = post.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer secret-key"
    assert headers["Content-Type"] == "application/json"


def test_no_headers_means_the_default_set(monkeypatch):
    import core.api_bearer as bearer

    post = _CapturingPost()
    monkeypatch.setattr(bearer.requests, "post", post)
    bearer.bearer_request_impl(
        "https://example.test/v1/chat/completions", "secret-key", "m-1",
        [{"role": "user", "content": "hi"}], 0.0,
    )
    assert set(post.calls[0]["headers"]) == {"Authorization", "Content-Type"}


# ─── the catalogs declare the header ────────────────────────────────────────


@pytest.mark.parametrize("rel_path", [
    "services/cloud.json",
    "defaults/services/cloud.json",
])
def test_cloud_catalog_declares_the_session_header(rel_path):
    root = Path(__file__).resolve().parent.parent
    data = json.loads((root / rel_path).read_text(encoding="utf-8"))
    declared = data.get("extra_headers") or {}
    assert declared.get("X-Session-ID") == "{session_id}"
