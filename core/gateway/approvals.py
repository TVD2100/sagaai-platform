# -*- coding: utf-8 -*-
"""Approval tickets for a dangerous run that a transport drives.

An interactive page has a button: the click is the confirmation. A
TRANSPORT has no button, so it needs a value that stands for the decision
and cannot be forged by the program being confirmed.

The ticket is that value. It is issued by the gateway after a human
answered, bound to the exact operation by ``spec_hash`` (the tool plus its
arguments), scoped to one run and short-lived. The agent loop consumes it
immediately before re-dispatching the operation, and the operation itself
never sees it: a tool argument is emitted by the model, a ticket is written
by the platform.

Nothing here decides WHETHER an operation is dangerous - that is the tool
layer's assessment. This module only records the answer a human gave to
that assessment.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

#: How long an unused ticket stays valid, in seconds.
DEFAULT_TTL_SECONDS = 300
#: Upper bound of the TTL a caller may request.
MAX_TTL_SECONDS = 3600
#: Directory of tickets inside the run store.
APPROVALS_DIR = "approvals"

#: Decisions a ticket may carry.
ALLOW = "allow"
DENY = "deny"


def normalise_args(args: Any) -> Dict[str, Any]:
    """Return the arguments in the one form both sides compare.

    The parked call carries the arguments the model emitted and the tool
    layer sees them as named parameters, so an omitted optional key arrives
    as ``None`` on one side and is absent on the other. Neither is more
    correct, but a hash cannot tell that they describe the same call, so
    keys without a value are dropped before either side is fingerprinted.
    """
    if not isinstance(args, dict):
        return {}
    return {str(key): value for key, value in args.items() if value is not None}


def spec_hash(tool: str, args: Dict[str, Any]) -> str:
    """Return the fingerprint of ONE operation.

    The hash covers the tool name and the arguments with sorted keys, so the
    same call always produces the same value and a re-ordered dict does not
    look like a different operation. The operation is what the human saw in
    the dialog, so the hash is what makes their answer apply to it alone.
    """
    payload = {"tool": str(tool or ""), "args": normalise_args(args)}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _store_dir() -> str:
    """Return the directory of tickets; it lives with the run records."""
    from core.gateway import runs

    root = runs.store_root()
    return os.path.join(root, APPROVALS_DIR) if root else ""


def _safe(run_id: str) -> str:
    """Return the file-safe form of a run id."""
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_"
                   for ch in str(run_id or ""))[:120]


def issue(run_id: str, tool: str, args: Dict[str, Any], decision: str = ALLOW,
          ttl_seconds: int = 0) -> Dict[str, Any]:
    """Write a ticket for one operation and return its public view.

    A ticket replaces the previous one of the same run: a second decision
    means the human changed their mind, and keeping both would leave the
    earlier answer usable after it was revoked.
    """
    directory = _store_dir()
    if not directory:
        return {"ok": False, "error": "The run store is unavailable."}
    rid = str(run_id or "").strip()
    if not rid:
        return {"ok": False, "error": "run id is required"}
    chosen = str(decision or ALLOW).strip().lower()
    if chosen not in (ALLOW, DENY):
        return {"ok": False, "error": f"Unknown decision '{decision}'."}
    ttl = int(ttl_seconds or DEFAULT_TTL_SECONDS)
    ttl = max(1, min(MAX_TTL_SECONDS, ttl))
    record = {
        "run_id": rid,
        "tool": str(tool or ""),
        "spec_hash": spec_hash(tool, args or {}),
        "args": normalise_args(args or {}),
        "decision": chosen,
        "issued_at": time.time(),
        "expires_at": time.time() + ttl,
    }
    try:
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, _safe(rid) + ".json")
        temporary = target + ".tmp%s" % os.getpid()
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
        os.replace(temporary, target)
    except OSError as exc:
        return {"ok": False, "error": f"The ticket could not be stored: {exc}"}
    return {"ok": True, "run_id": rid, "decision": chosen,
            "spec_hash": record["spec_hash"], "expires_at": record["expires_at"],
            "ttl_seconds": ttl}


def peek(run_id: str) -> Optional[Dict[str, Any]]:
    """Return the live ticket of a run, or None (expired counts as None)."""
    directory = _store_dir()
    if not directory:
        return None
    path = os.path.join(directory, _safe(run_id) + ".json")
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    try:
        if float(record.get("expires_at") or 0) <= time.time():
            _remove(path)
            return None
    except (TypeError, ValueError):
        _remove(path)
        return None
    return record


def consume(run_id: str, tool: str, args: Dict[str, Any]) -> str:
    """Take the decision of a run for ONE operation and delete the ticket.

    Returns ``allow``, ``deny`` or an empty string. An empty string covers
    every reason the answer does not apply: no ticket, an expired one, or
    one issued for a DIFFERENT operation. A ticket that does not match the
    operation being re-dispatched is discarded rather than kept: a human
    answered about one thing, and letting it authorise another is exactly
    the substitution the ticket exists to prevent.
    """
    record = peek(run_id)
    if record is None:
        return ""
    expected = spec_hash(tool, args or {})
    directory = _store_dir()
    path = os.path.join(directory, _safe(run_id) + ".json") if directory else ""
    if str(record.get("spec_hash") or "") != expected:
        if path:
            _remove(path)
        return ""
    decision = str(record.get("decision") or "")
    if path:
        _remove(path)
    return decision if decision in (ALLOW, DENY) else ""


def cancel(run_id: str) -> bool:
    """Drop the ticket of a run; return True when one was there."""
    directory = _store_dir()
    if not directory:
        return False
    path = os.path.join(directory, _safe(run_id) + ".json")
    existed = os.path.exists(path)
    if existed:
        _remove(path)
    return existed


def _remove(path: str) -> None:
    """Delete a ticket file, ignoring a race with another reader."""
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover - a missing ticket is not an error
        pass

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
