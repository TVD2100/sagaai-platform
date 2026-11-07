# -*- coding: utf-8 -*-
"""Named execution profiles.

A profile is the only place where a permission is granted: the tool layer
names a profile, the policy resolves it, and a provider either honours it or
refuses. There is no implicit "default allows everything" - an unknown name
resolves to nothing and the run is denied.

``legacy_process`` reproduces the behaviour the execution tools had before
the sandbox existed (a child process with a built environment, no OS
isolation). It exists so the migration can be staged: a caller that asks for
it gets the old behaviour and the journal records that no isolation was
claimed.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from core.sandbox.contracts import ResourceLimits, SandboxProfile

#: Read-only inspection of the workspace; no writes, no network.
READ_ONLY = "read_only"
#: The default for generated code: writes inside the workspace, no network.
WORKSPACE_WRITE = "workspace_write"
#: Workspace writes plus outbound network (package installs, API probes).
NETWORK = "network"
#: No OS isolation, only the built environment and the resource limits.
LEGACY_PROCESS = "legacy_process"

DEFAULT_PROFILE = WORKSPACE_WRITE

_PROFILES: Dict[str, SandboxProfile] = {
    READ_ONLY: SandboxProfile(
        name=READ_ONLY,
        isolation="os",
        network=False,
        writable=False,
        requires=("network-off", "write-confined"),
        description="Read-only inspection: the child cannot write to the project.",
        limits=ResourceLimits(timeout_s=120),
    ),
    WORKSPACE_WRITE: SandboxProfile(
        name=WORKSPACE_WRITE,
        isolation="os",
        network=False,
        writable=True,
        requires=("network-off", "write-confined"),
        description="Generated code: writes inside the workspace, no network.",
        limits=ResourceLimits(timeout_s=300),
    ),
    NETWORK: SandboxProfile(
        name=NETWORK,
        isolation="os",
        network=True,
        writable=True,
        requires=("write-confined",),
        description="Workspace writes and outbound network access.",
        limits=ResourceLimits(timeout_s=300),
    ),
    LEGACY_PROCESS: SandboxProfile(
        name=LEGACY_PROCESS,
        isolation="process",
        network=True,
        writable=True,
        description="Child process without OS isolation: built environment and limits only.",
        limits=ResourceLimits(timeout_s=300),
    ),
}


#: Names in the order the UI lists them.
PROFILE_ORDER: Tuple[str, ...] = (READ_ONLY, WORKSPACE_WRITE, NETWORK, LEGACY_PROCESS)


def get_profile(name: str) -> Optional[SandboxProfile]:
    """Return the profile called *name*, or None when it does not exist."""
    return _PROFILES.get(str(name or ""))


def all_profiles() -> Tuple[SandboxProfile, ...]:
    """Return every profile, in the order the UI presents them."""
    return tuple(_PROFILES[name] for name in PROFILE_ORDER)


def profile_names() -> Tuple[str, ...]:
    """Return the names of the known profiles."""
    return PROFILE_ORDER

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
