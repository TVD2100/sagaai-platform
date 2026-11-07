# -*- coding: utf-8 -*-
"""One place where a run is planned, executed and recorded.

Callers describe WHAT to run; this module asks the policy whether it may
run, executes it through the chosen provider and writes the journal row.
Keeping the three steps together is what makes the journal trustworthy: a
refusal and an execution are recorded by the same code path, so a run can
never execute without leaving a trace.

The journal write is best-effort. A record is valuable, but failing to write
one must not turn a successful run into an error for the user.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from core.sandbox.contracts import ResourceLimits, SandboxRequest, SandboxResult


def build_request(*, tool: str, argv: Sequence[str], cwd: str, env: Dict[str, str],
                  code_hash: str = "", profile: str = "", thread_id: str = "",
                  workspace: str = "") -> SandboxRequest:
    """Describe one run without executing anything."""
    return SandboxRequest(
        tool=str(tool or ""), argv=tuple(str(item) for item in argv),
        cwd=str(cwd or ""), env=dict(env or {}), code_hash=str(code_hash or ""),
        profile=str(profile or ""), thread_id=str(thread_id or ""),
        workspace=str(workspace or ""),
    )


def execute(request: SandboxRequest, *, profile: str = "",
            timeout_s: Optional[int] = None,
            providers: Optional[Sequence[Any]] = None,
            journal_engine: Any = None,
            subprocess_module: Any = None) -> SandboxResult:
    """Run *request* under *profile* and record the outcome.

    Args:
        request: the prepared run.
        profile: profile name; defaults to the one stored in the request.
        timeout_s: a caller-side ceiling; it can only tighten the profile.
        providers: explicit provider list (tests); defaults to the build set.
        journal_engine: SQLAlchemy engine of the main database. ``None``
            means "look it up lazily", and a lookup failure simply skips the
            record.
        subprocess_module: spawn module handed to the default providers; the
            tool layer passes its own dependency so a replaced module is
            honoured instead of the process-wide one.

    Returns:
        The provider result, or a denial built by the policy.
    """
    registry = _registry(providers, subprocess_module)
    override = None
    if isinstance(timeout_s, int) and timeout_s > 0:
        override = ResourceLimits(timeout_s=timeout_s,
                                  max_output_bytes=request_limits().max_output_bytes)
    result = registry.run(profile or request.profile, request, limits_override=override)
    _record(request, result, journal_engine)
    return result


def request_limits() -> ResourceLimits:
    """Return the limits used when a caller supplies only a timeout."""
    return ResourceLimits()


def _registry(providers: Optional[Sequence[Any]], subprocess_module: Any = None):
    """Return the registry to use: the caller's providers or the build set."""
    from core.sandbox.policy import SandboxRegistry

    if providers is None:
        from core.sandbox.providers import default_providers

        return SandboxRegistry(default_providers(subprocess_module=subprocess_module))
    return SandboxRegistry(providers)


def _record(request: SandboxRequest, result: SandboxResult, engine: Any) -> None:
    """Write the journal row, ignoring a journal that cannot be written."""
    try:
        from storage import sandbox_journal

        target = engine if engine is not None else _main_engine()
        if target is None:
            return
        sandbox_journal.record_run(
            target,
            run_id=sandbox_journal.new_run_id(),
            tool=request.tool,
            profile=request.profile,
            isolation=result.isolation,
            provider=result.provider,
            request_fingerprint=request.fingerprint(),
            thread_id=request.thread_id,
            workspace=request.workspace,
            limits=result.limits_used,
            denied=result.denied,
            ok=result.ok,
            returncode=result.returncode,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            error=result.error,
            log_path=result.log_path,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except Exception:  # pragma: no cover - a record must never break a run
        pass


def _main_engine():
    """Return the main database engine, or None when it is not available."""
    try:
        from storage.db import get_engine

        return get_engine()
    except Exception:  # pragma: no cover - a headless run has no database
        return None

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
