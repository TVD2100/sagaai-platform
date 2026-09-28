# -*- coding: utf-8 -*-
"""Tests for core.ssh_tools - the orchestrator tool layer of the SSH connector.

Reuses the fake-paramiko helper and the isolated_data_dir fixture from
``tests/test_ssh_connector.py``.
"""
import stat as stat_mod
import types

from tests.test_ssh_connector import (
    _make_password_conn,
    install_fake_paramiko,
    isolated_data_dir,  # noqa: F401 - re-exported pytest fixture
)


# ─── Catalog metadata ───────────────────────────────────────────────────────


def test_catalog_lists_six_tools():
    from core import ssh_tools
    tools = ssh_tools.get_tools()
    names = [t["name"] for t in tools]
    assert names == [
        "ssh_exec", "ssh_list_dir", "ssh_read_file",
        "ssh_test_connection", "ssh_upload_file", "ssh_write_file",
    ]
    for t in tools:
        assert t["desc"]
        assert "connector_id" in t["desc"]


# ─── Argument validation ────────────────────────────────────────────────────


def test_missing_connector_id_returns_error():
    from core import ssh_tools
    result = ssh_tools.ssh_exec(command="ls")
    assert result["ok"] is False
    assert "connector_id" in result["error"]


def test_wrong_service_returns_error(isolated_data_dir):
    import core.connectors as c
    from core import ssh_tools
    conn = c.create_connection("github_rest", "GH", "tok")
    result = ssh_tools.ssh_test_connection(connector_id=conn["id"])
    assert result["ok"] is False
    assert "not an SSH connection" in result["error"]


# ─── ssh_test_connection ────────────────────────────────────────────────────


def test_ssh_test_connection_tool(isolated_data_dir, monkeypatch):
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_test_connection(connector_id=conn["id"])
    assert result["ok"] is True
    assert result["result"] == {
        "ok": True, "host": "203.0.113.10", "port": 2222,
        "username": "deploy",
    }
    assert env.instances[-1].closed is True


# ─── ssh_exec ───────────────────────────────────────────────────────────────


def test_ssh_exec_tool_normalizes_timeout(isolated_data_dir, monkeypatch):
    env = install_fake_paramiko(monkeypatch, exec_outputs=(b"ok\n", b"", 0))
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_exec(connector_id=conn["id"], command="uptime",
                                timeout="90")
    assert result["ok"] is True
    assert result["result"]["stdout"] == "ok\n"
    assert result["result"]["timeout_seconds"] == 90.0
    assert env.instances[-1].exec_args == ("uptime", 90.0)


def test_ssh_exec_requires_command(isolated_data_dir, monkeypatch):
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_exec(connector_id=conn["id"], command="   ")
    assert result["ok"] is False
    assert "Missing required argument: command" in result["error"]


def test_ssh_exec_wraps_auth_failure_without_secrets(isolated_data_dir, monkeypatch):
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    env.state.connect_error = env.AuthenticationException("nope")
    from core import ssh_tools
    result = ssh_tools.ssh_exec(connector_id=conn["id"], command="ls")
    assert result["ok"] is False
    assert "authentication failed" in result["error"]
    assert "sup3r-pass" not in result["error"]


# ─── ssh_list_dir ───────────────────────────────────────────────────────────


def _attr(name, mode, size=0, mtime=0):
    return types.SimpleNamespace(filename=name, st_mode=mode,
                                 st_size=size, st_mtime=mtime)


def test_ssh_list_dir_tool(isolated_data_dir, monkeypatch):
    listings = {
        "/var/log": [
            _attr("app.log", stat_mod.S_IFREG | 0o644, 42, 7),
            _attr("archive", stat_mod.S_IFDIR | 0o755),
        ]
    }
    install_fake_paramiko(monkeypatch, listings=listings)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_list_dir(connector_id=conn["id"], path="/var/log")
    assert result["ok"] is True
    names = [e["name"] for e in result["result"]]
    assert names == ["app.log", "archive"]
    assert result["result"][0]["size"] == 42
    assert result["result"][1]["type"] == "dir"


def test_ssh_list_dir_default_path_is_dot(isolated_data_dir, monkeypatch):
    install_fake_paramiko(monkeypatch, listings={".": []})
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_list_dir(connector_id=conn["id"])
    assert result["ok"] is True
    assert result["result"] == []


def test_ssh_list_dir_error_wrapped(isolated_data_dir, monkeypatch):
    install_fake_paramiko(monkeypatch, listings={})
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_list_dir(connector_id=conn["id"], path="/missing")
    assert result["ok"] is False
    assert "Cannot list remote directory" in result["error"]


