# -*- coding: utf-8 -*-
"""
Scenario tests for the SSH connector feature (core.connectors +
core.ssh_connector + core.ssh_tools + orchestrator binding).

Walk the feature end-to-end through its public entry points the way a user
would use it:

  1. Lifecycle: create an SSH connection, list it, test it, rotate the
     config/secret and delete it.
  2. Secret safety: passwords and private keys never appear in public
     manifests, list output, on-disk plaintext or tool error messages.
  3. Orchestrator binding: enabling the connection makes the extended
     prompt advertise the ssh_* tools.
  4. Dispatcher story: an orchestrator answers "run a command / list a dir /
     read a file / write a file / test the server" through the ssh_* tools;
     disabling the connection revokes them.
  5. Failure story: authentication failures and unknown ids produce clean
     ok=False dicts without leaking secrets.
  6. Publishing story: a user uploads workspace files with ssh_upload_file
     (sha256-verified), recovers from a missing remote directory via
     create_dirs, and backs up the file being replaced.

No network access: paramiko is replaced by the fake from
``tests/test_ssh_connector.py``, so the SSH transport never opens.
"""
import hashlib
import json
import os
import shutil
import stat as stat_mod
import tempfile
import types
from pathlib import Path

import pytest

import core.paths
from tests.test_ssh_connector import install_fake_paramiko


@pytest.fixture()
def isolated_data_dir():
    """Point DATA_DIR and DB paths at a throwaway temp directory."""
    tmp = tempfile.mkdtemp(prefix="sagaai_ssh_scenario_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    old_attrs = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR"):
        old_attrs[attr] = getattr(core.paths, attr, None)
    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    from storage.db import reset_engine, reset_devagent_engine
    reset_engine()
    reset_devagent_engine()
    yield tmp
    reset_engine()
    reset_devagent_engine()

    if old_env:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    for attr, value in old_attrs.items():
        if value is not None:
            setattr(core.paths, attr, value)
    shutil.rmtree(tmp, ignore_errors=True)


def _ssh_connection(name="Prod SSH", host="203.0.113.10", port=2222,
                    username="deploy", password="sup3r-pass"):
    """Create a password-auth SSH connection, return its public manifest."""
    from core import connectors
    return connectors.create_connection(
        "ssh", name,
        config={"host": host, "port": port, "username": username},
        secrets={"password": password},
    )


# 1. Happy path ---------------------------------------------------------------
def test_scenario_ssh_connection_lifecycle(isolated_data_dir, monkeypatch):
    """A user creates, lists, tests, rotates and deletes an SSH connection."""
    from core import connectors
    from core import ssh_connector

    env = install_fake_paramiko(monkeypatch)
    created = _ssh_connection(name="Prod SSH")
    conn_id = created["id"]
    assert created["service"] == "ssh"
    assert created["has_secrets"] is True
    assert created["secrets_masked"] == {"password": "***"}
    assert created["config"] == {"host": "203.0.113.10", "port": 2222,
                                "username": "deploy"}

    # The connection is visible in the list with its (masked) credential info.
    items = connectors.list_connections()
    assert [c["name"] for c in items] == ["Prod SSH"]
    assert items[0]["id"] == conn_id
    assert items[0]["config"]["port"] == 2222
    assert items[0]["secrets_masked"] == {"password": "***"}

    # "Test connection" opens a session and refreshes the stored account.
    result = ssh_connector.test_connection(conn_id)
    assert result["ok"] is True
    assert result["host"] == "203.0.113.10"
    assert result["username"] == "deploy"
    client = env.instances[-1]
    assert client.loaded_host_keys is True
    assert client.closed is True
    assert connectors.get_connection(conn_id)["account"] == "deploy@203.0.113.10"

    # Rotate host/port and the password; the username is kept.
    updated = connectors.update_connection(
        conn_id, name="Prod SSH-2",
        config={"host": "198.51.100.7", "port": 2200},
        secrets={"password": "rotated-pass"},
    )
    assert updated["name"] == "Prod SSH-2"
    assert updated["config"] == {"host": "198.51.100.7", "port": 2200,
                                 "username": "deploy"}
    assert updated["account"] == "deploy@203.0.113.10"
    assert connectors.get_connection_secrets(conn_id) == {
        "password": "rotated-pass"
    }

    # Delete removes the folder and the connection disappears.
    assert connectors.delete_connection(conn_id) is True
    assert connectors.get_connection(conn_id) == {}
    assert connectors.list_connections() == []


