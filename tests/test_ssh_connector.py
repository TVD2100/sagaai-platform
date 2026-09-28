# -*- coding: utf-8 -*-
"""Tests for core.ssh_connector using an injected fake paramiko module."""
import io
import json
import os
import stat as stat_mod
import sys
import types

import pytest

import core.paths


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp directory so connectors never touch real data."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    yield tmp_path


def _make_password_conn(**overrides):
    """Create a password-auth SSH connection in the isolated data dir."""
    import core.connectors as c
    config = {"host": "203.0.113.10", "port": 2222, "username": "deploy"}
    config.update(overrides.pop("config", {}))
    secrets = {"password": "sup3r-pass"}
    secrets.update(overrides.pop("secrets", {}))
    return c.create_connection("ssh", "Test SSH", config=config,
                               secrets=secrets, **overrides)


def install_fake_paramiko(monkeypatch, connect_error=None, exec_outputs=None,
                          remote_files=None, listings=None, existing_dirs=None):
    """Install a fake ``paramiko`` module; return a recorder namespace."""
    mod = types.ModuleType("paramiko")

    class SSHException(Exception):
        pass

    class AuthenticationException(SSHException):
        pass

    class BadHostKeyException(SSHException):
        pass

    mod.SSHException = SSHException
    mod.AuthenticationException = AuthenticationException
    mod.BadHostKeyException = BadHostKeyException

    class AutoAddPolicy:
        pass

    mod.AutoAddPolicy = AutoAddPolicy

    class _BaseKey:
        calls = []

        @classmethod
        def from_private_key(cls, file_obj, password=None):
            text = file_obj.read()
            cls.calls.append((text, password))
            if "NOT-A-KEY" in text:
                raise ValueError("invalid key")
            return cls()

    class RSAKey(_BaseKey):
        calls = []

    class Ed25519Key(_BaseKey):
        calls = []

    class ECDSAKey(_BaseKey):
        calls = []

    mod.RSAKey = RSAKey
    mod.Ed25519Key = Ed25519Key
    mod.ECDSAKey = ECDSAKey

    class FakeChannel:
        def __init__(self, exit_status=0):
            self._exit_status = exit_status

        def recv_exit_status(self):
            return self._exit_status

    class FakeStream:
        def __init__(self, data=b"", exit_status=0):
            self._data = data
            self.channel = FakeChannel(exit_status)

        def read(self, size=-1):
            if size is None or size < 0:
                out, self._data = self._data, b""
                return out
            out, self._data = self._data[:size], self._data[size:]
            return out

    class FakeReadFile:
        def __init__(self, data):
            self._data = data
            self._pos = 0
            self.closed = False

        def read(self, size=-1):
            if size is None or size < 0:
                out = self._data[self._pos:]
                self._pos = len(self._data)
                return out
            out = self._data[self._pos:self._pos + size]
            self._pos += len(out)
            return out

        def stat(self):
            return types.SimpleNamespace(st_size=len(self._data))

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    class FakeWriteFile:
        def __init__(self, on_close):
            self._buf = io.BytesIO()
            self._on_close = on_close

        def write(self, data):
            self._buf.write(data)

        def close(self):
            self._on_close(self._buf.getvalue())

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    files = dict(remote_files or {})
    dirs = set(existing_dirs or [])

    class FakeSFTP:
        def __init__(self):
            self.mkdirs = []
            self.written = {}
            self.closed = False

        def listdir_attr(self, path):
            if listings is None or path not in listings:
                raise IOError(f"no such directory: {path}")
            return list(listings[path])

        def open(self, path, mode="rb"):
            if mode.startswith("r"):
                if path in self.written:
                    return FakeReadFile(self.written[path])
                if path not in files:
                    raise IOError(f"no such file: {path}")
                return FakeReadFile(files[path])
            if path in dirs:
                raise IOError(f"is a directory: {path}")
            parent = os.path.dirname(path)
            if parent and parent not in dirs:
                raise IOError(f"no such directory: {parent}")
            def _commit(data, p=path):
                # Persist to this session AND the shared remote state so
                # later sessions see the written file.
                self.written[p] = data
                files[p] = data

            return FakeWriteFile(_commit)

        def stat(self, path):
            if path in files:
                return types.SimpleNamespace(st_size=len(files[path]))
            if path in dirs:
                return types.SimpleNamespace(st_size=0)
            raise IOError(f"no such path: {path}")

        def mkdir(self, path):
            if path in dirs:
                raise IOError(f"already exists: {path}")
            dirs.add(path)
            self.mkdirs.append(path)

        def close(self):
            self.closed = True

    instances = []
    state = types.SimpleNamespace(connect_error=connect_error)

    class SSHClient:
        def __init__(self):
            self.policy = None
            self.loaded_host_keys = False
            self.connect_kwargs = None
            self.closed = False
            self.exec_args = None
            self.last_sftp = None
            instances.append(self)

        def load_system_host_keys(self):
            self.loaded_host_keys = True

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            self.connect_kwargs = dict(kwargs)
            if state.connect_error is not None:
                raise state.connect_error

        def exec_command(self, command, timeout=None):
            self.exec_args = (command, timeout)
            out, err, status = exec_outputs or (b"", b"", 0)
            return FakeStream(b""), FakeStream(out, status), FakeStream(err, 0)

        def open_sftp(self):
            self.last_sftp = FakeSFTP()
            return self.last_sftp

        def close(self):
            self.closed = True

    mod.SSHClient = SSHClient
    monkeypatch.setitem(sys.modules, "paramiko", mod)
    return types.SimpleNamespace(
        module=mod, instances=instances, state=state,
        SSHException=SSHException,
        AuthenticationException=AuthenticationException,
        BadHostKeyException=BadHostKeyException,
        AutoAddPolicy=AutoAddPolicy,
        RSAKey=RSAKey,
    )


