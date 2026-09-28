# -*- coding: utf-8 -*-
"""
core.ssh_connector - SSH/SFTP service connector backed by paramiko.

Functions operate on a connection id (see ``core.connectors``), so SSH
credentials never travel through the application in plain text: the
non-secret config (host, port, username) and the encrypted secrets
(password / private_key / key_passphrase) are resolved and decrypted
inside this module. Secrets are never included in results or error
messages.

Host-key policy: system known_hosts are loaded and unknown hosts are
accepted on first use (``AutoAddPolicy`` - the equivalent of OpenSSH's
``StrictHostKeyChecking=accept-new``). A host key that does not match the
one stored in known_hosts is rejected by paramiko.

Limits: command timeout 60 s by default (clamped to 300 s max), command
output truncated to 100 KB per stream, file read capped at 256 KB,
inline file write capped at 1 MB, streamed upload capped at 50 MB,
connection/banner/auth timeout 15 s.

``upload_file`` streams a LOCAL file (resolved inside the active project
root; path traversal is rejected) to the remote host over SFTP in 64 KB
chunks and, by default, verifies the transfer by re-reading the remote
file and comparing sha256 digests.

``paramiko`` is imported lazily; a missing dependency raises a clean
``SSHConnectorError`` with installation hints. No streamlit imports.
"""
from __future__ import annotations

import hashlib
import posixpath
import stat
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import connectors

__test__ = False  # pytest: functions named test_* are API, not unit tests

CONNECT_TIMEOUT: float = 15.0
DEFAULT_COMMAND_TIMEOUT: float = 60.0
MAX_COMMAND_TIMEOUT: float = 300.0
MAX_OUTPUT_BYTES: int = 100_000
MAX_READ_BYTES: int = 256_000
MAX_WRITE_BYTES: int = 1_000_000
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024
_READ_CHUNK: int = 65536


class SSHConnectorError(ValueError):
    """User-facing error raised by SSH connector operations."""


def _ensure_paramiko():
    """Import paramiko lazily; raise a clean error when it is missing."""
    try:
        import paramiko
        return paramiko
    except ImportError:
        raise SSHConnectorError(
            "paramiko is not installed. Run: pip install paramiko"
        )


