"""
core.gateway.bootstrap - the cold start of an installation, for any transport.

Until now the sequence that turns a bare copy of the platform into a working
installation lived in two places: ``app.py`` of the Streamlit UI and
``server.app.lifespan`` of the HTTP API. A third transport cannot copy it a
third time - the steps are order-dependent and a missed one produces a
confusing failure instead of an honest one (a server without the default
import answers "unknown agent" while the real cause is a missing seed).

The sequence, and why the order is not interchangeable:

1. create the data folders - every later step writes into them;
2. seed the bundled defaults - without this a fresh installation has no
   employee at all;
3. bring both databases to the current schema revision - the driver runs the
   message-layout archivers first and then the revision chain, so the order
   is one place instead of three;
4. open the engines of both databases - a connection that proves the schema
   reached in step 3 is readable;
5. open the orchestration service - it sweeps records of dead processes.

Every step is idempotent: on a current installation it reads a pragma, finds
nothing to do and returns. The function never raises - a failure is reported
per step, because a transport that cannot bootstrap still has to explain what
is missing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger("core.gateway.bootstrap")


def _ensure_dirs(report: Dict[str, Any]) -> bool:
    """Create the runtime folders of the installation."""
    try:
        from core.paths import ensure_data_dirs
        ensure_data_dirs()
        report["dirs"] = "ok"
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("data directories not created: %s", exc)
        report["dirs"] = "error: " + str(exc)
        return False


def _ensure_defaults(report: Dict[str, Any]) -> bool:
    """Seed the bundled employees, assistants, instructions and skills."""
    try:
        from core.default_imports import ensure_all_defaults
        ensure_all_defaults()
        report["defaults"] = "ok"
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("default import failed: %s", exc)
        report["defaults"] = "error: " + str(exc)
        return False


def _ensure_databases(report: Dict[str, Any]) -> bool:
    """Bring both databases to the layout and the schema this build expects.

    One call covers what used to be three ordered steps: the archivers move
    the message layout forward, then the revision chain upgrades the schema,
    and ``storage.migrations`` keeps that order. Running either half here
    would make the order a property of two transports instead of one driver.
    """
    try:
        from storage import migrations

        result = migrations.upgrade_installation() or {}
        databases = list(result.get("databases") or [])
        for item in databases:
            name = str(item.get("kind") or "schema")
            report[name + "_db"] = str(item.get("action") or "none")
        report["schema"] = str(result.get("head") or "")
        return bool(result.get("ok"))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("database layout and schema failed: %s", exc)
        report["schema"] = "error: " + str(exc)
        return False


def _ensure_schema(report: Dict[str, Any]) -> bool:
    """Open the engines of the main and the agent databases.

    ``_ensure_databases`` has already brought both files to ``head``; this
    step only builds the engines the rest of the bootstrap uses, opening a
    connection that proves the schema is readable. A database that reached
    ``head`` pays a single read of ``alembic_version`` here.
    """
    try:
        from storage import db as storage_db
        storage_db.get_engine()
        storage_db.get_devagent_engine()
        report["engines"] = "ok"
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("engine startup failed: %s", exc)
        report["engines"] = "error: " + str(exc)
        return False


def _start_service(report: Dict[str, Any]) -> bool:
    """Open the orchestration service, which sweeps records of dead runs."""
    try:
        from core.orchestration.service import get_service
        service = get_service()
        report["service"] = "ok"
        try:
            report["store_root"] = str(service.store_root() or "")
        except Exception:  # pragma: no cover - informational
            pass
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("orchestration service unavailable: %s", exc)
        report["service"] = "error: " + str(exc)
        return False


def cold_start(*, seed_defaults: bool = True,
               open_service: bool = True) -> Dict[str, Any]:
    """Bring the installation to a runnable state and report every step.

    ``seed_defaults=False`` skips the bundled default import - a read-only
    diagnostic must not write a roster into a database it was only asked to
    inspect. ``open_service=False`` skips the orchestration service, which
    creates its run store on disk.

    Returns ``{ok, steps: {name: status}, ...}``; ``ok`` is False when any
    REQUIRED step failed. Missing data is not a failure by itself: a clean
    installation is exactly the state this function is meant to build.
    """
    steps: Dict[str, Any] = {}
    required_ok = True

    required_ok = _ensure_dirs(steps) and required_ok
    if not required_ok:
        return {"ok": False, "steps": steps}
    if seed_defaults:
        _ensure_defaults(steps)
    else:
        steps["defaults"] = "skipped"
    _ensure_databases(steps)
    _ensure_schema(steps)
    if open_service:
        _start_service(steps)
    else:
        steps["service"] = "skipped"

    failed = [name for name, value in steps.items()
              if str(value).startswith("error:")]
    return {"ok": not failed, "steps": steps, "failed": failed}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
