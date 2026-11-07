"""Child-process execution for run_code / run_test.

``subprocess.run`` is not enough for code the agent generates:

* a timeout kills only the direct child, while its grandchildren keep
  running, keep writing files and keep the pipes open;
* ``capture_output=True`` buffers the whole output in the platform process,
  so a loop that prints cannot be stopped by a timeout - memory runs out
  first;
* the partial output of a timed-out run is thrown away, which is exactly the
  output that explains what went wrong.

This module owns the process lifecycle: a new session, bounded output read by
reader threads, a full log on disk, and a two-stage kill of the whole process
group. It reports a structured result the tool layer turns into a tool result.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: How much of each stream is kept in memory (head and tail).
DEFAULT_OUTPUT_LIMIT = 64_000

#: Grace period between SIGTERM and SIGKILL when a run is stopped.
_KILL_GRACE_SEC = 2.0


def _reader(stream, sink: bytearray, limit: int, done: threading.Event,
            stats: Dict[str, int], key: str) -> None:
    """Drain *stream* into *sink*, keeping at most *limit* bytes of the tail.

    The sink is a bounded bytearray, not a list of chunks: a chunk larger
    than the limit would otherwise be kept whole and the memory bound would
    be the chunk size rather than the limit. Trimming the head keeps the
    newest output, which is what explains a failure.
    The number of bytes actually read is recorded in ``stats[key]``: the
    sink is trimmed to the limit, so its length alone cannot tell whether
    anything was dropped.
    """
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            sink.extend(chunk)
            excess = len(sink) - limit
            if excess > 0:
                del sink[:excess]
    except (OSError, ValueError):
        pass
    finally:
        stats[key] = total
        done.set()


def kill_tree(proc, *, is_posix: bool) -> None:
    """Terminate the whole process group of *proc*, then force-kill it."""
    if is_posix:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + _KILL_GRACE_SEC
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass


def run_child(
    cmd: List[str],
    *,
    env: Dict[str, str],
    cwd: str,
    timeout_s: int,
    log_path: Optional[Path] = None,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    subprocess_module: Any = None,
    on_spawn: Optional[Callable[[Any], None]] = None,
) -> Dict[str, Any]:
    """Run *cmd* as a child process with bounded output and a killable tree.

    Args:
        cmd: argv list; never a shell string.
        env: fully built child environment (see ``sandbox_env``).
        cwd: working directory of the child.
        timeout_s: wall-clock limit in seconds.
        log_path: where the complete output is written; a temporary file is
            used when omitted.
        output_limit: bytes kept in memory per stream (the tail).
        subprocess_module: module used to spawn (a patch point in tests).
        on_spawn: called with the process right after it starts, so a caller
            can keep a handle and stop the run from another thread.

    Returns:
        A dict with ``ok``, ``returncode``, ``stdout``, ``stderr``,
        ``timed_out``, ``truncated``, ``log_path``, ``duration_ms``.
    """
    sp = subprocess_module or __import__("subprocess")
    is_posix = os.name == "posix"
    started = time.monotonic()
    out_sink = bytearray()
    err_sink = bytearray()
    out_done = threading.Event()
    err_done = threading.Event()
    read_bytes: Dict[str, int] = {"out": 0, "err": 0}

    popen_kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": sp.DEVNULL,
        "stdout": sp.PIPE,
        "stderr": sp.PIPE,
    }
    if is_posix:
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = sp.Popen(cmd, **popen_kwargs)
    if on_spawn is not None:
        # The handle is handed over BEFORE the wait starts: a cancel that
        # arrives while the child runs must find a process to stop.
        on_spawn(proc)
    readers = [
        threading.Thread(target=_reader,
                         args=(proc.stdout, out_sink, output_limit, out_done, read_bytes, "out"),
                         daemon=True),
        threading.Thread(target=_reader,
                         args=(proc.stderr, err_sink, output_limit, err_done, read_bytes, "err"),
                         daemon=True),
    ]
    for thread in readers:
        thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except Exception as exc:  # subprocess.TimeoutExpired or a stand-in
        if type(exc).__name__ != "TimeoutExpired":
            kill_tree(proc, is_posix=is_posix)
            raise
        timed_out = True
        kill_tree(proc, is_posix=is_posix)
        try:
            proc.wait(timeout=_KILL_GRACE_SEC)
        except Exception:
            pass

    out_done.wait(timeout=_KILL_GRACE_SEC)
    err_done.wait(timeout=_KILL_GRACE_SEC)
    for thread in readers:
        thread.join(timeout=_KILL_GRACE_SEC)

    raw_out = bytes(out_sink)
    raw_err = bytes(err_sink)
    total_out = read_bytes["out"]
    total_err = read_bytes["err"]

    log_file = Path(log_path) if log_path else _default_log_path()
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_bytes(
            b"=== stdout ===\n" + raw_out + b"\n=== stderr ===\n" + raw_err
        )
    except OSError:
        log_file = Path("")

    def _decode(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace")

    return {
        "ok": (not timed_out) and proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _decode(raw_out),
        "stderr": _decode(raw_err),
        "timed_out": timed_out,
        "truncated": total_out > output_limit or total_err > output_limit,
        "log_path": str(log_file) if str(log_file) else "",
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _default_log_path() -> Path:
    """Return a log path inside the platform runtime directory."""
    import tempfile
    base = Path(tempfile.gettempdir()) / "sagaai-logs"
    return base / f"run-{int(time.time() * 1000)}.log"


def split_for_display(text: str, limit: int = 4000) -> Tuple[str, bool]:
    """Return the tail of *text* and whether it was shortened for display."""
    if len(text) <= limit:
        return text, False
    return text[-limit:], True

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
