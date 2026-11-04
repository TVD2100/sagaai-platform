"""
server - the HTTP transport of SagaAI.

This package is the ONLY place in the platform that knows about FastAPI and
pydantic. Everything below it (``core.gateway``, ``core.orchestration``,
``dev_agent``) speaks plain dataclasses, so the same core serves the UI, the
scheduler, a CLI and this server without a second implementation of any
business rule.

Dependency direction, and a test enforces it:

    server  ->  core.gateway  ->  core.orchestration  ->  dev_agent

What lives where:

- ``app``        - ``create_app()`` and the lifespan (startup order matters);
- ``auth``       - bearer token check and the slug scope it carries;
- ``deps``       - FastAPI dependencies shared by the routes;
- ``schemas``    - pydantic models; the ONLY place they appear;
- ``errors``     - ``GatewayError`` -> JSON response;
- ``middleware`` - request id, access log, host allowlist;
- ``routes``     - one module per resource group.

Two things this package must never do:

1. **Run schema migrations.** ``storage.db.get_engine`` executes them,
   including a ``DROP TABLE`` on an old layout; the HTTP process does not
   call it. Readiness is a read-only probe instead.
2. **Serve more than one process.** The run registry, the workspace binding
   and the concurrency pool all live in process memory. ``workers > 1``
   breaks cancellation, the limits and the mailbox, so an instance lock
   refuses to start a second server.
"""

from __future__ import annotations

__all__ = ["app", "auth", "deps", "errors", "middleware", "schemas"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
