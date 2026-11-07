"""Nested runs: a sandbox is inherited, never entered twice.

macOS refuses a nested ``sandbox_apply`` with ``Operation not permitted``.
That message names the wrapper rather than the cause, so a nested run used to
look like a broken tool. The fix is to carry the grants of the enclosing
policy in the environment, compare them with what the new run asks for, and
run the program directly when the enclosing policy already covers it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sandbox import inheritance, profiles
from core.sandbox.contracts import ResourceLimits, SandboxRequest
from core.sandbox.profiles import get_profile
from core.sandbox.providers import default_providers
from core.sandbox.providers.inherited import InheritedProvider, capabilities_of
from core.sandbox.providers.seatbelt import SeatbeltProvider

# ─── the marker ─────────────────────────────────────────────────────────────


def test_marker_round_trips_every_grant():
    value = inheritance.format_marker("os", True, False, "network")
    parsed = inheritance.parse_marker(value)
    assert parsed is not None
    assert parsed.isolation == "os"
    assert parsed.network is True
    assert parsed.writable is False
    assert parsed.profile == "network"


@pytest.mark.parametrize("value", ["", None, "garbage", "v2;isolation=os",
                                   "v1", "v1;network=1", "v1;isolation="])
def test_a_malformed_marker_is_treated_as_absent(value):
    """A guess must never widen a grant: unusable means "not sandboxed"."""
    assert inheritance.parse_marker(value) is None


def test_the_request_environment_is_copied_not_mutated():
    original = {"HOME": "/tmp/x"}
    marked = inheritance.mark_env(original, "os", False, True, "workspace_write")
    assert inheritance.SANDBOX_MARKER in marked
    assert inheritance.SANDBOX_MARKER not in original


def test_with_marker_returns_a_new_request():
    request = SandboxRequest(tool="t", argv=("echo",), cwd="/", env={})
    marked = inheritance.with_marker(request, get_profile(profiles.NETWORK))
    assert inheritance.SANDBOX_MARKER in marked.env
    assert inheritance.SANDBOX_MARKER not in request.env
    assert marked.argv == request.argv


# ─── grant comparison ───────────────────────────────────────────────────────


def test_a_narrower_request_runs_inside_a_wider_policy():
    enclosing = inheritance.EnclosingSandbox(isolation="os", network=True, writable=True)
    assert inheritance.compare(enclosing, get_profile(profiles.READ_ONLY)).ok
    assert inheritance.compare(enclosing, get_profile(profiles.WORKSPACE_WRITE)).ok


def test_a_write_inside_a_read_only_policy_is_refused():
    enclosing = inheritance.EnclosingSandbox(isolation="os", network=False, writable=False)
    verdict = inheritance.compare(enclosing, get_profile(profiles.WORKSPACE_WRITE))
    assert verdict.ok is False
    assert "read-only" in verdict.error


def test_network_inside_an_offline_policy_is_refused():
    enclosing = inheritance.EnclosingSandbox(isolation="os", network=False, writable=True)
    verdict = inheritance.compare(enclosing, get_profile(profiles.NETWORK))
    assert verdict.ok is False
    assert "network" in verdict.error


def test_a_stronger_isolation_request_is_refused():
    enclosing = inheritance.EnclosingSandbox(isolation="process", network=True, writable=True)
    verdict = inheritance.compare(enclosing, get_profile(profiles.WORKSPACE_WRITE))
    assert verdict.ok is False
    assert "cannot be entered twice" in verdict.error


# ─── the provider ───────────────────────────────────────────────────────────


def test_the_provider_is_unavailable_outside_a_sandbox(monkeypatch):
    monkeypatch.delenv(inheritance.SANDBOX_MARKER, raising=False)
    provider = InheritedProvider()
    assert provider.available() is False
    assert provider.isolation == "none"
    assert provider.capabilities == ()


def test_the_provider_reports_the_enclosing_grants(monkeypatch):
    monkeypatch.setenv(inheritance.SANDBOX_MARKER,
                       inheritance.format_marker("os", False, True, "workspace_write"))
    provider = InheritedProvider()
    assert provider.available() is True
    assert provider.isolation == "os"
    assert "network-off" in provider.capabilities
    assert "write-confined" in provider.capabilities


def test_an_online_policy_does_not_claim_network_off():
    """An online sandbox cannot promise the child an offline runtime."""
    caps = capabilities_of(inheritance.EnclosingSandbox("os", network=True, writable=True))
    assert "network-off" not in caps
    assert "write-confined" in caps


def test_a_read_only_policy_does_not_claim_write_confined():
    caps = capabilities_of(inheritance.EnclosingSandbox("os", network=False, writable=False))
    assert "write-confined" not in caps


def test_the_inherited_provider_is_registered_by_default():
    """Without it a nested run finds no provider and is denied."""
    names = [p.name for p in default_providers()]
    assert "inherited" in names


# ─── the wrapping providers step aside ──────────────────────────────────────


def test_seatbelt_does_not_wrap_an_already_confined_run(monkeypatch):
    """The nested wrapper must be skipped, not attempted and failed."""
    monkeypatch.setenv(inheritance.SANDBOX_MARKER,
                       inheritance.format_marker("os", False, True, "workspace_write"))
    seen = {}

    import core.sandbox.providers.seatbelt as seatbelt_module

    def fake_run_wrapped(name, isolation, prefix, request, limits, **kwargs):
        seen["prefix"] = list(prefix)
        seen["isolation"] = isolation
        from core.sandbox.contracts import SandboxResult
        return SandboxResult(ok=True, provider=name, isolation=isolation)

    monkeypatch.setattr(seatbelt_module, "run_wrapped", fake_run_wrapped)
    request = SandboxRequest(tool="t", argv=("echo", "x"), cwd="/tmp", env={})
    result = SeatbeltProvider().run(request, get_profile(profiles.WORKSPACE_WRITE),
                                    ResourceLimits())

    assert result.ok is True
    assert seen["prefix"] == []
    assert seen["isolation"] == "os"


def test_seatbelt_refuses_a_write_inside_a_read_only_sandbox(monkeypatch):
    monkeypatch.setenv(inheritance.SANDBOX_MARKER,
                       inheritance.format_marker("os", False, False, "read_only"))
    request = SandboxRequest(tool="t", argv=("echo", "x"), cwd="/tmp", env={})
    result = SeatbeltProvider().run(request, get_profile(profiles.WORKSPACE_WRITE),
                                    ResourceLimits())
    assert result.ok is False
    assert result.denied is True
    assert "read-only" in result.error


def test_the_environment_builder_keeps_the_marker(monkeypatch):
    """A lost marker makes the grandchild try to nest and die."""
    from dev_agent import sandbox_env

    value = inheritance.format_marker("os", False, True, "workspace_write")
    monkeypatch.setenv(sandbox_env.SANDBOX_GRANTS_VAR, value)
    env = sandbox_env.build_child_env(project_root=None)
    assert env.get(sandbox_env.SANDBOX_GRANTS_VAR) == value


def test_the_environment_builder_never_forwards_other_platform_variables(monkeypatch):
    """The marker is a capability description, not a way to pass secrets."""
    from dev_agent import sandbox_env

    monkeypatch.setenv("SAGAAI_API_TOKEN", "secret")
    env = sandbox_env.build_child_env(project_root=None)
    assert "SAGAAI_API_TOKEN" not in env
