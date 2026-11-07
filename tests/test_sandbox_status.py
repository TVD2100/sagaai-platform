"""The sandbox status block and the workspace tool description.

The model picks the commands, so it must be told what the profile in force
allows BEFORE it emits one: without the block it writes outside the project
and reports a failure, or expects network access the profile refuses. The
block must agree with the policy - a status that promised more than the
policy grants would be worse than no status at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sandbox import status
from core.sandbox.policy import SandboxProvider


class StubProvider(SandboxProvider):
    """A provider with fixed facts, used to pin what the block reports."""

    def __init__(self, name: str, isolation: str, capabilities=(), available=True):
        self.name = name
        self.isolation = isolation
        self.capabilities = tuple(capabilities)
        self._available = available

    def available(self) -> bool:
        return self._available

    def run(self, request, profile, limits):  # pragma: no cover - never called
        raise AssertionError("the status block must not execute anything")


def _os_provider():
    """Return the strongest stub that satisfies the isolated profiles."""
    return StubProvider("stub-os", "os", ("network-off", "write-confined"))


@pytest.fixture(autouse=True)
def _clear_cache():
    """The status cache is process-wide, so every test starts clean."""
    status.reset_cache()
    yield
    status.reset_cache()


# ─── the decision ───────────────────────────────────────────────────────────


def test_the_status_names_the_provider_the_policy_selects():
    facts = status.describe("workspace_write", providers=[_os_provider()])

    assert facts["known"] is True
    assert facts["provider"] == "stub-os"
    assert facts["provider_isolation"] == "os"
    assert facts["denied"] is False
    assert facts["writable"] is True
    assert facts["network"] is False


def test_an_unknown_profile_is_reported_as_unknown():
    facts = status.describe("nonsense", providers=[_os_provider()])

    assert facts["known"] is False
    assert facts["denied"] is True
    assert "nonsense" in facts["error"]


def test_a_profile_without_a_provider_is_denied():
    """No provider must read as a refusal, not as a weaker promise."""
    facts = status.describe("workspace_write",
                            providers=[StubProvider("weak", "process")])

    assert facts["denied"] is True
    assert facts["provider"] == ""


def test_the_limits_come_from_the_profile():
    facts = status.describe("read_only", providers=[_os_provider()])

    assert facts["limits"]["timeout_s"] == 120


def test_the_answer_is_cached_per_profile(monkeypatch):
    """A provider probe spawns a process; the prompt is rebuilt often."""
    calls = []

    def fake_defaults():
        calls.append(1)
        return [_os_provider()]

    monkeypatch.setattr(status, "_default_providers", fake_defaults)
    status.describe("workspace_write")
    status.describe("workspace_write")
    assert len(calls) == 1

    status.describe("workspace_write", fresh=True)
    assert len(calls) == 2


# ─── the block ──────────────────────────────────────────────────────────────


def test_the_block_states_the_profile_provider_and_guarantees():
    facts = status.describe("workspace_write", providers=[_os_provider()])
    text = "\n".join(status.format_lines(facts, "/work/project"))

    assert text.startswith("## Code execution sandbox")
    assert "`workspace_write`" in text
    assert "`stub-os`" in text
    assert "no network" in text
    assert "writes confined" in text
    assert "/work/project" in text


def test_the_block_says_git_staging_works_but_hooks_are_closed():
    """The instruction set tells the agent to commit; the block must agree."""
    facts = status.describe("workspace_write", providers=[_os_provider()])
    text = "\n".join(status.format_lines(facts))

    assert "`git add`" in text
    assert "`hooks`" in text and "`config`" in text


def test_a_read_only_profile_tells_the_model_to_use_the_file_tools():
    facts = status.describe("read_only", providers=[_os_provider()])
    text = "\n".join(status.format_lines(facts))

    assert "read-only" in text
    assert "file tools" in text


def test_a_network_profile_reports_the_shared_temporary_tree():
    provider = StubProvider("stub-net", "os", ("write-confined",))
    facts = status.describe("network", providers=[provider])
    text = "\n".join(status.format_lines(facts))

    assert "shared temporary tree" in text
    assert "has no network" not in text


def test_an_off_profile_admits_there_is_no_operating_system_boundary():
    facts = status.describe("off", providers=[StubProvider("weak", "process")])
    text = "\n".join(status.format_lines(facts))

    assert "no operating-system boundary" in text


def test_an_unknown_profile_block_states_the_refusal():
    facts = status.describe("nonsense", providers=[_os_provider()])
    text = "\n".join(status.format_lines(facts))

    assert "does not exist" in text
    assert "writes confined" not in text


def test_a_denied_profile_block_states_the_refusal():
    facts = status.describe("workspace_write",
                            providers=[StubProvider("weak", "process")])
    text = "\n".join(status.format_lines(facts))

    assert "cannot run on this machine" in text
    assert "refused, not downgraded" in text


def test_the_block_never_raises_on_broken_input():
    """Prompt metadata must never break the prompt itself."""
    assert status.block("workspace_write", providers=[]) != ""
    assert isinstance(status.block("workspace_write", providers=[]), str)


# ─── the prompt wiring ──────────────────────────────────────────────────────


def test_the_prompt_block_is_appended_for_the_employee_profile(monkeypatch):
    """The block must be built from the employee config, not a constant."""
    from core.sandbox import status as sandbox_status

    seen = {}

    def fake_block(profile_name, workspace="", providers=None):
        seen["profile"] = profile_name
        seen["workspace"] = workspace
        return "## Code execution sandbox\n\nstub line"

    monkeypatch.setattr("core.sandbox.status.block", fake_block)
    lines = sandbox_status.prompt_lines(
        {"sandbox_profile": "read_only"}, "/work/project")

    assert seen["profile"] == "read_only"
    assert seen["workspace"] == "/work/project"
    assert "stub line" in lines


def test_the_cleared_toolbar_switch_reports_the_off_profile(monkeypatch):
    from core.sandbox import status as sandbox_status

    seen = {}

    def fake_block(profile_name, workspace="", providers=None):
        seen["profile"] = profile_name
        return "## Code execution sandbox"

    monkeypatch.setattr("core.sandbox.status.block", fake_block)
    sandbox_status.prompt_lines(
        {"sandbox_profile": "read_only", "chat_sandbox": False}, "")

    assert seen["profile"] == "off"


# ─── the set_workspace description ──────────────────────────────────────────


def test_set_workspace_description_says_when_to_call_it():
    """The model must act on a named project instead of asking for the path."""
    from dev_agent.tool_catalog_data import WORKSPACE_TOOL_CATALOG

    entry = next(t for t in WORKSPACE_TOOL_CATALOG if t["name"] == "set_workspace")
    desc = entry["desc"]

    assert "path" in desc
    assert "list_recent_workspaces" in desc
    assert "SAME turn" in desc
    assert "absolute path" in desc
