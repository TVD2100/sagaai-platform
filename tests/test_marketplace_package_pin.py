# -*- coding: utf-8 -*-
"""tests/test_marketplace_package_pin.py - the update baseline stays readable.

An update decides whether the user changed a value by comparing the
installed config against the manifest of the installed version, read from
the cached package. The cache has a lifetime; without a pin an agent
installed months ago answers "the user changed everything" and an update
moves nothing. These tests cover the pin the installer sets and its release
once no installation refers to the package any more.

Every test imports its modules on the spot: another test file in this suite
re-imports the ``core.marketplace`` package on purpose, so a module bound at
import time would be a stale object the patches never reach.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _modules():
    """Return the marketplace modules in force right now."""
    from core.marketplace import package_pin, registry, store

    return package_pin, registry, store


def _patch_store(monkeypatch, installed):
    """Record the pin calls and serve *installed* as the registry rows.

    The pin module imports both collaborators inside the call, so the patches
    go on the modules themselves rather than on an attribute of the caller.
    """
    package_pin, registry, store = _modules()
    calls = {"pin": [], "unpin": []}

    def _pin(sha256, pinned=True):
        (calls["pin"] if pinned else calls["unpin"]).append(sha256)

    monkeypatch.setattr(store, "pin_package", _pin)
    monkeypatch.setattr(registry, "list_installed",
                        lambda type_=None: list(installed))
    return package_pin, calls


def test_a_fresh_install_pins_its_package(monkeypatch):
    package_pin, calls = _patch_store(monkeypatch, [])

    package_pin.refresh_package_pin("sha-new")

    assert calls["pin"] == ["sha-new"]
    assert calls["unpin"] == []


def test_an_update_pins_the_new_package_and_releases_the_old(monkeypatch):
    """The old package is no longer needed once nothing refers to it."""
    package_pin, calls = _patch_store(monkeypatch, [{"package_sha256": "sha-new"}])

    package_pin.refresh_package_pin("sha-new", "sha-old")

    assert calls["pin"] == ["sha-new"]
    assert calls["unpin"] == ["sha-old"]


def test_the_old_package_stays_pinned_while_another_agent_uses_it(monkeypatch):
    package_pin, calls = _patch_store(monkeypatch, [
        {"id": "this-one", "package_sha256": "sha-new"},
        {"id": "other", "package_sha256": "sha-old"},
    ])

    package_pin.refresh_package_pin("sha-new", "sha-old")

    assert calls["pin"] == ["sha-new"]
    assert calls["unpin"] == []


def test_a_reinstall_of_the_same_package_pins_once_and_releases_nothing(monkeypatch):
    package_pin, calls = _patch_store(monkeypatch, [{"package_sha256": "sha-same"}])

    package_pin.refresh_package_pin("sha-same", "sha-same")

    assert calls["pin"] == ["sha-same"]
    assert calls["unpin"] == []


def test_a_registry_failure_does_not_break_the_install(monkeypatch):
    """A pin is bookkeeping: an install that already committed must not fail."""
    package_pin, registry, store = _modules()

    def _boom(*args, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(store, "pin_package", _boom)
    monkeypatch.setattr(registry, "list_installed", _boom)

    # Reaching the end of the call without raising is the assertion.
    package_pin.refresh_package_pin("sha-new", "sha-old")


def test_the_previous_sha_is_read_before_the_row_is_rewritten(monkeypatch):
    """The row already names the new package once register_agent has run."""
    package_pin, registry, _store = _modules()
    seen = []

    def _row(type_, entity_id):
        seen.append((type_, entity_id))
        return {"package_sha256": "sha-before"}

    monkeypatch.setattr(registry, "get_installed", _row)

    assert package_pin.previous_package_sha("agent-id") == "sha-before"
    assert seen == [("agent", "agent-id")]


def test_an_unreadable_registry_answers_an_empty_previous_sha(monkeypatch):
    package_pin, registry, _store = _modules()

    def _boom(*args, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(registry, "get_installed", _boom)

    assert package_pin.previous_package_sha("agent-id") == ""


def _isolated_cache(tmp_path, monkeypatch):
    """Point the package cache at *tmp_path* and return its module."""
    _package_pin, _registry, store = _modules()

    monkeypatch.setattr(store, "packages_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(store, "ensure_layout", lambda: None)
    monkeypatch.setattr(store, "cache_index_path",
                        lambda: str(tmp_path / "index.json"))
    monkeypatch.setattr(store, "atomic_write_bytes",
                        lambda path, data: Path(path).write_bytes(data))
    monkeypatch.setattr(store, "atomic_write_json",
                        lambda path, data: (tmp_path / "index.json").write_text(
                            json.dumps(data), encoding="utf-8"))
    return store


def test_the_pin_survives_the_cache_lifetime(tmp_path, monkeypatch):
    """A pinned package stays readable after the cache TTL has passed."""
    from datetime import timedelta

    store = _isolated_cache(tmp_path, monkeypatch)

    store.cache_package("sha-pinned", b"payload")
    store.pin_package("sha-pinned")
    # Age the entry past the cache lifetime; the pin is what keeps it.
    store._save_cache_index({
        "sha-pinned": {"size": 7, "cached_at": "2000-01-01T00:00:00Z",
                       "pinned": True},
    })

    assert store.get_cached_package("sha-pinned") == b"payload"
    assert timedelta(days=store.CACHE_TTL_DAYS).days == 30


def test_releasing_the_pin_lets_the_entry_expire(tmp_path, monkeypatch):
    """The release a finished update performs is what the sweep waits for."""
    store = _isolated_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "remove_cached_package", lambda sha: None)

    store.cache_package("sha-old", b"payload")
    store.pin_package("sha-old")
    store.pin_package("sha-old", False)
    store._save_cache_index({
        "sha-old": {"size": 7, "cached_at": "2000-01-01T00:00:00Z",
                    "pinned": False},
    })

    assert store.get_cached_package("sha-old") is None


def test_a_pin_of_an_absent_package_is_not_an_error(tmp_path, monkeypatch):
    """The cache may have been swept before the pin is written."""
    store = _isolated_cache(tmp_path, monkeypatch)
    (tmp_path / "index.json").write_text("{}", encoding="utf-8")

    store.pin_package("sha-absent")
    store.pin_package("sha-absent", False)

    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8")) == {}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
