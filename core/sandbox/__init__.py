# -*- coding: utf-8 -*-
"""Execution sandbox of the platform.

The package separates three questions that used to be tangled in the
execution tools:

* ``policy`` - may this run happen at all, and under which isolation
  (fail-closed: an unavailable guarantee denies the run);
* ``providers`` - how the run is isolated and executed;
* ``journal`` - what was planned and what came out of it.

The separation exists so a guarantee can be proved instead of asserted: the
tool layer no longer contains the decision, and a provider cannot widen the
permissions the policy granted.
"""
from __future__ import annotations

from typing import List

from core.sandbox.contracts import (
    ISOLATION_ORDER,
    ResourceLimits,
    SandboxProfile,
    SandboxRequest,
    SandboxResult,
    isolation_rank,
    satisfies,
)
from core.sandbox.policy import SandboxProvider, SandboxRegistry, plan_run, select_provider
from core.sandbox.profiles import (
    DEFAULT_PROFILE,
    LEGACY_PROCESS,
    NETWORK,
    READ_ONLY,
    WORKSPACE_WRITE,
    all_profiles,
    get_profile,
    profile_names,
)


def default_registry() -> SandboxRegistry:
    """Return a registry holding every provider this build ships."""
    from core.sandbox.providers import default_providers

    return SandboxRegistry(default_providers())


__all__: List[str] = [
    "DEFAULT_PROFILE", "ISOLATION_ORDER", "LEGACY_PROCESS", "NETWORK",
    "READ_ONLY", "WORKSPACE_WRITE", "ResourceLimits", "SandboxProfile",
    "SandboxProvider", "SandboxRegistry", "SandboxRequest", "SandboxResult",
    "all_profiles", "default_registry", "get_profile", "isolation_rank",
    "plan_run", "profile_names", "satisfies", "select_provider",
]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