# ─── ssh_read_file ──────────────────────────────────────────────────────────


def test_ssh_read_file_tool(isolated_data_dir, monkeypatch):
    install_fake_paramiko(monkeypatch,
                          remote_files={"/etc/hostname": b"web-1\n"})
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_read_file(connector_id=conn["id"],
                                     path="/etc/hostname")
    assert result["ok"] is True
    assert result["result"] == {"path": "/etc/hostname", "content": "web-1\n",
                                "size": 6, "truncated": False}


def test_ssh_read_file_requires_path(isolated_data_dir, monkeypatch):
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_read_file(connector_id=conn["id"])
    assert result["ok"] is False
    assert "Missing required argument: path" in result["error"]


def test_ssh_read_file_binary_wrapped(isolated_data_dir, monkeypatch):
    install_fake_paramiko(monkeypatch, remote_files={"/x.bin": b"\xff\x00"})
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_read_file(connector_id=conn["id"], path="/x.bin")
    assert result["ok"] is False
    assert "UTF-8" in result["error"]


# ─── ssh_write_file ─────────────────────────────────────────────────────────


def test_ssh_write_file_tool(isolated_data_dir, monkeypatch):
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_write_file(
        connector_id=conn["id"], path="deploy/app.conf", content="k=v\n",
        create_dirs=True,
    )
    assert result["ok"] is True
    assert result["result"] == {"path": "deploy/app.conf", "size": 4,
                                "written": True}
    sftp = env.instances[-1].last_sftp
    assert sftp.written["deploy/app.conf"] == b"k=v\n"
    assert sftp.mkdirs == ["deploy"]


def test_ssh_write_file_requires_content(isolated_data_dir, monkeypatch):
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_write_file(connector_id=conn["id"], path="/a.txt")
    assert result["ok"] is False
    assert "Missing required argument: content" in result["error"]


def test_ssh_write_file_rejects_oversize(isolated_data_dir, monkeypatch):
    from core.ssh_connector import MAX_WRITE_BYTES
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_write_file(
        connector_id=conn["id"], path="/big.txt",
        content="x" * (MAX_WRITE_BYTES + 1),
    )
    assert result["ok"] is False
    assert "too large" in result["error"]


# ─── ssh_upload_file ────────────────────────────────────────────────────────


def test_ssh_upload_file_tool(isolated_data_dir, monkeypatch, tmp_path):
    import hashlib
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    payload = b"console.log('hi');\n" * 50
    src = tmp_path / "js" / "app.js"
    src.parent.mkdir(parents=True)
    src.write_bytes(payload)
    result = ssh_tools.ssh_upload_file(
        connector_id=conn["id"], local_path="js/app.js",
        remote_path="www/js/app.js", base_dir=str(tmp_path),
        create_dirs=True,
    )
    assert result["ok"] is True
    assert result["result"]["verified"] is True
    assert result["result"]["sha256_local"] == hashlib.sha256(payload).hexdigest()
    sftp = env.instances[-1].last_sftp
    assert sftp.written["www/js/app.js"] == payload
    assert sftp.mkdirs == ["www", "www/js"]


def test_ssh_upload_file_requires_paths(isolated_data_dir, monkeypatch):
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_upload_file(connector_id=conn["id"],
                                       remote_path="a.txt")
    assert result["ok"] is False
    assert "Missing required argument: local_path" in result["error"]
    result = ssh_tools.ssh_upload_file(connector_id=conn["id"],
                                       local_path="a.txt")
    assert result["ok"] is False
    assert "Missing required argument: remote_path" in result["error"]


def test_ssh_upload_file_verify_false(isolated_data_dir, monkeypatch, tmp_path):
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    src = tmp_path / "a.txt"
    src.write_bytes(b"abc")
    result = ssh_tools.ssh_upload_file(
        connector_id=conn["id"], local_path="a.txt", remote_path="a.txt",
        base_dir=str(tmp_path), verify=False,
    )
    assert result["ok"] is True
    assert result["result"]["verified"] is False
    assert result["result"]["sha256_remote"] == ""
    assert env.instances[-1].last_sftp.written["a.txt"] == b"abc"


def test_ssh_upload_file_escape_wrapped(isolated_data_dir, monkeypatch, tmp_path):
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    from core import ssh_tools
    result = ssh_tools.ssh_upload_file(
        connector_id=conn["id"], local_path="../x.txt", remote_path="x.txt",
        base_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "escapes" in result["error"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
