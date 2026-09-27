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
    created = c.create_connection("github_rest", "GitHub-TVD2100", "ghp_secret123", account="TVD2100")
    assert created["id"]
    assert created["service"] == "github_rest"
    assert created["name"] == "GitHub-TVD2100"
    assert created["account"] == "TVD2100"
    assert created["has_token"] is True
    assert created["token_masked"] == "***"
    assert "token_encrypted" not in created
    assert c.decrypt_token(created["id"]) == "ghp_secret123"


def test_manifest_on_disk_has_no_plaintext_token(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github_rest", "My GitHub", "super-secret-token")
    raw = _load_raw(created["id"])
    assert "super-secret-token" not in json.dumps(raw)
    assert raw["token_encrypted"]
    assert raw["token_encrypted"] != "super-secret-token"
    # Encrypted value decrypts correctly.
    from core.crypto import decrypt
    assert decrypt(raw["token_encrypted"]) == "super-secret-token"


def test_list_and_get(isolated_data_dir):
    import core.connectors as c
    c.create_connection("github_rest", "Beta", "tok1")
    c.create_connection("github_rest", "Alpha", "tok2")
    items = c.list_connections()
    assert [x["name"] for x in items] == ["Alpha", "Beta"]
    got = c.get_connection(items[0]["id"])
    assert got["name"] == "Alpha"
    assert got["has_token"] is True
    assert c.get_connection("missing") == {}


def test_update_connection(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github_rest", "Old", "tok-old")
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
    created = c.create_connection("github_rest", "X", "tok1")
    assert c.set_connection_token(created["id"], "tok2") is True
    assert c.decrypt_token(created["id"]) == "tok2"


def test_delete_connection(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github_rest", "Doomed", "tok")
    conn_id = created["id"]
    assert c.delete_connection(conn_id) is True
    assert c.get_connection(conn_id) == {}


def test_validation(isolated_data_dir):
    import core.connectors as c
    with pytest.raises(ValueError):
        c.create_connection("gitlab", "X", "tok")
    with pytest.raises(ValueError):
        c.create_connection("github_rest", "", "tok")
    with pytest.raises(ValueError):
        c.create_connection("github_rest", "X", "")


def test_services_registry(isolated_data_dir):
    import core.connectors as c
    services = c.list_services()
    assert any(s["id"] == "github_rest" for s in services)
    assert all(s["id"] != "github" for s in services)
    assert c.get_service("github_rest") is not None
    with pytest.raises(ValueError):
        c.get_service("github")
    with pytest.raises(ValueError):
        c.get_service("nope")


def test_create_github_rest_connection_encrypted_token(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github_rest", "GitHub REST", "ghr_secret999", account="TVD2100")
    assert created["service"] == "github_rest"
    assert created["has_token"] is True
    assert created["token_masked"] == "***"
    assert "token_encrypted" not in created
    # On-disk manifest stores only the encrypted token.
    raw = _load_raw(created["id"])
    assert "ghr_secret999" not in json.dumps(raw)
    assert raw["token_encrypted"] and raw["token_encrypted"] != "ghr_secret999"
    assert c.decrypt_token(created["id"]) == "ghr_secret999"
    # Public listing never leaks the token either.
    assert "ghr_secret999" not in json.dumps(c.list_connections())


def test_public_manifest_never_leaks_token(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection("github_rest", "Safe", "not-a-real-token")
    items = c.list_connections()
    raw_json = json.dumps(items)
    assert "not-a-real-token" not in raw_json
    assert "token_encrypted" not in raw_json


# ─── SSH connections (per-field secrets) ────────────────────────────────────


def test_create_ssh_connection_roundtrip(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection(
        "ssh", "Prod SSH",
        config={"host": "203.0.113.10", "port": 2222, "username": "deploy"},
        secrets={"password": "s3cret-pass"},
        account="deploy@203.0.113.10",
    )
    assert created["service"] == "ssh"
    assert created["has_secrets"] is True
    assert created["secrets_masked"] == {"password": "***"}
    assert "secrets_encrypted" not in created
    assert created["config"]["host"] == "203.0.113.10"
    assert created["config"]["port"] == 2222
    assert c.get_connection_secrets(created["id"]) == {"password": "s3cret-pass"}
    assert c.decrypt_secret(created["id"], "password") == "s3cret-pass"


def test_ssh_manifest_on_disk_keeps_secrets_encrypted(isolated_data_dir):
    import core.connectors as c
    key_body = "-----BEGIN OPENSSH PRIVATE KEY-----abc123"
    created = c.create_connection(
        "ssh", "Keyed SSH",
        config={"host": "example.org", "username": "root"},
        secrets={"private_key": key_body, "key_passphrase": "kp-pass"},
    )
    raw = _load_raw(created["id"])
    dumped = json.dumps(raw)
    assert "kp-pass" not in dumped
    assert "abc123" not in dumped
    assert raw["secrets_encrypted"]["private_key"]
    from core.crypto import decrypt
    assert decrypt(raw["secrets_encrypted"]["private_key"]) == key_body
    assert raw["config"]["port"] == 22


def test_ssh_public_view_never_leaks_secrets(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection(
        "ssh", "Safe SSH",
        config={"host": "h.example", "username": "u"},
        secrets={"password": "do-not-leak"},
    )
    dumped = json.dumps(c.list_connections())
    assert "do-not-leak" not in dumped
    assert "secrets_encrypted" not in dumped
    got = c.get_connection(created["id"])
    assert got["secrets_masked"] == {"password": "***"}
    assert got["has_secrets"] is True


def test_ssh_create_validation(isolated_data_dir):
    import core.connectors as c
    with pytest.raises(ValueError):
        c.create_connection("ssh", "No host", config={"username": "u"},
                            secrets={"password": "p"})
    with pytest.raises(ValueError):
        c.create_connection("ssh", "No user", config={"host": "h"},
                            secrets={"password": "p"})
    with pytest.raises(ValueError):
        c.create_connection("ssh", "No auth",
                            config={"host": "h", "username": "u"}, secrets={})
    with pytest.raises(ValueError):
        c.create_connection("ssh", "Passphrase only",
                            config={"host": "h", "username": "u"},
                            secrets={"key_passphrase": "kp"})
    with pytest.raises(ValueError):
        c.create_connection("ssh", "Bad port",
                            config={"host": "h", "username": "u", "port": "nope"},
                            secrets={"password": "p"})
    with pytest.raises(ValueError):
        c.create_connection("ssh", "Unknown field",
                            config={"host": "h", "username": "u"},
                            secrets={"password": "p", "api_key": "x"})


def test_update_ssh_connection_rotates_secret_and_config(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection(
        "ssh", "Rotate SSH",
        config={"host": "old.example", "username": "deploy"},
        secrets={"password": "old-pass"},
    )
    conn_id = created["id"]
    updated = c.update_connection(conn_id, name="Rotated",
                                  config={"host": "new.example"},
                                  secrets={"password": "new-pass"})
    assert updated["name"] == "Rotated"
    assert updated["config"]["host"] == "new.example"
    assert updated["config"]["port"] == 22
    assert c.get_connection_secrets(conn_id) == {"password": "new-pass"}
    c.update_connection(conn_id, secrets={"password": ""})
    assert c.get_connection_secrets(conn_id) == {"password": "new-pass"}


def test_set_connection_secret_and_decrypt(isolated_data_dir):
    import core.connectors as c
    created = c.create_connection(
        "ssh", "Key SSH",
        config={"host": "h", "username": "u"},
        secrets={"password": "p1"},
    )
    conn_id = created["id"]
    assert c.set_connection_secret(conn_id, "private_key", "KEY-BODY") is True
    assert c.get_connection_secrets(conn_id) == {
        "password": "p1", "private_key": "KEY-BODY",
    }
    assert c.decrypt_secret(conn_id, "private_key") == "KEY-BODY"
    with pytest.raises(ValueError):
        c.decrypt_secret(conn_id, "nope")
    with pytest.raises(ValueError):
        c.set_connection_secret(conn_id, "api_key", "x")
    with pytest.raises(ValueError):
        c.set_connection_secret(conn_id, "password", "   ")
