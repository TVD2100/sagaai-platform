"""
server.app - the ASGI application and its startup order.

The startup SEQUENCE is the important part of this module, and it is not
interchangeable:

1. take the instance lock - refuse to start a second server;
2. create the data folders - ``history``, ``rag_bases``, ``agents_data``;
3. seed the bundled defaults - without this a clean installation has no
   ``dev_agent`` employee at all, and every launch answers "unknown agent"
   while the real cause is a missing seed;
4. bring both databases to the current layout - the steps are idempotent;
5. create the orchestration service, which sweeps records of dead processes;
6. only then start serving.

Steps 2-4 are the cold start of the platform, which until now lived only in
``app.py`` of the Streamlit UI. A server that skipped them would answer
``200`` to ``/health`` and fail every real request with a misleading code -
so they run here, once, at startup, never per request.

What the server still does NOT do: change the schema in response to a
request. Migration is a startup action of the installation, not an API
feature.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI

from core.gateway import dialog as gateway_dialog
from core.gateway import health as gateway_health
from server import errors as server_errors
from server import middleware as server_middleware
from server.instance_lock import InstanceLock

log = logging.getLogger("server.app")

#: Version of the HTTP contract. Part of the path prefix, not a header.
API_VERSION = 1
#: Prefix every data route carries.
API_PREFIX = "/v1"
#: How many root runs may be in flight at once.
#:
#: Two, not five: the workspace binding lock is process-wide and held for the
#: whole of a tool call, so five concurrent dialogs do not give five times the
#: throughput - they give five times the queueing behind that lock. Two keeps
#: the model calls overlapped (which really are parallel) while keeping the
#: tool phase from degenerating into a long chain of waits.
DEFAULT_MAX_ROOT_RUNS = 2
#: How long a shutdown waits for running dialogs before closing them.
SHUTDOWN_GRACE_SECONDS = 30.0
#: Interval of the housekeeping sweep.
SWEEP_INTERVAL_SECONDS = 3600.0
#: Age after which a finished run record is removed.
RECORD_TTL_DAYS = 14.0


# ─── Startup state ──────────────────────────────────────────────────────────


class ServerState:
    """What the application holds for its whole lifetime."""

    def __init__(self) -> None:
        self.lock = InstanceLock(version=gateway_health.version())
        self.admission = threading.BoundedSemaphore(DEFAULT_MAX_ROOT_RUNS)
        self.started_at = time.time()
        self.startup_report: Dict[str, Any] = {}
        self.sweeper: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.host = "127.0.0.1"
        self.port = 8765
        self.allow_remote = False

    # ── Admission ──────────────────────────────────────────────────────────

    def try_admit(self) -> bool:
        """Take a root-run slot without waiting; False when none is free."""
        return bool(self.admission.acquire(blocking=False))

    def release(self) -> None:
        """Return a root-run slot."""
        try:
            self.admission.release()
        except ValueError:  # pragma: no cover - releasing twice
            pass

    @property
    def active_runs(self) -> int:
        """Return how many root slots are currently taken."""
        try:
            return int(DEFAULT_MAX_ROOT_RUNS - self.admission._value)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            return 0


STATE = ServerState()


# ─── Startup helpers ────────────────────────────────────────────────────────


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
    """Seed the bundled employees, assistants and instructions.

    Without this step a fresh installation has an EMPTY roster: the built-in
    ``dev_agent`` employee is imported from the bundled defaults on first
    start, and a server that never ran that import answers every launch with
    ``unknown_agent``. The failure looks like a wrong slug, which is exactly
    the kind of misleading diagnosis this call exists to prevent.
    """
    try:
        from core.default_imports import ensure_all_defaults
        result = ensure_all_defaults() or {}
        report["defaults"] = "ok"
        report["defaults_detail"] = {
            str(key): str(value) for key, value in list(result.items())[:10]
        }
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("default import failed: %s", exc)
        report["defaults"] = "error: " + str(exc)
        return False


def _ensure_databases(report: Dict[str, Any]) -> bool:
    """Bring both databases to the layout this build expects.

    Both calls are idempotent: on a current database they read a pragma and
    return. On a legacy one they archive it first, which is why this must
    happen before anything reads a message.
    """
    ok = True
    try:
        from core.devagent_db_archive import ensure_database_format as _devagent
        result = _devagent() or {}
        report["devagent_db"] = str(result.get("action") or "none")
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("devagent database layout failed: %s", exc)
        report["devagent_db"] = "error: " + str(exc)
        ok = False
    try:
        from core.chat_db_archive import ensure_database_format as _chat
        result = _chat() or {}
        report["chat_db"] = str(result.get("action") or "none")
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("chat database layout failed: %s", exc)
        report["chat_db"] = "error: " + str(exc)
        ok = False
    return ok


def _start_service(report: Dict[str, Any]) -> bool:
    """Create the orchestration service, which sweeps dead run records."""
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


def _sweep_once() -> None:
    """Remove expired run records and cancel markers, best effort."""
    try:
        from core.orchestration import runs as run_store
        run_store.remove_older_than(RECORD_TTL_DAYS)
    except Exception as exc:  # pragma: no cover - housekeeping is optional
        log.warning("run record sweep failed: %s", exc)
    try:
        from core.gateway import runs as gateway_runs
        directory = gateway_runs.cancels_dir()
        horizon = time.time() - RECORD_TTL_DAYS * 86400.0
        for path in directory.glob("*.flag"):
            try:
                if path.stat().st_mtime < horizon:
                    path.unlink()
            except OSError:  # pragma: no cover - racing delete
                continue
    except Exception as exc:  # pragma: no cover - housekeeping is optional
        log.debug("cancel marker sweep skipped: %s", exc)


def _on_run_closed(run_id: str, status: str) -> None:
    """Release the admission slot of a run the moment its driver finishes.

    A slot released only on a status read would leak for every client that
    launched a run and never polled it - and the next launch would answer
    ``429`` for no visible reason. The driver knows the exact moment, so the
    gateway calls this listener and the slot returns without a reader.
    """
    with _ACTIVE_LOCK:
        known = str(run_id or "") in _ACTIVE_RUN_IDS
        if known:
            _ACTIVE_RUN_IDS.remove(str(run_id))
    if known:
        STATE.release()
        log.info("run %s closed status=%s", run_id, status)


def _start_sweeper(state: ServerState) -> None:
    """Run the housekeeping sweep once now and then hourly."""
    def _loop() -> None:
        while not state.stop_event.is_set():
            _sweep_once()
            state.stop_event.wait(SWEEP_INTERVAL_SECONDS)

    thread = threading.Thread(target=_loop, name="api-sweeper", daemon=True)
    thread.start()
    state.sweeper = thread


def _shutdown_dialogs(state: ServerState) -> None:
    """Close the dialogs still running when the server is asked to stop.

    A driver thread is a daemon: it cannot be joined past the grace period
    and cannot be killed. Marking the record ``interrupted`` is what the
    client needs - otherwise the next status read reports ``running`` until
    the staleness window expires, and the caller waits for work that no
    longer exists.
    """
    deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
    while time.monotonic() < deadline and state.active_runs > 0:
        time.sleep(0.25)
    state.stop_event.set()
    try:
        from core.gateway import runs as gateway_runs
        for run_id in list(_ACTIVE_RUN_IDS):
            try:
                gateway_runs.request_cancel(run_id)
            except Exception:  # pragma: no cover - best effort
                continue
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("shutdown cancel pass failed: %s", exc)


#: Run ids this process admitted, for the shutdown pass.
_ACTIVE_RUN_IDS: List[str] = []
_ACTIVE_LOCK = threading.RLock()


def note_run(run_id: str) -> None:
    """Remember a run this process started."""
    with _ACTIVE_LOCK:
        if run_id and run_id not in _ACTIVE_RUN_IDS:
            _ACTIVE_RUN_IDS.append(run_id)


def forget_run(run_id: str) -> None:
    """Forget a run that reached a terminal status."""
    with _ACTIVE_LOCK:
        if run_id in _ACTIVE_RUN_IDS:
            _ACTIVE_RUN_IDS.remove(run_id)


def is_active_run(run_id: str) -> bool:
    """Return True when this process holds an admission slot for the run."""
    with _ACTIVE_LOCK:
        return str(run_id or "") in _ACTIVE_RUN_IDS


# ─── Lifespan ───────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the startup sequence, then serve, then close what is open."""
    server_middleware.install_log_filter()

    settings: Dict[str, Any] = {}
    try:
        from core import api_token_store as store
        settings = store.load_settings()
    except Exception as exc:  # pragma: no cover - defaults are enough
        log.warning("api settings unavailable at startup: %s", exc)
    STATE.host = str(settings.get("api_bind_host") or "127.0.0.1")
    STATE.port = int(settings.get("api_bind_port") or 8765)
    STATE.allow_remote = bool(settings.get("api_allow_remote"))
    STATE.lock = InstanceLock(port=STATE.port, version=gateway_health.version())

    report: Dict[str, Any] = {"version": gateway_health.version()}
    if not STATE.lock.acquire():
        log.error("another sagaai http server holds the instance lock")
        raise RuntimeError("another sagaai http server is already running")
    gateway_dialog.add_close_listener(_on_run_closed)
    try:
        _ensure_dirs(report)
        _ensure_defaults(report)
        _ensure_databases(report)
        _start_service(report)
        _start_sweeper(STATE)
        report["ready"] = list(gateway_health.probe_schema())
        STATE.startup_report = report
        log.info("http api started version=%s bind=%s:%s", report["version"],
                 STATE.host, STATE.port)
        yield
    finally:
        _shutdown_dialogs(STATE)
        gateway_dialog.remove_close_listener(_on_run_closed)
        STATE.lock.release()
        log.info("http api stopped")


