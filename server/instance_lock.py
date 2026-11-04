"""
server.instance_lock - exactly one HTTP server per installation.

Why a lock and not a note in the documentation: a second server process
silently breaks things nobody would connect to it. The run registry, the
concurrency pool, the notification mailbox and the cancellation flag all
live in process memory; two processes means two of each, so a cancel issued
through one cannot reach a run owned by the other, the limits double, and
the workspace binding of a thread is invisible outside its own process.

None of that raises an error. The damage is a wrong answer - a run that
reads as alive, a limit that admits twice the work, a status that never
becomes terminal. A refusal to start is the only way to make that visible.

The primitive is ``flock`` on a descriptor, the same one the marketplace
installer already uses: the operating system releases it when the process
dies, so a crashed server never leaves a stale lock behind. The file's
CONTENT is informational only; the lock is the descriptor.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:  # pragma: no cover - the import exists on every supported platform
    import fcntl
    _HAVE_FCNTL = True
except Exception:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

log = logging.getLogger("server.instance_lock")

#: Exit code of a refused start. Distinct from a crash (1) so a supervisor
#: can tell "another instance is running" from "this one is broken".
EXIT_ALREADY_RUNNING = 3
#: Name of the lock file inside the runtime folder.
LOCK_NAME = "http_server.lock"


# ─── Paths ──────────────────────────────────────────────────────────────────


def runtime_dir() -> Path:
    """Return the runtime folder that holds the lock.

    The platform runtime directory is the anchor, not the package tree: the
    lock belongs to the INSTALLATION, and writing it next to the source
    modules would ship it with the code.
    """
    try:
        from dev_agent import config as _config
        install_root = Path(getattr(_config, "INSTALL_ROOT", "") or ".")
        runtime = Path(getattr(_config, "_RUNTIME_DIR", "") or ".")
        package_dir = Path(getattr(_config, "DEV_AGENT_DIR", "") or ".").resolve()
        base = (install_root / ".dev_agent"
                if runtime.resolve() == package_dir else runtime)
    except Exception:  # pragma: no cover - config is best effort
        base = Path(".dev_agent")
    return Path(base) / "runtime"


def lock_path() -> Path:
    """Return the full path of the instance lock file."""
    return runtime_dir() / LOCK_NAME


# ─── The lock ───────────────────────────────────────────────────────────────


class InstanceLock:
    """An exclusive, process-scoped lock on the server instance.

    Use it as a context manager around the whole server lifetime::

        lock = InstanceLock(port=8765)
        if not lock.acquire():
            sys.exit(EXIT_ALREADY_RUNNING)
        try:
            serve()
        finally:
            lock.release()
    """

    def __init__(self, *, path: Optional[Path] = None,
                 port: int = 0, version: str = "") -> None:
        self.path = Path(path) if path else lock_path()
        self.port = int(port or 0)
        self.version = str(version or "")
        self._handle: Any = None
        self.holder: Dict[str, Any] = {}

    # ── Acquisition ────────────────────────────────────────────────────────

    def acquire(self) -> bool:
        """Take the lock; False when another process already holds it."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - unwritable runtime
            log.warning("instance lock directory unavailable: %s", exc)
            return False

        try:
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - unwritable file
            log.warning("instance lock file unavailable: %s", exc)
            return False

        if _HAVE_FCNTL:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.holder = self._read_holder(handle)
                try:
                    handle.close()
                except OSError:  # pragma: no cover - closing is best effort
                    pass
                return False

        self._handle = handle
        self._write_holder(handle)
        return True

    # ── Release ────────────────────────────────────────────────────────────

    def release(self) -> None:
        """Drop the lock. The operating system also does this on exit."""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        if _HAVE_FCNTL:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - releasing is best effort
                pass
        try:
            handle.close()
        except OSError:  # pragma: no cover - closing is best effort
            pass

    # ── Context manager ────────────────────────────────────────────────────

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise RuntimeError("another sagaai http server is already running")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    # ── Holder metadata ────────────────────────────────────────────────────

    def _write_holder(self, handle: Any) -> None:
        """Record who holds the lock; informational, never authoritative."""
        payload = {
            "pid": os.getpid(),
            "host": _hostname(),
            "started_at": time.time(),
            "port": self.port,
            "version": self.version,
        }
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(payload))
            handle.flush()
        except OSError as exc:  # pragma: no cover - metadata is best effort
            log.warning("instance lock metadata not written: %s", exc)

    def _read_holder(self, handle: Any) -> Dict[str, Any]:
        """Read the metadata of the holder, best effort."""
        try:
            handle.seek(0)
            raw = handle.read()
            data = json.loads(raw) if raw.strip() else {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):  # pragma: no cover - best effort
            return {}


def _hostname() -> str:
    try:
        return str(socket.gethostname() or "")
    except Exception:  # pragma: no cover - defensive
        return ""


# ─── Startup guard ──────────────────────────────────────────────────────────


def refuse_if_running(lock: InstanceLock) -> None:
    """Exit with the documented code when another instance holds the lock.

    Called by the launcher before uvicorn starts. The message names the
    holder when the metadata survived, because "port busy" sends an operator
    looking at the wrong thing.
    """
    if lock.acquire():
        return
    holder = lock.holder or {}
    pid = holder.get("pid")
    port = holder.get("port")
    detail = []
    if pid:
        detail.append("pid=%s" % pid)
    if port:
        detail.append("port=%s" % port)
    suffix = (" (" + ", ".join(detail) + ")") if detail else ""
    message = (
        "another sagaai http server is already running%s. "
        "SagaAI requires exactly one server process: the run registry, the "
        "delegation limits and the cancellation flags live in process "
        "memory, so a second process would answer with a different truth."
        % suffix
    )
    print("[http] " + message, file=sys.stderr, flush=True)
    sys.exit(EXIT_ALREADY_RUNNING)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