def _load_settings(conn_id: str) -> Dict[str, Any]:
    """Resolve host/port/username of an SSH connection (no secrets)."""
    data = connectors.get_connection_full(conn_id)
    if not data:
        raise SSHConnectorError(f"SSH connection not found: {conn_id}")
    service = str(data.get("service") or "")
    if service != "ssh":
        raise SSHConnectorError(
            f"Connection '{conn_id}' is not an SSH connection "
            f"(service: {service or '?'})"
        )
    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    host = str(cfg.get("host") or "").strip()
    username = str(cfg.get("username") or "").strip()
    if not host:
        raise SSHConnectorError("SSH connection has no host configured")
    if not username:
        raise SSHConnectorError("SSH connection has no username configured")
    try:
        port = int(cfg.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    return {"host": host, "port": port, "username": username}


def _clamp_timeout(timeout: Optional[float]) -> float:
    """Return a safe command timeout (default 60 s, clamped to 1..300 s)."""
    if timeout is None or timeout == "":
        return float(DEFAULT_COMMAND_TIMEOUT)
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        raise SSHConnectorError("timeout must be a number of seconds")
    if value < 1.0:
        value = 1.0
    return min(value, float(MAX_COMMAND_TIMEOUT))


def _decode(data: bytes) -> str:
    """Decode command output as UTF-8, replacing invalid bytes."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _read_capped(handle, cap: int) -> Tuple[bytes, bool]:
    """Read at most cap bytes from a handle; report whether it was longer.

    Reads cap+1 bytes via 64 KB chunks (paramiko streams may return fewer
    bytes per read than requested) and slices the result back to cap.
    """
    chunks: List[bytes] = []
    remaining = int(cap) + 1
    while remaining > 0:
        chunk = handle.read(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    truncated = len(data) > cap
    return data[:cap], truncated


def _close_quietly(*handles) -> None:
    """Close each handle, ignoring errors (None values are skipped)."""
    for handle in handles:
        if handle is None:
            continue
        try:
            handle.close()
        except Exception:
            pass


def _digest_capped(handle, cap: int) -> Tuple[str, int, bool]:
    """Stream a handle through sha256, reading at most cap + 1 bytes.

    Returns (hexdigest, bytes_read, oversized); oversized is True when the
    handle held more than *cap* bytes (the digest then covers those cap + 1
    bytes).
    """
    digest = hashlib.sha256()
    remaining = int(cap) + 1
    total = 0
    while remaining > 0:
        chunk = handle.read(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        digest.update(chunk)
        total += len(chunk)
        remaining -= len(chunk)
    return digest.hexdigest(), total, total > cap


def _remote_digest(sftp, remote: str, cap: int) -> Tuple[str, int, bool]:
    """Re-read a remote file and return its (sha256, bytes, oversized)."""
    handle = None
    try:
        handle = sftp.open(remote, "rb")
        return _digest_capped(handle, cap)
    except SSHConnectorError:
        raise
    except Exception as e:
        raise SSHConnectorError(f"Cannot verify remote file: {remote}: {e}")
    finally:
        _close_quietly(handle)


def _load_private_key(paramiko, key_text: str, passphrase: str):
    """Load a private key from text, trying the supported key types."""
    for cls_name in ("RSAKey", "Ed25519Key", "ECDSAKey"):
        cls = getattr(paramiko, cls_name, None)
        loader = getattr(cls, "from_private_key", None) if cls is not None else None
        if not callable(loader):
            continue
        try:
            return loader(StringIO(key_text), password=passphrase or None)
        except Exception:
            continue
    pkey_cls = getattr(paramiko, "PKey", None)
    loader = getattr(pkey_cls, "from_private_key", None) if pkey_cls is not None else None
    if callable(loader):
        try:
            return loader(StringIO(key_text), password=passphrase or None)
        except Exception:
            pass
    raise SSHConnectorError(
        "Cannot load the SSH private key (unsupported key type or wrong passphrase)"
    )


def _is_exc(err: Exception, paramiko, *names: str) -> bool:
    """Return True when *err* is an instance of one of the named classes."""
    for name in names:
        cls = getattr(paramiko, name, None)
        if isinstance(cls, type) and isinstance(err, cls):
            return True
    return False


def _map_connect_error(paramiko, err: Exception, username: str, host: str,
                      port: int) -> "SSHConnectorError":
    """Map a low-level connection failure to a clean user-facing error."""
    if _is_exc(err, paramiko, "AuthenticationException"):
        return SSHConnectorError(
            f"SSH authentication failed for {username}@{host}:{port}"
        )
    if _is_exc(err, paramiko, "BadHostKeyException"):
        return SSHConnectorError(
            f"SSH host key verification failed for {host}: the server key "
            "does not match known_hosts"
        )
    if _is_exc(err, paramiko, "SSHException"):
        return SSHConnectorError(f"SSH error for {host}:{port}: {err}")
    if isinstance(err, TimeoutError):
        return SSHConnectorError(f"SSH connection to {host}:{port} timed out")
    if isinstance(err, OSError):
        return SSHConnectorError(f"SSH network error for {host}:{port}: {err}")
    return SSHConnectorError(f"SSH connection error for {host}:{port}: {err}")


def _connect(conn_id: str):
    """Open an authenticated SSH client for a connection id.

    Auth mode is derived from the stored secrets: a private key wins over
    a password. Agent and key-file lookup are disabled so only the stored
    credentials are used. Raises ``SSHConnectorError`` on any failure.
    """
    settings = _load_settings(conn_id)
    paramiko = _ensure_paramiko()
    host = settings["host"]
    port = settings["port"]
    username = settings["username"]
    try:
        secrets = connectors.get_connection_secrets(conn_id)
    except Exception as e:
        raise SSHConnectorError(f"Cannot read SSH credentials: {e}")
    password = str(secrets.get("password") or "")
    key_text = str(secrets.get("private_key") or "")
    passphrase = str(secrets.get("key_passphrase") or "")
    if not password and not key_text:
        raise SSHConnectorError("SSH connection has no password or private key")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: Dict[str, Any] = {
        "hostname": host,
        "port": port,
        "username": username,
        "timeout": CONNECT_TIMEOUT,
        "banner_timeout": CONNECT_TIMEOUT,
        "auth_timeout": CONNECT_TIMEOUT,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if key_text:
        connect_kwargs["pkey"] = _load_private_key(paramiko, key_text, passphrase)
    else:
        connect_kwargs["password"] = password
    try:
        client.connect(**connect_kwargs)
    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        raise _map_connect_error(paramiko, e, username, host, port)
    return client


def _open_sftp(client):
    """Open an SFTP session; raise a clean error (and close) on failure."""
    try:
        return client.open_sftp()
    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        raise SSHConnectorError(f"Cannot open SFTP session: {e}")


def _ensure_remote_dirs(sftp, directory: str) -> None:
    """Create the remote directory chain (best effort, like mkdir -p)."""
    directory = str(directory or "").strip()
    if not directory or directory in (".", "/"):
        return
    current = "/" if directory.startswith("/") else ""
    for part in directory.split("/"):
        if not part:
            continue
        if current == "/":
            current = "/" + part
        elif current:
            current = f"{current}/{part}"
        else:
            current = part
        try:
            sftp.stat(current)
            continue
        except Exception:
            pass
        try:
            sftp.mkdir(current)
        except Exception as e:
            raise SSHConnectorError(
                f"Cannot create remote directory: {current}: {e}"
            )


def _write_open_error(remote: str, err: Exception,
                      create_dirs: bool) -> "SSHConnectorError":
    """Build a clean open-for-writing error (with a create_dirs hint)."""
    hint = ""
    if not create_dirs and posixpath.dirname(remote):
        hint = (" (the remote parent directory may be missing; "
                "pass create_dirs=true to create it)")
    return SSHConnectorError(
        f"Cannot open remote file for writing: {remote}: {err}{hint}"
    )


def _resolve_local_file(local_path: str, base_dir: str = "") -> Path:
    """Resolve a local upload source inside the workspace root.

    Relative paths resolve against *base_dir*; when *base_dir* is empty the
    active DevAgent workspace root (``dev_agent.config.PROJECT_ROOT``) is
    used. Absolute paths must still live inside the workspace root. Raises
    ``SSHConnectorError`` for empty paths and root escapes.
    """
    raw = str(local_path or "").strip()
    if not raw:
        raise SSHConnectorError("Local file path cannot be empty")
    base = str(base_dir or "").strip()
    if not base:
        try:
            from dev_agent import config as dev_config
            base = str(getattr(dev_config, "PROJECT_ROOT", "") or "")
        except Exception:
            base = ""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if not base:
            raise SSHConnectorError(
                "Relative local_path needs a workspace root: pass base_dir"
            )
        candidate = Path(base) / candidate
    resolved = candidate.resolve()
    if base:
        try:
            resolved.relative_to(Path(base).expanduser().resolve())
        except ValueError as exc:
            raise SSHConnectorError(
                f"Local path escapes the workspace root: {raw}"
            ) from exc
    return resolved


def _entries_from_attrs(remote: str, attrs) -> List[Dict[str, Any]]:
    """Convert SFTPAttributes into tool-friendly directory entries."""
    entries: List[Dict[str, Any]] = []
    for attr in attrs or []:
        name = str(getattr(attr, "filename", "") or "")
        if not name:
            continue
        mode = int(getattr(attr, "st_mode", 0) or 0)
        if stat.S_ISLNK(mode):
            etype = "link"
        elif stat.S_ISDIR(mode):
            etype = "dir"
        else:
            etype = "file"
        entries.append({
            "name": name,
            "path": posixpath.join(remote, name),
            "type": etype,
            "size": int(getattr(attr, "st_size", 0) or 0),
            "mtime": int(getattr(attr, "st_mtime", 0) or 0),
        })
    return entries


def test_connection(conn_id: str) -> Dict[str, Any]:
    """Open an SSH session to validate a connection and refresh its account.

    Returns {"ok": True, "host", "port", "username"} on success and
    stores ``username@host`` as the connection account (best effort).
    Raises ``SSHConnectorError`` on failure.
    """
    settings = _load_settings(conn_id)
    client = _connect(conn_id)
    try:
        client.close()
    except Exception:
        pass
    host = settings["host"]
    port = settings["port"]
    username = settings["username"]
    try:
        connectors.update_connection(conn_id, account=f"{username}@{host}")
    except Exception:
        pass  # account refresh is best-effort
    return {"ok": True, "host": host, "port": port, "username": username}


def exec_command(conn_id: str, command: str, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Run a shell command on the remote host over SSH.

    Output is read fully (both streams), then the exit status is collected.
    Each stream is truncated to ``MAX_OUTPUT_BYTES`` with a flag; the timeout
    defaults to 60 s and is clamped to ``MAX_COMMAND_TIMEOUT``. Returns
    {"command", "exit_status", "stdout", "stderr", "stdout_truncated",
    "stderr_truncated", "timeout_seconds"}.
    """
    cmd = str(command or "")
    if not cmd.strip():
        raise SSHConnectorError("Command cannot be empty")
    timeout_used = _clamp_timeout(timeout)
    client = _connect(conn_id)
    try:
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout_used)
        out_data, out_trunc = _read_capped(stdout, MAX_OUTPUT_BYTES)
        err_data, err_trunc = _read_capped(stderr, MAX_OUTPUT_BYTES)
        exit_status = stdout.channel.recv_exit_status()
    finally:
        try:
            client.close()
        except Exception:
            pass
    return {
        "command": cmd,
        "exit_status": int(exit_status),
        "stdout": _decode(out_data),
        "stderr": _decode(err_data),
        "stdout_truncated": out_trunc,
        "stderr_truncated": err_trunc,
        "timeout_seconds": timeout_used,
    }