# 2. Secret safety ------------------------------------------------------------
def test_scenario_ssh_secrets_never_leak(isolated_data_dir, monkeypatch):
    """Private key material and passphrases never reach public views/errors."""
    from core import connectors
    from core import ssh_tools

    env = install_fake_paramiko(monkeypatch)
    key_body = "-----BEGIN OPENSSH PRIVATE KEY-----\nSCENARIO-KEY\n"
    created = connectors.create_connection(
        "ssh", "Keyed SSH",
        config={"host": "git.example", "username": "git"},
        secrets={"private_key": key_body, "key_passphrase": "kp-secret"},
    )
    conn_id = created["id"]

    views = (connectors.list_connections(),
             [connectors.get_connection(conn_id)], [created])
    for view in views:
        raw = json.dumps(view)
        assert "SCENARIO-KEY" not in raw
        assert "kp-secret" not in raw
        assert "secrets_encrypted" not in raw

    # The manifest on disk keeps secrets encrypted; decryption still works.
    manifest = Path(isolated_data_dir) / "connectors" / conn_id / "manifest.json"
    raw = manifest.read_text(encoding="utf-8")
    assert "SCENARIO-KEY" not in raw
    assert "kp-secret" not in raw
    assert connectors.decrypt_secret(conn_id, "private_key") == key_body

    # Failure surfaces through the tool layer must stay clean too.
    env.state.connect_error = env.AuthenticationException("bad key")
    result = ssh_tools.ssh_test_connection(connector_id=conn_id)
    assert result["ok"] is False
    dumped = json.dumps(result)
    assert "SCENARIO-KEY" not in dumped
    assert "kp-secret" not in dumped


# 3. Orchestrator binding -----------------------------------------------------
def test_scenario_orchestrator_prompt_advertises_ssh_tools(isolated_data_dir):
    """An orchestrator bound to an SSH connection advertises the ssh_* tools."""
    from core.orchestrators import (
        create_orchestrator,
        get_enabled_connections,
        set_enabled_connections,
        _extend_prompt_with_connections,
    )

    conn = _ssh_connection(name="Bound SSH")
    slug = "scenario_ssh_orch"
    create_orchestrator(slug, "SSH Scenario", "Test")
    assert get_enabled_connections(slug) == []

    assert set_enabled_connections(slug, [conn["id"], conn["id"], " "]) is True
    assert get_enabled_connections(slug) == [conn["id"]]

    prompt = _extend_prompt_with_connections("Base prompt", slug)
    assert "## Available service connections" in prompt
    assert conn["id"] in prompt
    assert "Bound SSH" in prompt
    for name in ("ssh_exec", "ssh_list_dir", "ssh_read_file",
                 "ssh_test_connection", "ssh_upload_file", "ssh_write_file"):
        assert name in prompt, name
    # GitHub-only notes must not leak into an ssh-only prompt.
    assert "ghr_upload_file" not in prompt

    # Cleanup: reset DB state for this test process.
    set_enabled_connections(slug, [])


# 4. Dispatcher integration ---------------------------------------------------
def test_scenario_ssh_tools_through_dispatcher(isolated_data_dir, monkeypatch,
                                               tmp_path):
    """The orchestration loop calls all six ssh_* tools via UniversalDevAgent.

    The user story: connect an SSH server, enable the connection on an
    orchestrator, then let the orchestrator run commands, browse, read,
    upload and write files and re-test the server through its dispatcher.
    """
    from core.orchestrators import create_orchestrator, set_enabled_connections

    def _attr(name, mode, size=0, mtime=0):
        return types.SimpleNamespace(filename=name, st_mode=mode,
                                     st_size=size, st_mtime=mtime)

    env = install_fake_paramiko(
        monkeypatch,
        exec_outputs=(b"up 3 days\n", b"", 0),
        remote_files={"/etc/app.conf": b"debug=false\n"},
        listings={"/srv": [
            _attr("app.log", stat_mod.S_IFREG | 0o644, 42, 7),
            _attr("archive", stat_mod.S_IFDIR | 0o755, 0, 3),
        ]},
    )
    conn = _ssh_connection(name="Dispatcher SSH")
    slug = "scenario_ssh_dispatch"
    create_orchestrator(slug, "SSH Dispatch", "Test")
    assert set_enabled_connections(slug, [conn["id"]]) is True

    from dev_agent.universal_agent import UniversalDevAgent

    agent = UniversalDevAgent()
    agent.attach_orchestrator(slug)

    # Run a command on the remote host.
    result = agent.dispatch("ssh_exec", {"connector_id": conn["id"],
                                         "command": "uptime"})
    assert result["ok"] is True
    assert result["result"]["stdout"] == "up 3 days\n"
    assert result["result"]["exit_status"] == 0

    # Browse a directory.
    result = agent.dispatch("ssh_list_dir", {"connector_id": conn["id"],
                                             "path": "/srv"})
    assert result["ok"] is True
    names = [e["name"] for e in result["result"]]
    assert names == ["app.log", "archive"]
    assert result["result"][1]["type"] == "dir"

    # Read a configuration file.
    result = agent.dispatch("ssh_read_file", {"connector_id": conn["id"],
                                              "path": "/etc/app.conf"})
    assert result["ok"] is True
    assert result["result"]["content"] == "debug=false\n"

    # Write a file, creating the missing directories on the way.
    result = agent.dispatch("ssh_write_file", {
        "connector_id": conn["id"], "path": "deploy/app.conf",
        "content": "k=v\n", "create_dirs": True,
    })
    assert result["ok"] is True
    assert env.instances[-1].last_sftp.written["deploy/app.conf"] == b"k=v\n"
    assert env.instances[-1].last_sftp.mkdirs == ["deploy"]

    # Upload a local workspace file (sha256-verified).
    src = tmp_path / "deploy" / "app.js"
    src.parent.mkdir(parents=True)
    payload = b"console.log('dispatcher');\n"
    src.write_bytes(payload)
    result = agent.dispatch("ssh_upload_file", {
        "connector_id": conn["id"], "local_path": "deploy/app.js",
        "remote_path": "www/deploy/app.js", "base_dir": str(tmp_path),
        "create_dirs": True,
    })
    assert result["ok"] is True
    assert result["result"]["verified"] is True
    assert result["result"]["sha256_local"] == hashlib.sha256(payload).hexdigest()
    assert env.instances[-1].last_sftp.written["www/deploy/app.js"] == payload

    # Re-test the server from the orchestration loop.
    result = agent.dispatch("ssh_test_connection", {"connector_id": conn["id"]})
    assert result["ok"] is True
    assert result["result"]["host"] == "203.0.113.10"

    # Without connector_id the tool returns a clean error dict.
    result = agent.dispatch("ssh_exec", {"command": "ls"})
    assert result["ok"] is False
    assert "connector_id" in result["error"]

    # After disabling the connection the tools are no longer callable.
    set_enabled_connections(slug, [])
    agent.attach_orchestrator(slug)
    result = agent.dispatch("ssh_exec", {"connector_id": conn["id"],
                                         "command": "ls"})
    assert result["ok"] is False
    assert "unknown tool" in result.get("error", "").lower()