# ─── Application ────────────────────────────────────────────────────────────


def create_app(*, allowed_hosts: Optional[List[str]] = None,
               docs: bool = False) -> FastAPI:
    """Build the application.

    ``docs`` is off by default: an interactive console on a loopback port is
    a convenient surface for anything else running on the machine, and the
    schema is available to whoever needs it as a file.
    """
    app = FastAPI(
        title="SagaAI HTTP API",
        version=str(API_VERSION),
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    hosts = list(allowed_hosts or [])
    app.add_middleware(server_middleware.RequestContextMiddleware)
    if hosts:
        app.add_middleware(server_middleware.HostGuardMiddleware, allowed=hosts)

    server_errors.install(app)

    from server.routes import agents as route_agents
    from server.routes import dialogs as route_dialogs
    from server.routes import health as route_health
    from server.routes import runs as route_runs
    from server.routes import threads as route_threads

    app.include_router(route_health.router)
    app.include_router(route_agents.router, prefix=API_PREFIX)
    app.include_router(route_dialogs.router, prefix=API_PREFIX)
    app.include_router(route_runs.router, prefix=API_PREFIX)
    app.include_router(route_threads.router, prefix=API_PREFIX)

    return app


def server_app_limit() -> int:
    """Return how many root runs the server admits at once."""
    return int(DEFAULT_MAX_ROOT_RUNS)


def admission_state() -> Dict[str, Any]:
    """Return the admission counters for the readiness report."""
    return {
        "active_root_runs": STATE.active_runs,
        "limit": DEFAULT_MAX_ROOT_RUNS,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
