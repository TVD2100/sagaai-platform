"""tests/test_sandbox_off_profile.py - the switchable "sandbox off" mode.

The toolbar checkbox lets the user run generated code without isolation.
Two properties make that choice honest, and both are checked here:

* clearing the checkbox really disables isolation - the policy must not
  silently upgrade the run to a sandboxing backend just because the machine
  has one;
* turning isolation off does NOT turn off the resource limits or the built
  environment: a runaway child would otherwise take the application down.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sandbox import plan_run, profiles  # noqa: E402
from core.sandbox.contracts import SandboxProfile  # noqa: E402
from core.sandbox.policy import (  # noqa: E402
    SandboxRegistry,
    select_provider,
    within_isolation_ceiling,
)


class _StubProvider:
    """A provider that is available, declares capabilities and never runs."""

    def __init__(self, name: str, isolation: str, capabilities=(), ready: bool = True):
        self.name = name
        self.isolation = isolation
        self.capabilities = tuple(capabilities)
        self._ready = ready

    def available(self) -> bool:
        return self._ready

    def run(self, request, profile, limits):  # pragma: no cover - must not happen
        raise AssertionError("the policy was expected to refuse, not to run")


#: A machine that can isolate: the strongest backend is reachable.
_ISOLATING_BOX = (
    _StubProvider("seatbelt", "os", ("network-off", "write-confined")),
    _StubProvider("process", "process"),
)


# ── the profile itself ───────────────────────────────────────────────────────


def test_off_profile_declares_no_isolation_and_a_ceiling():
    """The off profile asks for nothing and refuses to be upgraded."""
    profile = profiles.get_profile(profiles.OFF)

    assert profile is not None
    assert profile.name == profiles.OFF
    assert profile.isolation == "none"
    assert profile.max_isolation == "process"
    assert profile.network is True
    assert profile.writable is True


def test_off_profile_keeps_the_resource_limits():
    """Switching isolation off must not remove the ceilings."""
    profile = profiles.get_profile(profiles.OFF)

    assert profile.limits.timeout_s > 0
    assert profile.limits.max_output_bytes > 0
    assert profile.limits.max_file_bytes > 0


def test_off_is_listed_after_the_isolating_profiles():
    """The selector lists off last: it is the exception, not the default."""
    names = profiles.profile_names()

    assert profiles.OFF in names
    assert names[-1] == profiles.OFF
    assert profiles.DEFAULT_PROFILE != profiles.OFF


def test_profile_dict_carries_the_ceiling():
    """The journal and the settings panel read the ceiling from to_dict."""
    payload = profiles.get_profile(profiles.OFF).to_dict()

    assert payload["max_isolation"] == "process"
    assert payload["isolation"] == "none"


# ── the policy honours the ceiling ───────────────────────────────────────────


def test_off_profile_selects_the_plain_process_provider():
    """On a machine that CAN isolate, off still runs without isolation."""
    profile = profiles.get_profile(profiles.OFF)

    provider = select_provider(profile, _ISOLATING_BOX)

    assert provider is not None
    assert provider.name == "process"


def test_the_same_machine_still_isolates_a_normal_profile():
    """The ceiling only applies to the profile that asks for it."""
    profile = profiles.get_profile(profiles.WORKSPACE_WRITE)

    provider = select_provider(profile, _ISOLATING_BOX)

    assert provider is not None
    assert provider.name == "seatbelt"


def test_off_profile_is_never_denied_for_lack_of_a_provider():
    """Without isolation there is nothing to be missing."""
    _, provider, denial = plan_run(profiles.OFF, _ISOLATING_BOX)

    assert denial is None
    assert provider is not None and provider.name == "process"


def test_off_profile_runs_through_the_registry():
    """The registry plans the run under process instead of refusing it."""
    from core.sandbox.contracts import SandboxResult

    stub = _StubProvider("process", "process")
    registry = SandboxRegistry([stub])
    seen = {}

    def _run(request, profile, limits):
        seen["profile"] = profile.name
        return SandboxResult(ok=True, provider=stub.name, isolation=stub.isolation)

    stub.run = _run
    result = registry.run(profiles.OFF, object())

    assert seen["profile"] == profiles.OFF
    assert result.ok is True
    # The limits are stamped on the result by the registry, so switching the
    # sandbox off cannot also switch off the ceilings.
    assert result.limits_used["timeout_s"] > 0


# ── the ceiling helper ───────────────────────────────────────────────────────


@pytest.mark.parametrize("offered,ceiling,expected", [
    ("os", "process", False),
    ("container", "process", False),
    ("process", "process", True),
    ("none", "process", True),
    ("os", "", True),
    ("os", "oops", True),
])
def test_ceiling_helper(offered, ceiling, expected):
    """A provider stronger than the ceiling does not qualify."""
    assert within_isolation_ceiling(offered, ceiling) is expected


def test_a_profile_without_a_ceiling_accepts_the_strongest_backend():
    """An empty ceiling keeps the old strongest-first behaviour."""
    profile = SandboxProfile(name="custom", isolation="process", max_isolation="")

    provider = select_provider(profile, _ISOLATING_BOX)

    assert provider is not None and provider.name == "seatbelt"


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