# 5. Failure story ------------------------------------------------------------
def test_scenario_ssh_failures_are_clean_dicts(isolated_data_dir, monkeypatch):
    """Auth failures and unknown ids become ok=False dicts without secrets."""
    from core import ssh_tools

    env = install_fake_paramiko(monkeypatch)
    conn = _ssh_connection(name="Broken SSH", password="ultra-secret")
    env.state.connect_error = env.AuthenticationException("denied")

    result = ssh_tools.ssh_exec(connector_id=conn["id"], command="uptime")
    assert result["ok"] is False
    assert "authentication failed" in result["error"]
    assert "203.0.113.10" in result["error"]
    assert "ultra-secret" not in json.dumps(result)

    # An unknown connector id is also reported as a clean error dict.
    result = ssh_tools.ssh_read_file(connector_id="nope", path="/x.txt")
    assert result["ok"] is False
    assert "not found" in result["error"]


# 6. Publishing story ---------------------------------------------------------
def test_scenario_publish_local_files(isolated_data_dir, monkeypatch, tmp_path):
    """A user publishes a site build: backup, upload, verify, checksum."""
    from core import ssh_tools

    index_html = b"<html>v2</html>\n"
    index_sha = hashlib.sha256(index_html).hexdigest()
    env = install_fake_paramiko(
        monkeypatch,
        exec_outputs=(f"{index_sha}  www/index.html\n".encode(), b"", 0),
    )
    conn = _ssh_connection(name="Publish SSH")

    files = [
        ("index.html", index_html),
        ("css/styles.css", b"body{margin:0}\n"),
        ("js/app.js", b"console.log('v2');\n" * 300),
    ]
    for rel, data in files:
        path = tmp_path / "site" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    # 1) Back up the file that is about to be replaced.
    result = ssh_tools.ssh_exec(connector_id=conn["id"],
                                command="cp -a www/index.html www/index.html.bak")
    assert result["ok"] is True
    assert result["result"]["exit_status"] == 0

    # 2) Without create_dirs the upload into a missing directory fails with
    #    an actionable hint.
    result = ssh_tools.ssh_upload_file(
        connector_id=conn["id"], local_path="site/css/styles.css",
        remote_path="www/css/styles.css", base_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "create_dirs=true" in result["error"]

    # 3) With create_dirs=true every file uploads and verifies by sha256.
    for rel, data in files:
        result = ssh_tools.ssh_upload_file(
            connector_id=conn["id"], local_path=f"site/{rel}",
            remote_path=f"www/{rel}", base_dir=str(tmp_path),
            create_dirs=True,
        )
        assert result["ok"] is True, result
        assert result["result"]["verified"] is True
        assert result["result"]["sha256_local"] == hashlib.sha256(data).hexdigest()

    written, mkdirs = {}, []
    for inst in env.instances:
        if inst.last_sftp is None:
            continue
        written.update(inst.last_sftp.written)
        mkdirs.extend(inst.last_sftp.mkdirs)
    for rel, data in files:
        assert written[f"www/{rel}"] == data
    assert set(mkdirs) == {"www", "www/css", "www/js"}

    # 4) A fresh session re-reads the published file and the server-side
    #    checksum matches the local one.
    result = ssh_tools.ssh_read_file(connector_id=conn["id"],
                                     path="www/index.html")
    assert result["ok"] is True
    assert result["result"]["content"] == index_html.decode()
    result = ssh_tools.ssh_exec(connector_id=conn["id"],
                                command="sha256sum www/index.html")
    assert result["ok"] is True
    assert result["result"]["stdout"].startswith(index_sha)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
