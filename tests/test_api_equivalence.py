"""Tests that the HTTP API is the same platform, not a second one.

The whole point of the gateway facade is that a dialog launched over HTTP is
the same dialog the page launches: same thread store, same loop, same
limits. A test cannot start a model, but it can check the properties that
would break that equivalence silently:

- every data route depends on the caller check - a route without it is an
  unauthenticated hole, and the walk over the route table catches it;
- the transport needs no UI: ``server`` imports without ``ui``;
- the read path answers from the same functions the page uses, so a field
  the page shows cannot be missing over HTTP for a reason other than an
  explicit decision in the schema.
"""

import inspect
import os
import subprocess
import sys

import pytest

pytest.importorskip("fastapi")

from server import app as server_app  # noqa: E402
from server import deps  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Paths that are intentionally open: a monitoring system has no token.
OPEN_PATHS = {"/health", "/ready", "/version", "/openapi.json"}


def _routes():
    """Yield ``(path, methods, endpoint)`` for every registered route."""
    application = server_app.create_app()
    for route in application.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if not path or endpoint is None:
            continue
        methods = sorted(getattr(route, "methods", []) or [])
        yield path, methods, endpoint


def _depends_on_caller(endpoint) -> bool:
    """Return True when the endpoint signature carries the caller check.

    The check reaches a handler either directly or through
    ``caller_scope``, which itself depends on ``require_caller``; both are
    accepted because a route that only needs the scope is authenticated just
    as well.
    """
    try:
        signature = inspect.signature(endpoint)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    for parameter in signature.parameters.values():
        marker = getattr(parameter.default, "dependency", None)
        if marker in (deps.require_caller, deps.caller_scope, deps.optional_scope):
            return True
    return False


# ─── Every data route is guarded ────────────────────────────────────────────


def test_every_data_route_depends_on_the_caller_check():
    unguarded = []
    for path, methods, endpoint in _routes():
        if path in OPEN_PATHS:
            continue
        if not _depends_on_caller(endpoint):
            unguarded.append("%s %s" % (",".join(methods), path))
    assert unguarded == [], (
        "these routes answer without an authenticated caller: %s" % unguarded)


def test_the_open_paths_are_exactly_the_probes():
    """A new open path must be a conscious edit of this list."""
    unguarded = set()
    for path, methods, endpoint in _routes():
        if not _depends_on_caller(endpoint):
            unguarded.add(path)
    assert unguarded.issubset(OPEN_PATHS), (
        "unexpected unauthenticated path: %s" % (unguarded - OPEN_PATHS))


def test_every_data_route_carries_the_v1_prefix():
    """The version is part of the path, so a client cannot silently drift."""
    for path, methods, endpoint in _routes():
        if path in OPEN_PATHS or not _depends_on_caller(endpoint):
            continue
        assert path.startswith(server_app.API_PREFIX + "/"), path


# ─── The transport does not need the UI ─────────────────────────────────────


def _import_blocked(modules, blocked):
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "BLOCKED = %r\n"
        "class Blocker:\n"
        "    def find_spec(self, name, *a, **k):\n"
        "        for prefix in BLOCKED:\n"
        "            if name == prefix or name.startswith(prefix + '.'):\n"
        "                raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "for mod in %r:\n"
        "    __import__(mod)\n"
        "    print('OK', mod)\n"
    ) % (ROOT, tuple(blocked), list(modules))
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=180)


def test_the_transport_imports_without_the_ui():
    result = _import_blocked(["server.app"], ("ui",))
    assert result.returncode == 0, result.stderr


def test_the_transport_imports_without_streamlit():
    result = _import_blocked(
        ["server.app", "server.routes.health", "server.routes.dialogs",
         "server.routes.runs", "server.routes.threads", "server.routes.agents"],
        ("streamlit",))
    assert result.returncode == 0, result.stderr


# ─── The same data, read the same way ───────────────────────────────────────


def test_the_routes_read_through_the_gateway_not_around_it():
    """A route may not reach into ``dev_agent`` or storage directly.

    The gateway is what keeps the HTTP answer identical to the page answer;
    a route that imports the thread store itself would eventually disagree
    with it. The check is textual on purpose: it is about the import graph
    a reader of the module sees.
    """
    offenders = []
    routes_dir = os.path.join(ROOT, "server", "routes")
    for name in sorted(os.listdir(routes_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(routes_dir, name), encoding="utf-8") as handle:
            source = handle.read()
        for banned in ("core.threads_devagent", "storage.repository",
                       "dev_agent."):
            if "import " + banned in source or "from " + banned in source:
                offenders.append("%s -> %s" % (name, banned))
    assert offenders == [], "routes bypass the gateway: %s" % offenders
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