# ─── Connection handling ────────────────────────────────────────────────────


def test_missing_paramiko_clean_error(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    conn = _make_password_conn()
    monkeypatch.setitem(sys.modules, "paramiko", None)
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.test_connection(conn["id"])
    assert "paramiko is not installed" in str(exc.value)


def test_connection_missing_id_errors(isolated_data_dir):
    import core.ssh_connector as sc
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.test_connection("nope")
    assert "not found" in str(exc.value)


def test_wrong_service_errors(isolated_data_dir):
    import core.connectors as c
    import core.ssh_connector as sc
    conn = c.create_connection("github_rest", "GH", "tok")
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.test_connection(conn["id"])
    assert "not an SSH connection" in str(exc.value)


def test_password_auth_connects_and_refreshes_account(isolated_data_dir, monkeypatch):
    import core.connectors as c
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    result = sc.test_connection(conn["id"])
    assert result == {"ok": True, "host": "203.0.113.10", "port": 2222,
                      "username": "deploy"}
    client = env.instances[-1]
    assert client.loaded_host_keys is True
    assert isinstance(client.policy, env.module.AutoAddPolicy)
    kwargs = client.connect_kwargs
    assert kwargs["hostname"] == "203.0.113.10"
    assert kwargs["port"] == 2222
    assert kwargs["username"] == "deploy"
    assert kwargs["password"] == "sup3r-pass"
    assert "pkey" not in kwargs
    assert kwargs["allow_agent"] is False
    assert kwargs["look_for_keys"] is False
    assert client.closed is True
    assert c.get_connection(conn["id"])["account"] == "deploy@203.0.113.10"


def test_private_key_auth_uses_pkey(isolated_data_dir, monkeypatch):
    import core.connectors as c
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    key_text = "-----BEGIN OPENSSH PRIVATE KEY-----\nKEY-BODY\n"
    conn = c.create_connection(
        "ssh", "Key SSH",
        config={"host": "h.example", "username": "root"},
        secrets={"private_key": key_text, "key_passphrase": "kp-pass"},
    )
    result = sc.test_connection(conn["id"])
    assert result["ok"] is True
    assert env.RSAKey.calls == [(key_text, "kp-pass")]
    kwargs = env.instances[-1].connect_kwargs
    assert kwargs.get("pkey") is not None
    assert "password" not in kwargs


def test_auth_failure_hides_secrets(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    env.state.connect_error = env.AuthenticationException("auth boom")
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.test_connection(conn["id"])
    message = str(exc.value)
    assert "authentication failed" in message
    assert "sup3r-pass" not in message
    assert "203.0.113.10" in message
    assert env.instances[-1].closed is True


def test_bad_host_key_message(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    env.state.connect_error = env.BadHostKeyException("mismatch")
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.test_connection(conn["id"])
    assert "host key" in str(exc.value)


def test_network_error_mapping(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    env.state.connect_error = OSError("connection refused")
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.test_connection(conn["id"])
    assert "network error" in str(exc.value)


def test_connection_without_credentials_errors(isolated_data_dir, monkeypatch):
    import core.connectors as c
    import core.ssh_connector as sc
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    path = os.path.join(core.paths.DATA_DIR, "connectors", conn["id"],
                        "manifest.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw.pop("secrets_encrypted")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.test_connection(conn["id"])
    assert "Cannot read SSH credentials" in str(exc.value)


# ─── exec_command ───────────────────────────────────────────────────────────


def test_exec_command_returns_output(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch,
                                exec_outputs=(b"hello\n", b"warn\n", 3))
    conn = _make_password_conn()
    result = sc.exec_command(conn["id"], "ls -la")
    assert result["exit_status"] == 3
    assert result["stdout"] == "hello\n"
    assert result["stderr"] == "warn\n"
    assert result["stdout_truncated"] is False
    assert result["stderr_truncated"] is False
    assert result["timeout_seconds"] == sc.DEFAULT_COMMAND_TIMEOUT
    assert env.instances[-1].exec_args == ("ls -la", sc.DEFAULT_COMMAND_TIMEOUT)
    assert env.instances[-1].closed is True


def test_exec_command_truncates_and_clamps_timeout(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    big_out = b"x" * (sc.MAX_OUTPUT_BYTES + 1234)
    big_err = b"y" * (sc.MAX_OUTPUT_BYTES + 1)
    env = install_fake_paramiko(monkeypatch,
                                exec_outputs=(big_out, big_err, 0))
    conn = _make_password_conn()
    result = sc.exec_command(conn["id"], "yes", timeout=99999)
    assert len(result["stdout"]) == sc.MAX_OUTPUT_BYTES
    assert result["stdout_truncated"] is True
    assert len(result["stderr"]) == sc.MAX_OUTPUT_BYTES
    assert result["stderr_truncated"] is True
    assert result["timeout_seconds"] == sc.MAX_COMMAND_TIMEOUT
    assert env.instances[-1].exec_args[1] == sc.MAX_COMMAND_TIMEOUT


def test_exec_command_validates_arguments(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    with pytest.raises(sc.SSHConnectorError):
        sc.exec_command(conn["id"], "   ")
    with pytest.raises(sc.SSHConnectorError):
        sc.exec_command(conn["id"], "ls", timeout="soon")


# ─── SFTP: list_dir / read_file / write_file ────────────────────────────────


def test_list_dir_classifies_entries(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc

    def attr(name, mode, size=0, mtime=0):
        return types.SimpleNamespace(filename=name, st_mode=mode,
                                     st_size=size, st_mtime=mtime)

    listings = {
        "/srv": [
            attr("zeta.txt", stat_mod.S_IFREG | 0o644, 10, 1000),
            attr("alpha", stat_mod.S_IFDIR | 0o755),
            attr("link", stat_mod.S_IFLNK | 0o777),
        ]
    }
    install_fake_paramiko(monkeypatch, listings=listings)
    conn = _make_password_conn()
    entries = sc.list_dir(conn["id"], "/srv")
    assert [e["name"] for e in entries] == ["alpha", "link", "zeta.txt"]
    by_name = {e["name"]: e for e in entries}
    assert by_name["alpha"]["type"] == "dir"
    assert by_name["link"]["type"] == "link"
    assert by_name["zeta.txt"]["type"] == "file"
    assert by_name["zeta.txt"]["size"] == 10
    assert by_name["zeta.txt"]["mtime"] == 1000
    assert by_name["zeta.txt"]["path"] == "/srv/zeta.txt"


def test_read_file_truncates(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    big = b"a" * (sc.MAX_READ_BYTES + 5000)
    install_fake_paramiko(monkeypatch, remote_files={"/data/big.txt": big})
    conn = _make_password_conn()
    result = sc.read_file(conn["id"], "/data/big.txt")
    assert result["truncated"] is True
    assert len(result["content"]) == sc.MAX_READ_BYTES
    assert result["size"] == len(big)


def test_read_file_rejects_binary(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    install_fake_paramiko(monkeypatch,
                          remote_files={"/bin.dat": b"\xff\xfe\x00\x01"})
    conn = _make_password_conn()
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.read_file(conn["id"], "/bin.dat")
    assert "UTF-8" in str(exc.value)


def test_read_file_missing(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    install_fake_paramiko(monkeypatch, remote_files={})
    conn = _make_password_conn()
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.read_file(conn["id"], "/nope.txt")
    assert "Cannot open remote file" in str(exc.value)


def test_write_file_creates_dirs_and_writes(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    result = sc.write_file(conn["id"], "data/sub/file.txt", "hello",
                           create_dirs=True)
    assert result == {"path": "data/sub/file.txt", "size": 5,
                      "written": True}
    sftp = env.instances[-1].last_sftp
    assert sftp.mkdirs == ["data", "data/sub"]
    assert sftp.written["data/sub/file.txt"] == b"hello"
    assert sftp.closed is True
    assert env.instances[-1].closed is True


def test_write_file_rejects_oversize(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.write_file(conn["id"], "/data/huge.txt",
                      "x" * (sc.MAX_WRITE_BYTES + 1))
    assert "too large" in str(exc.value)


def test_write_file_suggests_create_dirs_when_parent_missing(
        isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.write_file(conn["id"], "assets/app.js", "js")
    message = str(exc.value)
    assert "Cannot open remote file for writing" in message
    assert "create_dirs=true" in message


def test_write_file_top_level_without_hint(isolated_data_dir, monkeypatch):
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    result = sc.write_file(conn["id"], "top.txt", "ok")
    assert result["written"] is True
    assert env.instances[-1].last_sftp.written["top.txt"] == b"ok"


# ─── SFTP: upload_file ───────────────────────────────────────────────────


def test_upload_file_streams_and_verifies(isolated_data_dir, monkeypatch, tmp_path):
    import hashlib
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    payload = b"hello upload\n" * 1000
    src = tmp_path / "site" / "index.html"
    src.parent.mkdir(parents=True)
    src.write_bytes(payload)
    result = sc.upload_file(conn["id"], "site/index.html", "www/index.html",
                            base_dir=str(tmp_path), create_dirs=True)
    assert result["size"] == len(payload)
    assert result["sha256_local"] == hashlib.sha256(payload).hexdigest()
    assert result["sha256_remote"] == result["sha256_local"]
    assert result["verified"] is True
    sftp = env.instances[-1].last_sftp
    assert sftp.written["www/index.html"] == payload
    assert sftp.mkdirs == ["www"]
    assert env.instances[-1].closed is True


def test_upload_file_without_verify_skips_reread(isolated_data_dir, monkeypatch, tmp_path):
    import hashlib
    import core.ssh_connector as sc
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    src = tmp_path / "a.txt"
    src.write_bytes(b"abc")
    result = sc.upload_file(conn["id"], "a.txt", "a.txt",
                            base_dir=str(tmp_path), verify=False)
    assert result["sha256_local"] == hashlib.sha256(b"abc").hexdigest()
    assert result["verified"] is False
    assert result["sha256_remote"] == ""
    assert env.instances[-1].last_sftp.written["a.txt"] == b"abc"


def test_upload_file_rejects_escape_and_missing(isolated_data_dir, tmp_path):
    import core.ssh_connector as sc
    conn = _make_password_conn()
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.upload_file(conn["id"], "../outside.txt", "x.txt",
                       base_dir=str(tmp_path))
    assert "escapes" in str(exc.value)
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.upload_file(conn["id"], "missing.txt", "x.txt",
                       base_dir=str(tmp_path))
    assert "not found" in str(exc.value)


def test_upload_file_enforces_size_cap(isolated_data_dir, tmp_path):
    import core.ssh_connector as sc
    conn = _make_password_conn()
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 11)
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.upload_file(conn["id"], "big.bin", "big.bin",
                       base_dir=str(tmp_path), max_bytes=10)
    assert "too large" in str(exc.value)


def test_upload_file_hints_create_dirs(isolated_data_dir, monkeypatch, tmp_path):
    import core.ssh_connector as sc
    install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    src = tmp_path / "app.js"
    src.write_bytes(b"js")
    with pytest.raises(sc.SSHConnectorError) as exc:
        sc.upload_file(conn["id"], "app.js", "assets/app.js",
                       base_dir=str(tmp_path))
    message = str(exc.value)
    assert "Cannot open remote file for writing" in message
    assert "create_dirs=true" in message


def test_upload_file_uses_active_workspace_by_default(isolated_data_dir, monkeypatch, tmp_path):
    import core.ssh_connector as sc
    import dev_agent.config as dev_config
    env = install_fake_paramiko(monkeypatch)
    conn = _make_password_conn()
    monkeypatch.setattr(dev_config, "PROJECT_ROOT", tmp_path)
    src = tmp_path / "index.html"
    src.write_bytes(b"<html/>")
    result = sc.upload_file(conn["id"], "index.html", "index.html")
    assert result["verified"] is True
    assert env.instances[-1].last_sftp.written["index.html"] == b"<html/>"
