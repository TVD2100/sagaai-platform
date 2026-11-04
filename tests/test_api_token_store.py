"""Tests of the API token store.

The token is a credential, so the tests check the properties that make it
one: a generated value is long and prefixed, the stored record never holds
the plaintext, verification is by hash, an expired record is refused and a
record without a scope grants nothing.
"""

import json

import pytest

from core import api_token_store as store

# ─── Generation ─────────────────────────────────────────────────────────────


def test_generated_token_has_the_prefix_and_enough_entropy():
    result = store.generate_token(slugs=["dev_agent"], note="test")
    token = result["token"]
    assert token.startswith(store.TOKEN_PREFIX)
    assert len(token) > len(store.TOKEN_PREFIX) + 40


def test_generated_record_never_contains_the_plaintext():
    result = store.generate_token(slugs=["dev_agent"])
    record = result["record"]
    assert "token" not in record
    assert result["token"] not in json.dumps(record)
    assert record["sha256"] == store.hash_token(result["token"])


def test_generated_record_id_is_the_hash_prefix():
    result = store.generate_token()
    record = result["record"]
    assert record["id"] == record["sha256"][:8]
    assert len(record["id"]) == 8


def test_two_generated_tokens_differ():
    first = store.generate_token()["token"]
    second = store.generate_token()["token"]
    assert first != second


def test_default_scope_is_every_employee():
    assert store.generate_token()["record"]["slugs"] == ["*"]


def test_an_explicit_scope_is_kept():
    record = store.generate_token(slugs=["dev_agent", "teacher_assistant"])["record"]
    assert record["slugs"] == ["dev_agent", "teacher_assistant"]


# ─── Verification ───────────────────────────────────────────────────────────


def test_verify_accepts_the_matching_token():
    result = store.generate_token()
    match = store.verify_token(result["token"], [result["record"]])
    assert match is not None
    assert match["id"] == result["record"]["id"]


def test_verify_rejects_a_different_token():
    result = store.generate_token()
    other = store.generate_token()["token"]
    assert store.verify_token(other, [result["record"]]) is None


def test_verify_rejects_a_token_without_the_prefix():
    result = store.generate_token()
    bare = result["token"][len(store.TOKEN_PREFIX):]
    assert store.verify_token(bare, [result["record"]]) is None


def test_verify_rejects_empty_input():
    result = store.generate_token()
    assert store.verify_token("", [result["record"]]) is None
    assert store.verify_token("   ", [result["record"]]) is None


def test_verify_finds_the_right_record_among_several():
    first = store.generate_token()
    second = store.generate_token()
    match = store.verify_token(second["token"],
                               [first["record"], second["record"]])
    assert match is not None
    assert match["id"] == second["record"]["id"]


def test_verify_ignores_broken_records():
    result = store.generate_token()
    records = [None, {}, {"sha256": ""}, "junk", result["record"]]
    assert store.verify_token(result["token"], records) is not None


# ─── Expiry ─────────────────────────────────────────────────────────────────


def test_a_record_without_expiry_never_expires():
    assert store.is_expired({"expires_at": ""}) is False
    assert store.is_expired({}) is False


def test_an_expired_record_is_recognised():
    assert store.is_expired({"expires_at": "2020-01-01T00:00:00+00:00"}) is True


def test_a_future_expiry_is_not_expired():
    assert store.is_expired({"expires_at": "2099-01-01T00:00:00+00:00"}) is False


def test_an_unparsable_expiry_does_not_disable_a_token():
    """A broken date must not silently revoke a working credential."""
    assert store.is_expired({"expires_at": "not-a-date"}) is False


def test_a_naive_expiry_is_read_as_utc():
    assert store.is_expired({"expires_at": "2020-01-01T00:00:00"}) is True


# ─── Scope ──────────────────────────────────────────────────────────────────


def test_scope_is_returned_as_a_list():
    assert store.scope_of({"slugs": ["a", "b"]}) == ["a", "b"]


def test_a_missing_scope_grants_nothing():
    assert store.scope_of({}) == []
    assert store.scope_of({"slugs": None}) == []
    assert store.scope_of({"slugs": "dev_agent"}) == []


def test_blank_scope_entries_are_dropped():
    assert store.scope_of({"slugs": ["a", "", "  ", "b"]}) == ["a", "b"]


# ─── Bind helpers ───────────────────────────────────────────────────────────


def test_loopback_binds_are_not_remote():
    for host in ("127.0.0.1", "::1", "localhost", ""):
        assert store.is_remote_bind(host) is False


def test_any_other_bind_is_remote():
    for host in ("0.0.0.0", "192.168.1.10", "example.com"):
        assert store.is_remote_bind(host) is True
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
