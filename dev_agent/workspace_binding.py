# DevAgent workspace binding - thread isolation layer.
#
# SagaAI runs every orchestrator inside ONE Python process (Streamlit).
# DevAgent's workspace used to be a set of module-level globals in
# dev_agent.config (PROJECT_ROOT, TARGET_FILE, ACTIVE_THREAD_ID, ...), so
# when two dialogs worked in parallel in two browser windows, switching the
# workspace in window A silently repointed window B at A's folder. This
# module removes that leakage.
#
# Design ("practical isolation", not a full WorkspaceContext rewrite):
#
#   1. A process-wide REGISTRY maps thread_id -> workspace state under an
#      RLock. Every dialog thread owns its own workspace/target_file.
#
#   2. DevAgent dispatchers register their thread id via ``set_thread_id``.
#      During a tool dispatch the executor runs under ``thread_context()``:
#      the config globals are swapped to the thread's snapshot for the
#      DURATION of one tool call and restored right after. The RLock is
#      held for the whole block, so two parallel tool calls from different
#      threads can never see each other's paths. Tradeoff accepted:
#      long tool calls (run_code up to 3 min) serialize other threads'
#      tool dispatches while they run - correctness over concurrency here.
#
#   3. Persistence is delegated to the existing core thread-meta layer
#      (core.threads_devagent.save_thread_workspace / load_thread_meta),
#      so bindings survive restarts and thread reopen.
#
#   4. Untrusted "no thread" callers (tests, scripts, single-file mode)
#      keep the legacy behavior: whatever config.PROJECT_ROOT currently
#      holds. The registry only adds isolation, it never breaks fallback.

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from . import config

# ─── Registry ─────────────────────────────────────────────────────────────────
_BINDING_LOCK: threading.RLock = threading.RLock()

# thread_id -> {"workspace": str|None, "target_file": str|None}
_REGISTRY: Dict[str, Dict[str, Optional[str]]] = {}


def _norm_tid(thread_id: str) -> str:
    """Normalize a thread id to the registry key form."""
    return (thread_id or "").strip()


def _state_from_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Build a registry state dict from thread-meta DB data."""
    meta = meta or {}
    return {
        "workspace": meta.get("workspace") or None,
        "target_file": meta.get("target_file") or None,
    }


def register_thread(thread_id: str,
                    workspace: Optional[str] = None,
                    target_file: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Create/update the in-memory binding for a thread.

    Called by the UI when a dialog is created/opened and by the dispatcher
    after every workspace switch. Persistence to the DB is the caller's
    responsibility (core.threads_devagent.save_thread_workspace), just like
    it already is in ui/pages/orchestrator.py.
    """
    tid = _norm_tid(thread_id)
    if not tid:
        return {"workspace": None, "target_file": None}
    with _BINDING_LOCK:
        state = _state_from_meta({"workspace": workspace, "target_file": target_file})
        _REGISTRY[tid] = state
        return dict(state)


def get_thread_state(thread_id: str) -> Optional[Dict[str, Optional[str]]]:
    """Return the bound workspace state for a thread, or None."""
    tid = _norm_tid(thread_id)
    if not tid:
        return None
    with _BINDING_LOCK:
        state = _REGISTRY.get(tid)
        return dict(state) if state is not None else None


def get_thread_workspace(thread_id: str) -> Optional[str]:
    """Return the bound workspace root for a thread, or None."""
    state = get_thread_state(thread_id)
    if state is None:
        return None
    return state.get("workspace")


def clear_thread(thread_id: str) -> None:
    """Forget a thread binding (used on dialog reset/delete)."""
    tid = _norm_tid(thread_id)
    if not tid:
        return
    with _BINDING_LOCK:
        _REGISTRY.pop(tid, None)


def has_thread(thread_id: str) -> bool:
    """True when a binding exists for this thread."""
    return get_thread_state(thread_id) is not None


def sync_lock() -> threading.RLock:
    """Expose the shared RLock so workspace_tools serializes direct calls
    (UI picker path) with dispatch-time state swaps."""
    return _BINDING_LOCK


def sync_registry_from_config(thread_id: str) -> Dict[str, Optional[str]]:
    """Update the registry entry for a thread from the live config globals.

    Called after a successful workspace switch inside a bound dialog, so the
    NEXT dispatch of this thread applies the NEW state. No-op for empty ids.
    """
    tid = _norm_tid(thread_id)
    if not tid:
        return {"workspace": None, "target_file": None}
    with _BINDING_LOCK:
        state = {
            "workspace": str(config.PROJECT_ROOT),
            "target_file": config.TARGET_FILE or None,
        }
        _REGISTRY[tid] = state
        return dict(state)


# ─── Config swap helpers ──────────────────────────────────────────────────────

def snapshot_config() -> Dict[str, Any]:
    """Capture the full mutable DevAgent path state (under the lock)."""
    with _BINDING_LOCK:
        return config.snapshot_state()


def restore_config_state(state: Optional[Dict[str, Any]]) -> None:
    """Restore a snapshot previously returned by snapshot_config()."""
    if state is None:
        return
    with _BINDING_LOCK:
        config.restore_state(state)


def _apply_state_unlocked(tid: str, state: Dict[str, Optional[str]]) -> None:
    """Apply a bound state to the live config globals. Lock MUST be held."""
    root = state.get("workspace")
    if root:
        config.apply_paths(root, target_file=state.get("target_file"),
                           thread_id=tid, create_dirs=False)
    else:
        # Bound but workspace-less: keep the current root yet pin the
        # thread id so per-thread journals still go to the right file.
        config.ACTIVE_THREAD_ID = tid


def ensure_thread_active(thread_id: str) -> bool:
    """Apply the thread's bound state to the live config once (if bound).

    Unlike thread_context this does NOT restore afterwards: the agent loop
    uses it so journal scaffolding and thread-file tools outside a dispatch
    also target THIS thread. Returns True when the thread had a binding and
    config now reflects it; False means the caller should fall back to
    setting config.ACTIVE_THREAD_ID itself (legacy behavior).
    """
    tid = _norm_tid(thread_id)
    if not tid:
        return False
    with _BINDING_LOCK:
        state = _REGISTRY.get(tid)
        if state is None:
            return False
        _apply_state_unlocked(tid, state)
        return True


class thread_context:
    """Context manager: run a block with the thread's config state active.

    Acquires the binding RLock for the WHOLE block: snapshot config, apply
    the thread's bound state, yield, restore, release. This serializes tool
    dispatches of different dialogs so a call can never observe another
    thread's paths.

    Usage::

        with thread_context(thread_id) as engaged:
            if engaged:
                return core.dispatch(...)
            return fallback_dispatch(...)
    """

    __slots__ = ("thread_id", "_snapshot", "engaged")

    def __init__(self, thread_id: str):
        self.thread_id = _norm_tid(thread_id)
        self._snapshot: Optional[Dict[str, Any]] = None
        self.engaged = False

    def __enter__(self) -> "thread_context":
        _BINDING_LOCK.acquire()
        try:
            if not self.thread_id:
                return self
            state = _REGISTRY.get(self.thread_id)
            if state is None:
                return self
            self._snapshot = config.snapshot_state()
            _apply_state_unlocked(self.thread_id, state)
            self.engaged = True
            return self
        except Exception:
            # Never let a swap failure deadlock the dispatcher.
            _BINDING_LOCK.release()
            self._snapshot = None
            self.engaged = False
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._snapshot is not None:
                try:
                    config.restore_state(self._snapshot)
                except Exception:
                    pass
        finally:
            self._snapshot = None
            self.engaged = False
            _BINDING_LOCK.release()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
