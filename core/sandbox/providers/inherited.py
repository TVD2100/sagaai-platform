# -*- coding: utf-8 -*-
"""Provider for a run that already executes inside a sandbox.

No operating-system sandbox can be entered twice, so a provider that would
normally wrap the child is unavailable the moment the platform itself runs
confined: its probe cannot start a nested ``sandbox_apply``. Without a
backend for that situation the policy would deny every nested run with
"no provider reaches the required isolation", which is true but useless -
the run is already as isolated as this machine can make it.

This provider names that situation. It wraps nothing and claims only the
grants the ENCLOSING policy actually enforces, read from the marker the
enclosing provider stamped into the environment. The policy therefore keeps
its fail-closed behaviour: a profile whose requirements the enclosing
policy does not meet (an offline profile inside an online sandbox) finds no
provider and is refused, instead of silently losing the guarantee.
"""
from __future__ import annotations

from typing import Any, List, Sequence

from core.sandbox.contracts import ResourceLimits, SandboxProfile, SandboxRequest, SandboxResult
from core.sandbox.inheritance import EnclosingSandbox, compare, enclosing_sandbox
from core.sandbox.policy import SandboxProvider
from core.sandbox.providers._spawn import run_wrapped

#: Name reported in a result and stored in the journal.
PROVIDER_NAME = "inherited"


def capabilities_of(enclosing: EnclosingSandbox) -> Sequence[str]:
    """Return what the enclosing policy really guarantees to a child."""
    caps: List[str] = []
    if enclosing.writable:
        caps.append("write-confined")
    if not enclosing.network:
        caps.append("network-off")
    return tuple(caps)


class InheritedProvider(SandboxProvider):
    """Run a request under a sandbox the current process is already inside."""

    name = PROVIDER_NAME

    def __init__(self, subprocess_module: Any = None) -> None:
        """Store the spawn module; ``None`` means the real ``subprocess``."""
        self._subprocess = subprocess_module

    def _enclosing(self) -> Any:
        """Return the policy this process runs under, or None."""
        return enclosing_sandbox()

    @property
    def isolation(self) -> str:  # type: ignore[override]
        """The isolation of the enclosing policy, or ``none`` when absent."""
        found = self._enclosing()
        return found.isolation if found is not None else "none"

    @property
    def capabilities(self) -> Sequence[str]:  # type: ignore[override]
        """Only the grants the enclosing policy really enforces."""
        found = self._enclosing()
        return capabilities_of(found) if found is not None else ()

    def available(self) -> bool:
        """True exactly when this process already runs inside a sandbox."""
        return self._enclosing() is not None

    def run(self, request: SandboxRequest, profile: SandboxProfile,
            limits: ResourceLimits) -> SandboxResult:
        """Execute *request* without a second sandbox and report what applies."""
        found = self._enclosing()
        if found is None:
            return SandboxResult(ok=False, provider=self.name, isolation="none",
                                 error="This process does not run inside a sandbox.")
        verdict = compare(found, profile)
        if not verdict.ok:
            return SandboxResult(ok=False, provider=self.name,
                                 isolation=found.isolation, denied=True,
                                 error=verdict.error)
        # The marker is re-stamped, so a chain of children keeps reporting
        # the same enclosing policy instead of falling back to "none".
        from core.sandbox import inheritance

        marked = inheritance.mark_env(request.env, found.isolation, found.network,
                                      found.writable, found.profile)
        from dataclasses import replace

        marked_request = replace(request, env=marked)
        return run_wrapped(self.name, found.isolation, [], marked_request, limits,
                           subprocess_module=self._subprocess)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
