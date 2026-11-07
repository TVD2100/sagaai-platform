# -*- coding: utf-8 -*-
"""Decide whether and where a run may execute.

The policy is the only place that can refuse a run before it starts, and it
refuses in two situations that the rest of the platform must not be able to
work around:

* the profile is unknown - a typo must never fall back to a permissive
default;
* no available provider reaches the isolation the profile requires - the
    run is denied instead of being silently downgraded (fail-closed).

A denial is a normal result, not an exception: the tool layer turns it into
a tool result, so the model learns why nothing ran.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from core.sandbox.contracts import (
    SandboxProfile,
    SandboxResult,
    isolation_rank,
    satisfies,
)
from core.sandbox.profiles import get_profile


class SandboxProvider:
    """Minimal provider protocol shared by every isolation backend.

    A provider declares what it can do (``name``, ``isolation``) and runs a
    prepared request. It never inspects the program: analysis belongs to the
    tool layer, isolation to the provider.
    """

    name = ""
    isolation = "none"
    #: What this provider really enforces, beyond the isolation level:
    #: ``network-off`` and ``write-confined``. A provider that cannot state
    #: a capability does not have it, so a profile needing one is refused.
    capabilities: Sequence[str] = ()

    def available(self) -> bool:
        """Return True when this provider can run on the current machine."""
        return False

    def run(self, request, profile: SandboxProfile, limits):  # pragma: no cover
        """Execute one request under *profile* and return a SandboxResult."""
        raise NotImplementedError


def _deny(error: str, provider: str = "", isolation: str = "none") -> SandboxResult:
    """Build the result of a run that was refused before execution."""
    return SandboxResult(ok=False, provider=provider, isolation=isolation,
                         denied=True, error=error)


def _ordered(providers: Sequence[SandboxProvider]) -> List[SandboxProvider]:
    """Return the providers strongest first, so the best option wins.

    Ordering by the declared isolation keeps the choice deterministic: the
    same machine always picks the same provider for the same profile.
    """
    from core.sandbox.contracts import isolation_rank

    return sorted(providers, key=lambda item: isolation_rank(item.isolation),
                  reverse=True)


def within_isolation_ceiling(provider_level: str, max_level: str) -> bool:
    """True when *provider_level* does not exceed the *max_level* ceiling.

    An empty ceiling means "no ceiling": the profile states a minimum only.
    A ceiling that names an unknown level is treated as no ceiling - the
    minimum requirement still applies, so an unknown name can never hand
    out a stronger guarantee than the profile asked for.

    Public because the settings panel reports the provider a profile would
    actually run under: a panel that ignored the ceiling would name a
    backend the policy then refuses to use.
    """
    wanted = str(max_level or "")
    if not wanted:
        return True
    ceiling = isolation_rank(wanted)
    offered = isolation_rank(provider_level)
    if ceiling < 0 or offered < 0:
        return True
    return offered <= ceiling


def select_provider(profile: SandboxProfile,
                    providers: Iterable[SandboxProvider]) -> Optional[SandboxProvider]:
    """Return the strongest available provider that meets *profile*.

    ``None`` means the requirement cannot be met on this machine; the caller
    must deny the run, never fall back to a weaker provider.

    ``profile.max_isolation`` caps the choice from above: a profile that
    exists to run code WITHOUT isolation must not be silently upgraded to a
    sandboxing backend just because this machine happens to have one.
    """
    for provider in _ordered(list(providers)):
        if not satisfies(provider.isolation, profile.isolation):
            continue
        if not within_isolation_ceiling(provider.isolation, profile.max_isolation):
            continue
        offered = set(getattr(provider, "capabilities", ()) or ())
        if not set(profile.requires).issubset(offered):
            continue
        try:
            if provider.available():
                return provider
        except Exception:  # pragma: no cover - a probe must not break the gate
            continue
    return None


def resolve_profile(name: str) -> Optional[SandboxProfile]:
    """Return the profile called *name*, or None when it is not defined."""
    return get_profile(name)


def plan_run(profile_name: str, providers: Iterable[SandboxProvider]):
    """Resolve a profile and pick a provider for it.

    Returns:
        ``(profile, provider, denial)``. On success *denial* is None; on
        refusal *provider* is None and *denial* is the result to return.
    """
    profile = resolve_profile(profile_name)
    if profile is None:
        return None, None, _deny(
            f"Unknown sandbox profile '{profile_name}'. The run was not "
            "started: a profile name is never guessed."
        )
    provider = select_provider(profile, providers)
    if provider is None:
        return profile, None, _deny(
            "No available sandbox provider reaches the isolation this profile "
            f"requires ({profile.isolation}). The run was not started: "
            "running it without the guarantee would silently weaken it.",
            isolation="none",
        )
    return profile, provider, None


def _tighten(limits, override):
    """Return the profile limits with any *override* applied where stricter.

    Every field is a ceiling, so an override only lowers it. A larger value
    is ignored rather than rejected: the caller asked for a bound it does
    not control, and the profile is the authority on what it may have.
    """
    if override is None:
        return limits
    from dataclasses import replace

    changes = {}
    for name in ("timeout_s", "memory_mb", "cpu_seconds", "max_processes",
                 "max_output_bytes", "max_file_bytes"):
        proposed = getattr(override, name, None)
        current = getattr(limits, name)
        if isinstance(proposed, int) and 0 < proposed < current:
            changes[name] = proposed
        elif isinstance(proposed, int) and proposed > current:
            changes[name] = current
    return replace(limits, **changes) if changes else limits


class SandboxRegistry:
    """The providers this process may use, strongest first.

    The registry is built once per call from the caller's list, so a test can
    inject a stub provider without touching global state - and a production
    call cannot accidentally keep a provider from a previous dialog.
    """

    def __init__(self, providers: Iterable[SandboxProvider] = ()):
        self._providers: List[SandboxProvider] = list(providers)

    def add(self, provider: SandboxProvider) -> "SandboxRegistry":
        """Register one provider and return self (chaining)."""
        self._providers.append(provider)
        return self

    def providers(self) -> List[SandboxProvider]:
        """Return the registered providers, strongest first."""
        return _ordered(self._providers)

    def run(self, profile_name: str, request, limits_override=None) -> SandboxResult:
        """Plan and execute one request under *profile_name*.

        A refusal returns a denied result; nothing is executed.
        ``limits_override`` may only TIGHTEN the profile: a caller cannot use
        it to grant itself a longer timeout or a larger output bound than the
        profile allows.
        """
        profile, provider, denial = plan_run(profile_name, self.providers())
        if denial is not None:
            return denial
        assert profile is not None and provider is not None
        limits = _tighten(profile.limits, limits_override)
        try:
            result = provider.run(request, profile, limits)
        except Exception as exc:  # pragma: no cover - defensive boundary
            return SandboxResult(ok=False, provider=provider.name,
                                 isolation=provider.isolation,
                                 error=f"{type(exc).__name__}: {exc}",
                                 limits_used=limits.to_dict())
        # The limits are stamped on the result AFTER the provider ran: a
        # provider cannot report limits other than the ones it was given.
        result.limits_used = limits.to_dict()
        return result

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
