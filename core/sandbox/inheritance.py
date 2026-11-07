# -*- coding: utf-8 -*-
"""Detect that a run already executes inside a sandbox.

An operating-system sandbox cannot be entered twice. macOS refuses a nested
``sandbox_apply`` outright - the child dies with ``Operation not permitted``
and the message names the wrapper, not the problem, so the failure reads as a
broken tool rather than an unsupported nesting. The enclosing sandbox is also
the only policy the kernel can enforce at that point: a second profile would
have nothing left to confine.

A provider therefore stamps the grants of its own policy into the child
environment. A provider that finds the stamp knows the child is already
confined and must not wrap it again; what it may do then follows from
comparing the two grants:

* the request needs MORE than the enclosing policy gives (a write where the
  parent is read-only, or a network call where the parent is offline) - the
  run is refused with an explanation, because it could only fail later with
  an error that hides the cause;
* otherwise the run proceeds un-wrapped and reports the isolation of the
  enclosing policy, so nothing claims a guarantee the kernel is not
  applying.

The marker is a plain value, so it survives a chain of children; a malformed
value is treated as absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from core.sandbox.contracts import SandboxProfile, satisfies

#: Environment variable that carries the grants of the enclosing policy.
SANDBOX_MARKER = "SAGAAI_SANDBOX_GRANTS"

#: Prefix of the marker value; a newer format must be ignored, not guessed.
_MARKER_VERSION = "v1"


@dataclass(frozen=True)
class EnclosingSandbox:
    """The policy a run is already executing under."""

    isolation: str
    network: bool
    writable: bool
    profile: str = ""


def format_marker(isolation: str, network: bool, writable: bool,
                  profile: str = "") -> str:
    """Return the marker value describing the policy of a spawned child."""
    return ";".join((
        _MARKER_VERSION,
        "isolation=%s" % (isolation or "none"),
        "network=%d" % (1 if network else 0),
        "writable=%d" % (1 if writable else 0),
        "profile=%s" % (profile or ""),
    ))


def parse_marker(value: Any) -> Optional[EnclosingSandbox]:
    """Return the policy described by *value*, or None when it is unusable."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = [item.strip() for item in raw.split(";") if item.strip()]
    if not parts or parts[0] != _MARKER_VERSION:
        return None
    fields: Dict[str, str] = {}
    for item in parts[1:]:
        if "=" not in item:
            return None
        key, _, val = item.partition("=")
        fields[key.strip()] = val.strip()
    isolation = fields.get("isolation", "")
    if not isolation:
        return None
    return EnclosingSandbox(
        isolation=isolation,
        network=fields.get("network", "0") == "1",
        writable=fields.get("writable", "0") == "1",
        profile=fields.get("profile", ""),
    )


def enclosing_sandbox(env: Optional[Mapping[str, str]] = None) -> Optional[EnclosingSandbox]:
    """Return the policy this run already executes under, or None.

    The child's own environment is consulted first, then the environment of
    the current process: a platform started inside a sandbox carries the
    marker itself, and the run it starts must not try to nest.
    """
    found = parse_marker((env or {}).get(SANDBOX_MARKER))
    if found is not None:
        return found
    import os

    return parse_marker(os.environ.get(SANDBOX_MARKER))


@dataclass(frozen=True)
class Verdict:
    """Whether an already-confined run may proceed, and why not."""

    ok: bool
    error: str = ""


def mark_env(env: Mapping[str, str], isolation: str, network: bool,
             writable: bool, profile: str = "") -> Dict[str, str]:
    """Return a copy of *env* carrying the grants a child will run under.

    The environment of a request is a plain dict that the provider may copy
    freely; the platform's own environment is never mutated, so two runs of
    the same dialog cannot inherit each other's grant.
    """
    marked = dict(env or {})
    marked[SANDBOX_MARKER] = format_marker(isolation, network, writable, profile)
    return marked


def with_marker(request, profile: SandboxProfile):
    """Return *request* with the marker of *profile* added to its environment.

    The provider calls this right before spawning a wrapped child, so the
    next nested run finds the grants of the policy that is actually applied
    rather than the one it merely asked for.
    """
    from dataclasses import replace

    env = mark_env(request.env, profile.isolation, profile.network,
                   profile.writable, profile.name)
    return replace(request, env=env)


def compare(enclosing: EnclosingSandbox, profile: SandboxProfile) -> Verdict:
    """Decide whether *profile* may run inside *enclosing* without wrapping.

    Only the direction that cannot work is refused. A request for less than
    the enclosing policy grants is allowed: the enclosing policy is what the
    kernel enforces, and the result reports that policy rather than the
    narrower one that was asked for.
    """
    if not satisfies(enclosing.isolation, profile.isolation):
        return Verdict(False, (
            "This run asks for %s isolation but already executes inside %s "
            "isolation, and an operating-system sandbox cannot be entered "
            "twice. Run it from a process that is not itself sandboxed."
            % (profile.isolation, enclosing.isolation)
        ))
    if profile.writable and not enclosing.writable:
        return Verdict(False, (
            "This run asks to write but already executes inside a read-only "
            "sandbox: the enclosing policy does not allow writes and a "
            "nested sandbox is not possible. Run it from a process that is "
            "not itself sandboxed, or drop the write."
        ))
    if profile.network and not enclosing.network:
        return Verdict(False, (
            "This run asks for network access but already executes inside an "
            "offline sandbox: the enclosing policy has no network and a "
            "nested sandbox is not possible. Run it from a process that is "
            "not itself sandboxed."
        ))
    return Verdict(True)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
