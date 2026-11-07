# -*- coding: utf-8 -*-
"""tests/test_sandbox_phase1.py - contracts, policy and journal of the sandbox.

What is covered here:

- the isolation order and the ``satisfies`` rule (a weaker provider never
  meets a stronger requirement, an unknown level never satisfies anything);
- profiles: named, distinct, and an unknown name resolves to nothing;
- the policy denies - not downgrades - when no provider reaches the
  required isolation, and a denial is a result rather than an exception;
- the registry picks the strongest available provider and can only tighten
  a caller's limits, never widen the profile;
- the journal records an executed run AND a refused run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sandbox import contracts, plan_run, profiles
from core.sandbox.contracts import ResourceLimits, SandboxRequest, SandboxResult
from core.sandbox.policy import SandboxProvider, SandboxRegistry, select_provider


class StubProvider(SandboxProvider):
    """A provider with a fixed level and a recorded call list."""

    def __init__(self, name: str, isolation: str, available: bool = True,
                 capabilities=()):
        self.name = name
        self.isolation = isolation
        self.capabilities = tuple(capabilities)
        self._available = available
        self.calls = []

    def available(self) -> bool:
        return self._available

    def run(self, request, profile, limits):
        self.calls.append((request, profile, limits))
        return SandboxResult(ok=True, provider=self.name, isolation=self.isolation,
                             stdout="done", duration_ms=1)


def _request(profile: str = profiles.LEGACY_PROCESS) -> SandboxRequest:
    return SandboxRequest(tool="run_code", argv=("python", "-c", "print(1)"),
                          cwd=".", env={"PATH": "/usr/bin"}, profile=profile)


# ── isolation order ──────────────────────────────────────────────────────────


def test_isolation_order_is_weakest_first():
    """The declared order is the ground truth for every comparison."""
    assert contracts.ISOLATION_ORDER == ("none", "process", "os", "container", "remote")
    assert contracts.isolation_rank("none") == 0
    assert contracts.isolation_rank("remote") > contracts.isolation_rank("container")


def test_unknown_level_has_no_rank():
    """A typo must not accidentally sort above a real level."""
    assert contracts.isolation_rank("processs") == -1
    assert contracts.isolation_rank("") == -1


def test_a_weaker_provider_never_meets_a_stronger_requirement():
    """The rule that makes fail-closed possible."""
    assert contracts.satisfies("os", "process") is True
    assert contracts.satisfies("process", "os") is False
    assert contracts.satisfies("container", "container") is True


def test_unknown_level_on_either_side_never_satisfies():
    """A typo on either side is a refusal, not a downgrade."""
    assert contracts.satisfies("oops", "process") is False
    assert contracts.satisfies("process", "oops") is False


# ── profiles ─────────────────────────────────────────────────────────────────


def test_profiles_are_named_and_distinct():
    """Every advertised profile resolves and no two share a name."""
    names = profiles.profile_names()
    assert len(names) == len(set(names))
    for name in names:
        profile = profiles.get_profile(name)
        assert profile is not None and profile.name == name


def test_unknown_profile_resolves_to_nothing():
    """A missing profile is a refusal, never a default."""
    assert profiles.get_profile("everything_allowed") is None
    assert profiles.get_profile("") is None


def test_default_profile_is_read_only_on_network():
    """Generated code runs with no network unless a profile grants it."""
    default = profiles.get_profile(profiles.DEFAULT_PROFILE)
    assert default is not None
    assert default.network is False


def test_network_profile_requires_os_isolation():
    """Network access is only offered together with OS isolation."""
    network = profiles.get_profile(profiles.NETWORK)
    assert network is not None
    assert network.isolation == "os"


# ── policy: fail-closed ──────────────────────────────────────────────────────


def test_unknown_profile_is_denied_by_the_policy():
    """plan_run refuses an unknown name instead of guessing."""
    profile, provider, denial = plan_run("nonsense", [StubProvider("p", "os")])
    assert profile is None and provider is None
    assert denial is not None and denial.denied is True
    assert "nonsense" in denial.error


def test_no_qualified_provider_denies_the_run():
    """A profile needing 'os' is refused when only 'process' exists."""
    profile, provider, denial = plan_run(profiles.NETWORK, [StubProvider("p", "process")])
    assert profile is not None and provider is None
    assert denial is not None and denial.denied is True
    assert "os" in denial.error


def test_denial_is_a_result_not_an_exception():
    """The caller gets a value it can return as a tool result."""
    registry = SandboxRegistry([StubProvider("p", "process")])
    result = registry.run(profiles.NETWORK, _request())
    assert isinstance(result, SandboxResult)
    assert result.ok is False and result.denied is True


def test_denied_run_never_reaches_a_provider():
    """Nothing may execute when the guarantee is unavailable."""
    weak = StubProvider("weak", "process")
    SandboxRegistry([weak]).run(profiles.NETWORK, _request())
    assert weak.calls == []


def test_unavailable_provider_is_skipped():
    """A provider that is not usable on this machine cannot be chosen."""
    offline = StubProvider("offline", "os", available=False)
    assert select_provider(profiles.get_profile(profiles.NETWORK), [offline]) is None


def test_strongest_available_provider_wins():
    """Selection is deterministic and prefers the stronger backend."""
    strong = StubProvider("strong", "container",
                          capabilities=("network-off", "write-confined"))
    weak = StubProvider("weak", "os", capabilities=("network-off", "write-confined"))
    chosen = select_provider(profiles.get_profile(profiles.WORKSPACE_WRITE), [weak, strong])
    assert chosen is strong


# ── limits ───────────────────────────────────────────────────────────────────


def test_a_caller_cannot_widen_a_limit():
    """A timeout above the profile ceiling is clamped, not honoured."""
    stub = StubProvider("p", "os", capabilities=("network-off", "write-confined"))
    registry = SandboxRegistry([stub])
    profile = profiles.get_profile(profiles.READ_ONLY)
    assert profile is not None
    registry.run(profiles.READ_ONLY, _request(),
                 limits_override=ResourceLimits(timeout_s=profile.limits.timeout_s + 600))
    used = stub.calls[0][2]
    assert used.timeout_s <= profile.limits.timeout_s


def test_a_stricter_limit_is_applied():
    """A lower timeout is honoured: the caller may always be stricter."""
    stub = StubProvider("p", "os", capabilities=("network-off", "write-confined"))
    SandboxRegistry([stub]).run(profiles.WORKSPACE_WRITE, _request(),
                                limits_override=ResourceLimits(timeout_s=5))
    assert stub.calls[0][2].timeout_s == 5


def test_limits_serialise_to_a_mapping():
    """The journal stores the limits as JSON, so they must be plain values."""
    payload = ResourceLimits(timeout_s=11, memory_mb=22).to_dict()
    assert payload["timeout_s"] == 11 and payload["memory_mb"] == 22
    assert all(isinstance(value, int) for value in payload.values())


# ── request fingerprint ──────────────────────────────────────────────────────


def test_fingerprint_ignores_environment_values_but_not_names():
    """A different temp dir is not a different program; a new variable is."""
    first = _request()
    second = SandboxRequest(tool=first.tool, argv=first.argv, cwd=first.cwd,
                            env={"PATH": "/other/bin"}, profile=first.profile)
    assert first.fingerprint() == second.fingerprint()
    third = SandboxRequest(tool=first.tool, argv=first.argv, cwd=first.cwd,
                           env={"PATH": "/usr/bin", "HOME": "/tmp"},
                           profile=first.profile)
    assert third.fingerprint() != first.fingerprint()


def test_fingerprint_tracks_the_program():
    """A different argv is a different run."""
    first = _request()
    other = SandboxRequest(tool=first.tool, argv=("python", "-c", "print(2)"),
                           cwd=first.cwd, env=first.env, profile=first.profile)
    assert other.fingerprint() != first.fingerprint()


# ── journal ──────────────────────────────────────────────────────────────────


@pytest.fixture
def engine(tmp_path):
    """A throwaway main database with the journal table."""
    from sqlalchemy import create_engine

    from storage.db import _ensure_sandbox_tables

    eng = create_engine("sqlite:///" + str(tmp_path / "journal.db"),
                        connect_args={"check_same_thread": False})
    _ensure_sandbox_tables(eng)
    yield eng
    eng.dispose()


def test_an_executed_run_is_recorded(engine):
    """Given a finished run, then the journal holds its outcome."""
    from storage import sandbox_journal

    sandbox_journal.record_run(engine, run_id="r1", tool="run_code",
                               profile=profiles.LEGACY_PROCESS, isolation="process",
                               provider="process", request_fingerprint="abc",
                               ok=True, returncode=0, duration_ms=12, stdout="hi")
    rows = sandbox_journal.recent_runs(engine)
    assert len(rows) == 1
    assert rows[0]["id"] == "r1" and rows[0]["ok"] == 1
    assert rows[0]["stdout_tail"] == "hi"


def test_a_refused_run_is_recorded_too(engine):
    """A denial without a record would make fail-closed unverifiable."""
    from storage import sandbox_journal

    sandbox_journal.record_run(engine, run_id="r2", tool="run_code",
                               profile=profiles.NETWORK, isolation="none",
                               provider="", request_fingerprint="abc",
                               denied=True, error="no provider reaches os")
    rows = sandbox_journal.recent_runs(engine)
    assert rows[0]["denied"] == 1
    assert "refused" in sandbox_journal.summarise(rows[0])


def test_a_journal_write_failure_never_breaks_a_run(tmp_path):
    """The journal is a record, not a gate."""
    from sqlalchemy import create_engine

    from core.sandbox import runner

    broken = create_engine("sqlite:///" + str(tmp_path / "empty.db"))
    stub = StubProvider("p", "process")
    result = runner.execute(_request(), profile=profiles.LEGACY_PROCESS,
                            providers=[stub], journal_engine=broken)
    broken.dispose()
    assert result.ok is True and stub.calls

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