def list_dir(conn_id: str, path: str = ".") -> List[Dict[str, Any]]:
    """List a remote directory via SFTP.

    Returns a name-sorted list of {"name", "path", "type" ("dir" | "file" |
    "link"), "size", "mtime"} entries.
    """
    remote = str(path or ".").strip() or "."
    client = _connect(conn_id)
    sftp = None
    try:
        sftp = _open_sftp(client)
        try:
            attrs = sftp.listdir_attr(remote)
        except Exception as e:
            raise SSHConnectorError(
                f"Cannot list remote directory: {remote}: {e}"
            )
        entries = _entries_from_attrs(remote, attrs)
        return sorted(entries, key=lambda e: e["name"].lower())
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass


def read_file(conn_id: str, path: str) -> Dict[str, Any]:
    """Read a UTF-8 text file from the remote host via SFTP.

    Content is capped at ``MAX_READ_BYTES``; the result carries the real
    file size and a truncation flag. Non-UTF-8 files raise a clean error.
    Returns {"path", "content", "size", "truncated"}.
    """
    remote = str(path or "").strip()
    if not remote:
        raise SSHConnectorError("File path cannot be empty")
    client = _connect(conn_id)
    sftp = None
    fh = None
    try:
        sftp = _open_sftp(client)
        try:
            fh = sftp.open(remote, "rb")
        except Exception as e:
            raise SSHConnectorError(f"Cannot open remote file: {remote}: {e}")
        data, truncated = _read_capped(fh, MAX_READ_BYTES)
        try:
            size = int(fh.stat().st_size)
        except Exception:
            size = len(data)
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SSHConnectorError(f"File is not UTF-8 text: {remote}")
    return {"path": remote, "content": content, "size": size,
            "truncated": truncated}


