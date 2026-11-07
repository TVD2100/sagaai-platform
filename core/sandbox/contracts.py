# -*- coding: utf-8 -*-
"""Data contracts of the execution sandbox.

A run is described BEFORE anything is executed: the policy layer reads the
request, picks a profile, and only then does a provider run it. Keeping the
description separate from the execution is what makes a run auditable - the
journal stores the request that was planned, not a reconstruction of what
the process happened to do.

Nothing here touches the operating system: these are plain values, so the
policy can be tested without spawning a process.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

#: Isolation a provider can offer, weakest first. A profile states the level
#: it REQUIRES; a provider only satisfies the requirement when its own level
#: is at least as strong.
ISOLATION_ORDER: Tuple[str, ...] = ("none", "process", "os", "container", "remote")


def isolation_rank(level: str) -> int:
    """Return the position of *level* in the strength order, -1 if unknown."""
    try:
        return ISOLATION_ORDER.index(str(level))
    except ValueError:
        return -1


def satisfies(provider_level: str, required_level: str) -> bool:
    """True when a provider at *provider_level* meets *required_level*.

    An unknown level on either side never satisfies anything: a typo must
    produce a refusal, not a silent downgrade of the guarantee.
    """
    offered = isolation_rank(provider_level)
    needed = isolation_rank(required_level)
    return offered >= 0 and needed >= 0 and offered >= needed


@dataclass(frozen=True)
class ResourceLimits:
    """Hard ceilings applied to one run, enforced by the provider."""

    timeout_s: int = 300
    memory_mb: int = 2048
    cpu_seconds: int = 300
    max_processes: int = 64
    max_output_bytes: int = 64_000
    #: Largest file the child may create. A single runaway write must not
    #: fill the disk of the machine the platform runs on.
    max_file_bytes: int = 256 * 1024 * 1024

    def to_dict(self) -> Dict[str, Any]:
        """Return the limits as a plain mapping (journal and UI use it)."""
        return {
            "timeout_s": self.timeout_s,
            "memory_mb": self.memory_mb,
            "cpu_seconds": self.cpu_seconds,
            "max_processes": self.max_processes,
            "max_output_bytes": self.max_output_bytes,
            "max_file_bytes": self.max_file_bytes,
        }


@dataclass(frozen=True)
class SandboxProfile:
    """A named set of permissions and limits a run may execute under."""

    name: str
    isolation: str
    network: bool = False
    writable: bool = False
    #: Strongest isolation this profile may be executed under. Empty means
    #: the profile accepts anything that reaches ``isolation``. A profile
    #: that exists to run code WITHOUT isolation sets it below the levels a
    #: stronger provider could supply - otherwise the strongest available
    #: backend would be picked and the profile would quietly gain a
    #: guarantee it was never meant to have.
    max_isolation: str = ""
    description: str = ""
    #: Capabilities a provider must declare to honour this profile. The
    #: isolation level alone is not enough: a provider can be at 'os' and
    #: still leave the network reachable, and then a profile that promises
    #: "no network" would be silently weakened.
    requires: Tuple[str, ...] = ()
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def to_dict(self) -> Dict[str, Any]:
        """Return the profile as a plain mapping."""
        return {
            "name": self.name,
            "isolation": self.isolation,
            "max_isolation": self.max_isolation,
            "network": self.network,
            "writable": self.writable,
            "requires": list(self.requires),
            "description": self.description,
            "limits": self.limits.to_dict(),
        }


@dataclass(frozen=True)
class SandboxRequest:
    """Everything a provider needs to run one piece of code.

    ``code_hash`` identifies the analysed text, so the journal can tell two
    runs of the same tool apart without storing the program itself.
    """

    tool: str
    argv: Tuple[str, ...]
    cwd: str
    env: Dict[str, str]
    code_hash: str = ""
    profile: str = ""
    thread_id: str = ""
    workspace: str = ""

    def fingerprint(self) -> str:
        """Return a stable hash of the executable part of the request.

        The environment is included by NAME only: its values carry paths and
        machine details, and a changed temporary directory must not look like
        a different program.
        """
        payload = {
            "tool": self.tool,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "env_keys": sorted(self.env),
            "code_hash": self.code_hash,
            "profile": self.profile,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class SandboxResult:
    """Outcome of one run, in the shape the journal and the tool layer read."""

    ok: bool = False
    provider: str = ""
    isolation: str = "none"
    run_id: str = ""
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    #: True when the run was stopped from outside while it was executing.
    #: Unlike ``timed_out`` this is a decision, not a limit of the profile.
    cancelled: bool = False
    truncated: bool = False
    duration_ms: int = 0
    log_path: str = ""
    denied: bool = False
    error: str = ""
    #: The limits the run actually executed under (profile values after any
    #: caller-side tightening); the journal stores them with the outcome.
    limits_used: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return the result as the mapping the tool layer returns."""
        payload: Dict[str, Any] = {
            "ok": self.ok,
            "provider": self.provider,
            "isolation": self.isolation,
            "run_id": self.run_id,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "log_path": self.log_path,
        }
        if self.denied:
            payload["denied"] = True
        if self.error:
            payload["error"] = self.error
        return payload

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
