# -*- coding: utf-8 -*-
"""What the execution sandbox will do with a run, in plain facts.

The model cannot see the policy layer: it emits ``run_code`` and reads the
result. Without a statement of the profile in force it guesses - it writes
outside the project and reports a failure, or it believes a network call is
possible when the profile refuses one before the process starts.

This module turns the policy decision into a small dictionary and a block of
text. The decision itself stays in ``core.sandbox.policy``: a status that
disagreed with the policy would be worse than no status at all, so the
provider here is the one the policy really selects, not a re-implementation.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Sequence

from core.sandbox.contracts import isolation_rank
from core.sandbox.policy import plan_run
from core.sandbox.profiles import DEFAULT_PROFILE, OFF

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}


def reset_cache() -> None:
    """Forget the cached decisions (a test or a settings change calls this)."""
    with _lock:
        _cache.clear()


def describe(profile_name: str = "", providers: Optional[Sequence[Any]] = None,
             fresh: bool = False) -> Dict[str, Any]:
    """Return the decision the policy makes for *profile_name*.

    Args:
        profile_name: the profile an execution tool will ask for; empty means
            the policy default.
        providers: provider list to plan against; ``None`` means this
            machine's defaults. A test injects stubs here.
        fresh: ignore the cache. A provider probe spawns a process, so the
            answer is remembered per profile: availability does not change
            while the application runs, and the prompt is rebuilt often.

    Returns:
        A plain dictionary with the profile facts, the selected provider (or
        an empty name and a denial reason), and the limits the run gets.
    """
    name = str(profile_name or "") or DEFAULT_PROFILE
    cacheable = providers is None and not fresh
    if cacheable:
        with _lock:
            found = _cache.get(name)
        if found is not None:
            return dict(found)

    resolved = providers if providers is not None else _default_providers()
    profile, provider, denial = plan_run(name, resolved)

    facts: Dict[str, Any] = {
        "profile": name,
        "known": profile is not None,
        "isolation": profile.isolation if profile else "",
        "network": bool(profile.network) if profile else False,
        "writable": bool(profile.writable) if profile else False,
        "shared_tmp": bool(profile.shared_tmp) if profile else False,
        "provider": provider.name if provider else "",
        "provider_isolation": provider.isolation if provider else "",
        "capabilities": list(getattr(provider, "capabilities", ()) or ()),
        "limits": profile.limits.to_dict() if profile else {},
        "denied": denial is not None,
        "error": denial.error if denial is not None else "",
    }
    if cacheable:
        with _lock:
            _cache[name] = dict(facts)
    return facts


def _default_providers() -> Sequence[Any]:
    """Return this machine's providers, or an empty list when unavailable."""
    try:
        from core.sandbox.providers import default_providers

        return default_providers()
    except Exception:  # pragma: no cover - a broken backend is not a prompt error
        return []


def _is_isolated(facts: Dict[str, Any]) -> bool:
    """True when the selected provider really reaches an OS-level boundary."""
    return isolation_rank(str(facts.get("provider_isolation") or "")) >= isolation_rank("os")


def _guarantee_words(facts: Dict[str, Any]) -> str:
    """Return the short list of what the provider really enforces."""
    caps = facts.get("capabilities") or []
    words = []
    if "network-off" in caps:
        words.append("no network")
    if "write-confined" in caps:
        words.append("writes confined")
    return ", ".join(words) if words else "no isolation guarantees"


def format_lines(facts: Dict[str, Any], workspace: str = "") -> List[str]:
    """Return the prompt block describing *facts* as plain lines.

    Args:
        facts: the dictionary ``describe`` returned.
        workspace: the project folder the run receives, when one is selected.

    Returns:
        The lines of the block, heading first. An unknown profile states the
        refusal instead of describing permissions it does not have.
    """
    name = facts.get("profile") or DEFAULT_PROFILE
    lines = ["## Code execution sandbox", ""]

    if not facts.get("known"):
        lines.append("The configured profile `%s` does not exist. Every execution "
                     "tool call is refused with an explanation until a valid "
                     "profile is chosen; report the refusal instead of retrying." % name)
        return lines

    provider = facts.get("provider") or ""
    if facts.get("denied") or not provider:
        lines.append("The configured profile `%s` cannot run on this machine: no "
                     "available provider reaches the isolation it requires. "
                     "Execution tool calls are refused, not downgraded." % name)
        return lines

    isolation = facts.get("provider_isolation") or "none"
    lines.append("`run_code` and `run_test` execute under the `%s` profile "
                 "(provider `%s`, isolation `%s`)." % (name, provider, isolation))

    if not _is_isolated(facts):
        lines.append("The child runs with the rights of the platform process: "
                     "the environment is filtered and the resource limits apply, "
                     "but no operating-system boundary confines it.")
    else:
        guarantees = _guarantee_words(facts)
        lines.append("Guarantees enforced by the operating system: %s." % guarantees)

    if facts.get("writable"):
        target = "`%s`" % workspace if workspace else "the active project folder"
        lines.append("Writes are confined to %s and the child's own HOME and "
                     "TMPDIR; `.git` is writable except `hooks`, `config` and "
                     "`modules`, so `git add` and `git commit` work." % target)
    else:
        lines.append("The project is read-only for the child: a command that "
                     "modifies files is refused. Use the file tools for edits.")

    if not facts.get("network"):
        lines.append("The child has no network: package installs, HTTP calls and "
                     "remote git operations fail inside it.")
    elif facts.get("shared_tmp"):
        lines.append("The machine's shared temporary tree (`/tmp`) is writable, "
                     "because a backend may stage a file at a fixed path there.")

    limits = facts.get("limits") or {}
    if limits:
        lines.append("Limits: %s s, %s bytes of output, %s MB of memory." % (
            limits.get("timeout_s", "?"), limits.get("max_output_bytes", "?"),
            limits.get("memory_mb", "?")))

    lines.append("Credentials are unreadable from the child, and a run that is "
                 "already inside a sandbox inherits that sandbox instead of "
                 "starting a second one. A refused run returns `denied: true` "
                 "with the reason: report it, do not retry the same command.")
    return lines


def prompt_lines(cfg: dict, workspace: str = "") -> List[str]:
    """Return the status block for the config of one employee.

    The profile is a setting of the employee (``sandbox_profile``) and the
    toolbar switch may turn isolation off for the dialog (``chat_sandbox``).
    Reading both here keeps the prompt assembly free of policy details.

    Args:
        cfg: the orchestrator config.
        workspace: the project folder of the calling dialog.

    Returns:
        The block lines, or an empty list when the status is unavailable.
    """
    try:
        enabled = cfg.get("chat_sandbox", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in ("0", "false", "no", "off")
        profile = OFF if enabled is False else str(cfg.get("sandbox_profile") or "")
        text = block(profile or DEFAULT_PROFILE, workspace)
    except Exception:  # pragma: no cover - metadata must never break the prompt
        return []
    return text.splitlines() if text else []


def _text(facts: Dict[str, Any], workspace: str = "") -> str:
    """Return the block text for *facts*; empty when it cannot be built."""
    try:
        return "\n".join(format_lines(facts, workspace))
    except Exception:  # pragma: no cover - metadata must never break the prompt
        return ""


def block(profile_name: str = "", workspace: str = "",
          providers: Optional[Sequence[Any]] = None) -> str:
    """Return the whole prompt block for *profile_name* as one string."""
    try:
        return _text(describe(profile_name, providers), workspace)
    except Exception:  # pragma: no cover - metadata must never break the prompt
        return ""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