def write_file(conn_id: str, path: str, content: str,
               create_dirs: bool = False) -> Dict[str, Any]:
    """Write a UTF-8 text file to the remote host via SFTP (atomic-free).

    Content is capped at ``MAX_WRITE_BYTES``. When *create_dirs* is True
    the remote parent directory chain is created first (mkdir -p style).
    Returns {"path", "size", "written"}.
    """
    remote = str(path or "").strip()
    if not remote:
        raise SSHConnectorError("File path cannot be empty")
    data = str(content if content is not None else "").encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise SSHConnectorError(
            f"File is too large to write: {len(data)} bytes "
            f"(max {MAX_WRITE_BYTES})"
        )
    client = _connect(conn_id)
    sftp = None
    fh = None
    try:
        sftp = _open_sftp(client)
        if create_dirs:
            _ensure_remote_dirs(sftp, posixpath.dirname(remote))
        try:
            fh = sftp.open(remote, "wb")
        except Exception as e:
            raise _write_open_error(remote, e, create_dirs)
        fh.write(data)
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass
    return {"path": remote, "size": len(data), "written": True}


def upload_file(conn_id: str, local_path: str, remote_path: str,
                base_dir: str = "", create_dirs: bool = False,
                verify: bool = True,
                max_bytes: int = MAX_UPLOAD_BYTES) -> Dict[str, Any]:
    """Upload a LOCAL file to the remote host over SFTP (streamed).

    The local source is resolved inside the workspace root (*base_dir*;
    when empty, the active DevAgent workspace root is used) - paths that
    escape that root are rejected. The file is streamed in 64 KB chunks
    and hashed on the fly; the size cap (default ``MAX_UPLOAD_BYTES``)
    is checked before connecting. With *verify* (default true) the
    remote file is re-read on the same session and its sha256 digest is
    compared with the local one. Returns {"path", "size", "sha256_local",
    "verified", "sha256_remote", "create_dirs"}.
    """
    source = _resolve_local_file(local_path, base_dir)
    if not source.is_file():
        raise SSHConnectorError(f"Local file not found: {local_path}")
    try:
        limit = int(max_bytes)
    except (TypeError, ValueError):
        limit = MAX_UPLOAD_BYTES
    if limit <= 0:
        limit = MAX_UPLOAD_BYTES
    try:
        size = int(source.stat().st_size)
    except OSError as e:
        raise SSHConnectorError(
            f"Cannot stat local file: {local_path}: {e}"
        )
    if size > limit:
        raise SSHConnectorError(
            f"Local file is too large to upload: {size} bytes "
            f"(max {limit})"
        )
    remote = str(remote_path or "").strip()
    if not remote:
        raise SSHConnectorError("Remote file path cannot be empty")
    digest = hashlib.sha256()
    client = _connect(conn_id)
    sftp = None
    fh = None
    local_fh = None
    remote_sha = ""
    remote_size = 0
    oversized = False
    try:
        sftp = _open_sftp(client)
        if create_dirs:
            _ensure_remote_dirs(sftp, posixpath.dirname(remote))
        try:
            fh = sftp.open(remote, "wb")
        except Exception as e:
            raise _write_open_error(remote, e, create_dirs)
        try:
            local_fh = open(source, "rb")
        except OSError as e:
            raise SSHConnectorError(
                f"Cannot read local file: {local_path}: {e}"
            )
        while True:
            chunk = local_fh.read(_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            fh.write(chunk)
        _close_quietly(local_fh)
        local_fh = None
        _close_quietly(fh)
        fh = None
        if verify:
            remote_sha, remote_size, oversized = _remote_digest(
                sftp, remote, limit
            )
    finally:
        _close_quietly(local_fh, fh, sftp, client)
    local_sha = digest.hexdigest()
    verified = bool(
        verify and not oversized
        and remote_size == size
        and remote_sha == local_sha
    )
    return {
        "path": remote,
        "size": size,
        "sha256_local": local_sha,
        "verified": verified,
        "sha256_remote": remote_sha,
        "create_dirs": bool(create_dirs),
    }


__all__ = [
    "SSHConnectorError",
    "test_connection", "exec_command", "list_dir", "read_file", "write_file",
    "upload_file",
]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
