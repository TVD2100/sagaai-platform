"""
server.routes.health - the three probes nobody needs a token for.

They answer questions about the PROCESS, not about the owner's data, so a
monitoring system must be able to call them without a credential. What they
must never do is become a diagnostics endpoint: no entity counts, no
versions of dependencies, no filesystem paths. That is a map for anyone
scanning the port.

- ``/health`` answers from memory. It never opens a database, so a liveness
  probe cannot be slowed down - or blocked - by a migration holding an
  exclusive lock on the file.
- ``/ready`` reads the schema in read-only mode and reports what is missing.
  It carries the version because an operator looking at a broken deployment
  needs to know which build is broken; ``/health`` does not, because it is
  the endpoint exposed to the widest audience.
- ``/version`` is separate so the version is announced once, deliberately.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from core.gateway import health as gateway_health
from server import schemas

router = APIRouter(tags=["health"])


@router.get("/health", response_model=schemas.HealthOut,
            summary="Liveness of the API process")
def health() -> dict:
    """Return a liveness report that never touches external storage."""
    report = gateway_health.health()
    try:
        from core import api_token_store as store
        enabled = bool(store.load_settings().get("api_enabled"))
    except Exception:  # pragma: no cover - the switch is informational here
        enabled = False
    return schemas.HealthOut(
        status=report.status,
        pid=report.pid,
        uptime_seconds=report.uptime_seconds,
        api_enabled=enabled,
    ).model_dump()


@router.get("/ready", response_model=schemas.ReadyOut,
            summary="Readiness: can this process serve data requests")
def ready(response: Response) -> dict:
    """Return a readiness report; 503 when the schema is not in place.

    A ``503`` here is a normal state of a fresh installation that has not run
    the platform once, not a failure of the server: the client is told to
    come back after the platform has started.
    """
    report = gateway_health.ready()
    if not report.ready:
        response.status_code = 503
        response.headers["Retry-After"] = "5"
    return schemas.ReadyOut(
        status=report.status,
        version=report.version,
        checks=dict(report.components),
        missing=list(report.missing),
        hint=report.hint,
    ).model_dump()


@router.get("/version", response_model=schemas.VersionOut,
            summary="Version of the platform and of the API contract")
def version() -> dict:
    """Return the platform version and the API contract version."""
    from server.app import API_VERSION
    return schemas.VersionOut(
        version=gateway_health.version(),
        api_version=int(API_VERSION),
    ).model_dump()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
