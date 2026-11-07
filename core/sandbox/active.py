# -*- coding: utf-8 -*-
"""Runs that are executing right now, and the way to stop them.

A timeout is a limit the child agrees to; cancellation is a decision taken
from outside while the child is still running. The two are different
mechanisms, so cancellation needs a handle on the process that the caller of
the run does not hold: the tool layer waits on a result, the UI has only the
dialog it belongs to.

This registry is that handle. A provider registers a run before spawning and
removes it afterwards; ``cancel_thread`` then stops every process started by
one dialog. Nothing here decides WHETHER a run may happen - that is the
policy - and nothing here writes the journal: a cancelled run is recorded by
the runner like any other, with ``cancelled`` set.

The registry lives in the platform process, so a restart clears it: a run
that survived a restart is already gone, because the child dies with the
parent process group.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_lock = threading.RLock()
_runs: Dict[int, "ActiveRun"] = {}
_counter = 0


@dataclass
class ActiveRun:
    """One run that has been registered and not yet finished."""

    key: int
    tool: str
    thread_id: str = ""
    profile: str = ""
    started_at: float = field(default_factory=time.monotonic)
    pid: Optional[int] = None
    cancelled: bool = False
    #: Log file of the run, so a viewer can follow the output while it is
    #: being produced instead of waiting for the final result.
    log_path: str = ""
    #: Backend-specific stop hook, called before the process is killed. A
    #: container run belongs to the daemon, so killing the ``docker run``
    #: client would leave the real process writing inside its mount.
    _on_cancel: Any = None
    _proc: Any = None

    def describe(self) -> Dict[str, Any]:
        """Return the public view of this run (UI and tests read it)."""
        return {
            "key": self.key,
            "tool": self.tool,
            "thread_id": self.thread_id,
            "profile": self.profile,
            "pid": self.pid,
            "cancelled": self.cancelled,
            "log_path": self.log_path,
            "elapsed_s": round(time.monotonic() - self.started_at, 3),
        }


def begin(tool: str, thread_id: str = "", profile: str = "",
          log_path: str = "", on_cancel: Any = None) -> ActiveRun:
    """Register a run that is about to start and return its handle."""
    global _counter
    with _lock:
        _counter += 1
        run = ActiveRun(key=_counter, tool=str(tool or ""),
                        thread_id=str(thread_id or ""), profile=str(profile or ""),
                        log_path=str(log_path or ""), _on_cancel=on_cancel)
        _runs[run.key] = run
        return run


def attach(run: ActiveRun, proc: Any) -> None:
    """Bind the spawned process to *run* so it can be stopped later."""
    with _lock:
        run._proc = proc
        run.pid = getattr(proc, "pid", None)
        # A cancel that arrived between the spawn and this call must not be
        # forgotten: the process now exists and has to be stopped at once.
        cancelled = run.cancelled
    if cancelled:
        _kill(run)


def end(run: ActiveRun) -> None:
    """Forget *run*: it has finished, one way or another."""
    with _lock:
        _runs.pop(run.key, None)


def cancel_thread(thread_id: str) -> int:
    """Stop every active run of one dialog and return how many were asked.

    The count is the number of runs that were still active, not the number
    that died: a process that had already exited is not an error.
    """
    with _lock:
        targets = [run for run in _runs.values()
                   if str(run.thread_id) == str(thread_id or "")]
    for run in targets:
        run.cancelled = True
        _kill(run)
    return len(targets)


def cancel_all() -> int:
    """Stop every active run regardless of the dialog that started it."""
    with _lock:
        targets = list(_runs.values())
    for run in targets:
        run.cancelled = True
        _kill(run)
    return len(targets)


def active_runs() -> List[Dict[str, Any]]:
    """Return the runs that are executing right now, oldest first."""
    with _lock:
        runs = sorted(_runs.values(), key=lambda item: item.started_at)
    return [run.describe() for run in runs]


def cancel_run(key: int) -> bool:
    """Stop one run by its key; return True when it was still active.

    Cancelling a dialog is the usual path; this is the per-run variant that
    a list of active runs needs, so pressing Stop on one card does not stop
    the sibling run of the same dialog.
    """
    with _lock:
        run = _runs.get(int(key))
    if run is None:
        return False
    run.cancelled = True
    _kill(run)
    return True


#: How much of a live log the tail reader looks at, in bytes.
TAIL_WINDOW_BYTES = 64 * 1024


def tail(key: int, lines: int = 20) -> List[str]:
    """Return the last *lines* lines of a run's log file.

    The log is read from its end, so a run that has already written a
    gigabyte does not cost a gigabyte to watch. An empty result means the
    run has not produced output yet - which is different from an error, so
    the caller renders it as "nothing yet" rather than failing.
    """
    with _lock:
        run = _runs.get(int(key))
        path = run.log_path if run is not None else ""
    if not path:
        return []
    return tail_file(path, lines)


def tail_file(path: str, lines: int = 20) -> List[str]:
    """Return the last *lines* lines of the file at *path*."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > TAIL_WINDOW_BYTES:
                handle.seek(size - TAIL_WINDOW_BYTES)
                handle.readline()  # drop the partial first line
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    wanted = max(1, int(lines))
    return text.splitlines()[-wanted:]


def _kill(run: ActiveRun) -> None:
    """Terminate the process group of *run*, when it has one.

    The backend hook runs first even when no process was attached yet: a
    container exists in the daemon from the moment ``docker run`` starts, so
    the client handle is not the only thing that can be stopped.
    """
    hook = run._on_cancel
    if hook is not None:
        try:
            hook()
        except Exception:  # pragma: no cover - a dead backend is not an error
            pass
    proc = run._proc
    if proc is None:
        return
    try:
        from dev_agent.sandbox_proc import kill_tree

        kill_tree(proc, is_posix=__import__("os").name == "posix")
    except Exception:  # pragma: no cover - a dead process is not an error
        pass

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
