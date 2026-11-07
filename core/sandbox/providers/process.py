# -*- coding: utf-8 -*-
"""T0 provider: a child process with a built environment and hard limits.

This is the weakest backend the sandbox can choose. It provides no operating
system isolation, so a profile that requires ``os`` or stronger never
resolves to it - the policy layer refuses the run instead of pretending the
guarantee exists.

The provider wraps nothing: it hands the argv straight to the shared runner,
which owns the process lifecycle (bounded output, a full log, a killable
process group and the cancellation handle).
"""
from __future__ import annotations

from typing import Any

from core.sandbox.contracts import ResourceLimits, SandboxProfile, SandboxRequest, SandboxResult
from core.sandbox.policy import SandboxProvider
from core.sandbox.providers._spawn import run_wrapped

#: Name reported in a result and stored in the journal.
PROVIDER_NAME = "process"


class ProcessProvider(SandboxProvider):
    """Run a request as a child process without OS-level isolation."""

    name = PROVIDER_NAME
    #: The weakest useful level: the child is separated from the platform
    #: process (environment, session, working directory) but not from the
    #: rest of the machine. No guarantee beyond that is advertised.
    isolation = "process"
    capabilities = ()

    def __init__(self, subprocess_module: Any = None) -> None:
        """Store the spawn module; ``None`` means the real ``subprocess``."""
        self._subprocess = subprocess_module

    def available(self) -> bool:
        """A child process needs nothing beyond the interpreter itself."""
        return True

    def run(self, request: SandboxRequest, profile: SandboxProfile,
            limits: ResourceLimits) -> SandboxResult:
        """Execute *request* and translate the runner output into a result."""
        return run_wrapped(self.name, self.isolation, [], request, limits,
                           subprocess_module=self._subprocess)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
