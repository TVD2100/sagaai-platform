# -*- coding: utf-8 -*-
"""Shared execution helper for every provider.

A provider does not own the process lifecycle: bounded output, a full log,
a killable process group and the timeout live in ``dev_agent.sandbox_proc``.
What a provider adds is a WRAPPER in front of the argv (``sandbox-exec -p
... -- argv``, ``bwrap ... -- argv``) and, for the plain process provider,
nothing at all.

Keeping one implementation means three things cannot drift apart: the
limits a profile granted, the log every run leaves behind, and the handle
that makes a run cancellable. A wrapper that built its own ``Popen`` would
silently lose the last one, and a run that cannot be stopped is not
sandboxed - it is merely limited.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, List, Optional

from core.sandbox.contracts import ResourceLimits, SandboxRequest, SandboxResult


def run_wrapped(provider_name: str, isolation: str, prefix: List[str],
                request: SandboxRequest, limits: ResourceLimits,
                subprocess_module: Any = None,
                argv: Optional[List[str]] = None,
                on_cancel: Optional[Callable[[], None]] = None) -> SandboxResult:
    """Run ``prefix + request.argv`` and translate the runner output.

    The run is registered in ``core.sandbox.active`` while the child is
    alive, so another thread can stop it; the registry entry is removed in a
    ``finally`` block, which is the only place that knows the child is gone.

    Args:
        provider_name: value reported in the result and stored in the journal.
        isolation: the level this wrapper actually provides.
        prefix: argv of the sandbox tool, ending with ``--`` where required;
            empty for a provider that wraps nothing.
        request: the prepared run; its ``argv`` is appended to *prefix*.
        limits: ceilings already computed by the policy.
        subprocess_module: spawn module (a patch point in tests).
        argv: a fully built command that replaces ``prefix + request.argv``.
            A provider that must run the program somewhere else (a container,
            an inner Linux) builds its own command; the request stays the
            record of WHAT was asked for.
        on_cancel: called when the run is stopped from outside, before the
            process is killed. A backend whose child is owned by ANOTHER
            process - the container daemon - needs this: killing the client
            leaves the real process running.
    """
    from core.sandbox import active
    from dev_agent.sandbox_proc import run_child

    if not request.argv:
        return SandboxResult(ok=False, provider=provider_name, isolation=isolation,
                             error="Empty command: nothing to run.")
    # The kernel limits go INSIDE the wrapper: the sandbox tool launches the
    # program, so the ceiling has to be part of what it launches, or the
    # program would start unbounded and only be limited afterwards.
    from core.sandbox import launcher

    command = (list(argv) if argv is not None
               else list(prefix) + launcher.wrap(request.argv, limits))
    log_path = log_path_for(request, provider_name)
    # The log path is published BEFORE the spawn: a viewer that attaches to
    # the run from another thread must be able to read output the moment
    # the first line is written, not after the run is over.
    run = active.begin(request.tool, thread_id=request.thread_id,
                       profile=request.profile,
                       log_path=str(log_path) if log_path else "",
                       on_cancel=on_cancel)
    try:
        raw = run_child(
            command,
            env=dict(request.env),
            cwd=request.cwd,
            timeout_s=int(limits.timeout_s),
            log_path=log_path,
            output_limit=int(limits.max_output_bytes),
            subprocess_module=subprocess_module,
            on_spawn=lambda proc: active.attach(run, proc),
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        return SandboxResult(ok=False, provider=provider_name, isolation=isolation,
                             error=f"{type(exc).__name__}: {exc}")
    finally:
        active.end(run)
    result = SandboxResult(
        ok=bool(raw.get("ok")),
        provider=provider_name,
        isolation=isolation,
        returncode=raw.get("returncode"),
        stdout=str(raw.get("stdout") or ""),
        stderr=str(raw.get("stderr") or ""),
        timed_out=bool(raw.get("timed_out")),
        truncated=bool(raw.get("truncated")),
        duration_ms=int(raw.get("duration_ms") or 0),
        log_path=str(raw.get("log_path") or ""),
    )
    if run.cancelled:
        # The child exited because it was killed, not because the program
        # finished: reporting that as a plain failure would hide the cause.
        result.ok = False
        result.cancelled = True
        result.error = "The run was stopped from outside before it finished."
    return result


def log_path_for(request: SandboxRequest, provider_name: str) -> Optional[Path]:
    """Return where the full output of this run is stored."""
    try:
        from core.paths import DATA_DIR

        base = Path(DATA_DIR) / "logs" / "sandbox"
        return base / (f"{request.tool or 'run'}-{provider_name}-"
                       f"{os.getpid()}-{request.fingerprint()[:12]}.log")
    except Exception:  # pragma: no cover - a missing data dir must not block a run
        return None

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
