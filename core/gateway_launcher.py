"""
core.gateway_launcher - start the HTTP API of this installation.

The launcher is the only supported way to run the server::

    python -m core.gateway_launcher

The bind address comes from the settings page, not from the command line:
a port is a property of the installation, and an operator who can pass
``--host 0.0.0.0`` on a command line will eventually do it on a machine
attached to a public network. Passing an extra confirmation flag in the
settings makes that a deliberate act with a recorded reason.

Refusals, and what each one means:

- exit 2 - the API is switched off, or a non-loopback bind was requested
  without the explicit confirmation.
- exit 3 - another server instance already holds the lock. See
  ``server.instance_lock`` for why a second process is not survivable.

The server runs in ONE process. ``uvicorn`` is called with a single worker
and the code never passes ``workers`` - the run registry, the admission
pool and the workspace binding are process memory.
"""

from __future__ import annotations

import logging
import os
import sys

#: Exit code of a refused configuration.
EXIT_BAD_CONFIG = 2


def _configure_logging() -> None:
    """Send the server log to stderr, where the platform already logs.

    A file handler is deliberately absent: systemd and journald already
    collect stderr, and a log file would need a rotation policy nobody would
    maintain for a local tool.
    """
    logging.basicConfig(
        level=os.environ.get("SAGAAI_API_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _refuse(message: str) -> None:
    """Print a refusal and exit with the documented code."""
    print("[http] " + message, file=sys.stderr, flush=True)
    sys.exit(EXIT_BAD_CONFIG)


def resolve_bind() -> tuple:
    """Return ``(host, port, allow_remote)`` from the stored settings.

    Settings win over the environment for the same reason the UI password
    does: the settings page is the place a person configured, and an
    environment variable left in a shell profile must not silently redefine
    where a network service listens.
    """
    from core import api_token_store as store

    settings = store.load_settings()
    host = str(settings.get("api_bind_host") or "127.0.0.1").strip()
    try:
        port = int(settings.get("api_bind_port") or 8765)
    except (TypeError, ValueError):
        port = 8765
    allow_remote = bool(settings.get("api_allow_remote"))

    env_bind = str(os.environ.get("SAGAAI_API_BIND", "") or "").strip()
    if env_bind and ":" in env_bind:
        env_host, _, env_port = env_bind.rpartition(":")
        try:
            port = int(env_port)
            host = env_host or host
        except ValueError:
            pass
    if os.environ.get("SAGAAI_API_ALLOW_REMOTE") == "1":
        allow_remote = True
    return host, port, allow_remote


def main(argv=None) -> int:
    """Start the server; return a process exit code."""
    _configure_logging()
    log = logging.getLogger("core.gateway_launcher")

    host, port, allow_remote = resolve_bind()

    from core import api_token_store as store
    state = store.api_state()
    if not state.get("enabled") and os.environ.get("SAGAAI_API_ENABLED") != "1":
        _refuse("the HTTP API is disabled. Enable it on the Access page first.")
    if not state.get("available"):
        _refuse("the HTTP API has no usable token. Generate one on the Access page.")

    if store.is_remote_bind(host) and not allow_remote:
        _refuse(
            "bind %s is not a loopback address and api_allow_remote is off. "
            "The API has no TLS of its own; expose it through a reverse proxy "
            "with TLS, or tick the confirmation on the Access page." % host
        )

    try:
        import uvicorn
    except Exception:
        _refuse("uvicorn is not installed. Run: pip install -r requirements.lock")

    from server.app import create_app
    from server.instance_lock import InstanceLock, refuse_if_running
    from server.middleware import allowed_hosts as _allowed_hosts

    lock = InstanceLock(port=port)
    refuse_if_running(lock)

    app = create_app(allowed_hosts=_allowed_hosts(host, port, allow_remote))
    if store.is_remote_bind(host):
        log.warning(
            "binding %s without TLS: the token and the dialogs travel in "
            "clear text. Terminate TLS in a reverse proxy in front of this port.",
            host,
        )
    log.info("starting sagaai http api on %s:%s (single worker)", host, port)
    try:
        uvicorn.run(app, host=host, port=port, workers=1, access_log=False,
                    log_level=os.environ.get("SAGAAI_API_LOG_LEVEL", "info").lower())
    finally:
        lock.release()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
