# -*- coding: utf-8 -*-
"""
Tests for core.connectors: CRUD, encrypted token storage, public view.
"""
import os
import sys
import json
import shutil
import tempfile

import pytest

import core.paths


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp directory so connectors never touch real data."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    yield tmp_path


def _load_raw(conn_id):
    root = os.path.join(core.paths.DATA_DIR, "connectors")
    with open(os.path.join(root, conn_id, "manifest.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def test_create_connection_roundtrip(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github", "GitHub-TVD2100", "ghp_secret123", account="TVD2100")
    assert created["id"]
    assert created["service"] == "github"
    assert created["name"] == "GitHub-TVD2100"
    assert created["account"] == "TVD2100"
    assert created["has_token"] is True
    assert created["token_masked"] == "***"
    assert "token_encrypted" not in created
    assert c.decrypt_token(created["id"]) == "ghp_secret123"


def test_manifest_on_disk_has_no_plaintext_token(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github", "My GitHub", "super-secret-token")
    raw = _load_raw(created["id"])
    assert "super-secret-token" not in json.dumps(raw)
    assert raw["token_encrypted"]
    assert raw["token_encrypted"] != "super-secret-token"
    # Encrypted value decrypts correctly.
    from core.crypto import decrypt
    assert decrypt(raw["token_encrypted"]) == "super-secret-token"


def test_list_and_get(isolated_data_dir):
    import core.connectors as c
    c.create_connection("github", "Beta", "tok1")
    c.create_connection("github", "Alpha", "tok2")
    items = c.list_connections()
    assert [x["name"] for x in items] == ["Alpha", "Beta"]
    got = c.get_connection(items[0]["id"])
    assert got["name"] == "Alpha"
    assert got["has_token"] is True
    assert c.get_connection("missing") == {}


def test_update_connection(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github", "Old", "tok-old")
    updated = c.update_connection(created["id"], name="New Name", token="tok-new")
    assert updated["name"] == "New Name"
    assert updated["has_token"] is True
    assert c.decrypt_token(created["id"]) == "tok-new"
    # Account preserved when not supplied.
    updated2 = c.update_connection(created["id"], name="Final")
    assert updated2["account"] == ""
    assert c.decrypt_token(created["id"]) == "tok-new"


def test_set_connection_token(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github", "X", "tok1")
    assert c.set_connection_token(created["id"], "tok2") is True
    assert c.decrypt_token(created["id"]) == "tok2"


def test_delete_connection(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github", "Doomed", "tok")
    conn_id = created["id"]
    assert c.delete_connection(conn_id) is True
    assert c.get_connection(conn_id) == {}


def test_validation(isolated_data_dir):
    import core.connectors as c
    with pytest.raises(ValueError):
        c.create_connection("gitlab", "X", "tok")
    with pytest.raises(ValueError):
        c.create_connection("github", "", "tok")
    with pytest.raises(ValueError):
        c.create_connection("github", "X", "")


def test_services_registry(isolated_data_dir):
    import core.connectors as c
    services = c.list_services()
    assert any(s["id"] == "github" for s in services)
    assert c.get_service("github") is not None
    with pytest.raises(ValueError):
        c.get_service("nope")


def test_public_manifest_never_leaks_token(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github", "Safe", "not-a-real-token")
    items = c.list_connections()
    raw_json = json.dumps(items)
    assert "not-a-real-token" not in raw_json
    assert "token_encrypted" not in raw_json
