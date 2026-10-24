# -*- coding: utf-8 -*-
"""tests/test_orchestration_contracts.py - invariants of the agent run contract.

``core.orchestration.contracts`` is the only thing the UI, the HTTP layer and
the in-process runtime share, so its invariants are pinned here: exactly four
stop reasons, one error shape, a compact result dict, and a TOTAL mapping from
the executor's final status onto the contract triple. A gap in any of them
would surface as a silent success somewhere far from this file.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import contracts as C  # noqa: E402


# ─── stop reasons ────────────────────────────────────────────────────────────


def test_stop_reasons_are_exactly_four():
    """Four values, no duplicates, and a refusal is NOT one of them."""
    assert C.STOP_REASONS == ("completed", "aborted", "error", "max-tokens")
    assert len(set(C.STOP_REASONS)) == 4
    assert C.ERR_REFUSAL == "refusal"
    assert C.ERR_REFUSAL not in C.STOP_REASONS


def test_aborted_and_error_stay_distinct():
    """A known termination (partial result) never shares a code with a failure."""
    assert C.STOP_ABORTED != C.STOP_ERROR
    assert C.STOP_COMPLETED != C.STOP_MAX_TOKENS


# ─── final_status mapping ────────────────────────────────────────────────────


def test_map_final_status_success_values():
    """``applied``/``discarded`` are successful outcomes, not a stop reason."""
    for status in ("done", "applied", "discarded"):
        assert C.map_final_status(status) == (C.STATUS_DONE, C.STOP_COMPLETED, None)


def test_map_final_status_terminal_failures():
    assert C.map_final_status("error") == (C.STATUS_FAILED, C.STOP_ERROR, None)
    assert C.map_final_status("sanitized_required") == (
        C.STATUS_FAILED, C.STOP_ERROR, C.ERR_SANITIZED_REQUIRED)
    assert C.map_final_status("stopped_by_user") == (
        C.STATUS_CANCELLED, C.STOP_ABORTED, C.ERR_CANCELLED_BY_USER)
    assert C.map_final_status("stopped_max_steps") == (
        C.STATUS_CANCELLED, C.STOP_ABORTED, C.ERR_MAX_STEPS)


def test_map_final_status_awaiting_values_are_not_terminal():
    for status in ("awaiting_confirmation", "awaiting_user", "awaiting_approval"):
        mapped = C.map_final_status(status)
        assert mapped[0] == C.STATUS_AWAITING_INPUT
        assert mapped[1] is None
        assert mapped[0] not in C.TERMINAL_STATUSES


def test_map_final_status_unknown_is_failure_not_success():
    """An unmapped status must never be reported as a completed run."""
    assert C.map_final_status("something_new") == (
        C.STATUS_FAILED, C.STOP_ERROR, C.ERR_PROVIDER_ERROR)
    assert C.map_final_status("") == (C.STATUS_FAILED, C.STOP_ERROR, C.ERR_PROVIDER_ERROR)


def test_map_final_status_keeps_explicit_error_code():
    """A child refusal reaches the parent as failed/error/refusal."""
    assert C.map_final_status("error", error_code=C.ERR_REFUSAL) == (
        C.STATUS_FAILED, C.STOP_ERROR, C.ERR_REFUSAL)


def test_final_status_map_covers_every_documented_status():
    documented = {
        "done", "applied", "discarded", "error", "awaiting_confirmation",
        "awaiting_user", "awaiting_approval", "sanitized_required",
        "stopped_by_user", "stopped_max_steps",
    }
    assert set(C.FINAL_STATUS_MAP) == documented


# ─── result and brief shapes ─────────────────────────────────────────────────


def test_run_result_to_dict_is_compact():
    """The parent gets a summary, never the child transcript."""
    result = C.AgentRunResult(
        run_id="r1", status=C.STATUS_DONE, stop_reason=C.STOP_COMPLETED,
        summary="short summary",
        artifacts=[C.Artifact(path="out.txt", kind="file", digest="1-2")],
        steps_used=3, tokens_used={"in": 10, "out": 5},
    )
    payload = result.to_dict()
    assert payload["run_id"] == "r1"
    assert payload["status"] == C.STATUS_DONE
    assert payload["summary"] == "short summary"
    assert payload["artifacts"] == [{"path": "out.txt", "kind": "file", "digest": "1-2"}]
    for forbidden in ("history", "transcript", "events", "messages"):
        assert forbidden not in payload
    assert result.is_terminal is True


def test_run_result_running_is_not_terminal():
    assert C.AgentRunResult(run_id="r").is_terminal is False


def test_agent_brief_artifacts_in_is_a_name_to_path_map():
    """The child runner calls ``artifacts_in.items()``: the field must be a dict."""
    brief = C.AgentBrief(goal="do it")
    assert brief.artifacts_in == {}
    assert hasattr(brief.artifacts_in, "items")

    filled = C.AgentBrief(goal="do it", artifacts_in={"input": "a.txt"})
    assert list(filled.artifacts_in.items()) == [("input", "a.txt")]


# ─── error shape ─────────────────────────────────────────────────────────────


def test_error_result_has_the_single_documented_shape():
    payload = C.error_result("r1", "Agent not allowed.", C.ERR_AGENT_NOT_ALLOWED,
                             allowed=["dev_agent"], self_only=True)
    assert payload == {
        "ok": False,
        "error": "Agent not allowed.",
        "error_code": C.ERR_AGENT_NOT_ALLOWED,
        "run_id": "r1",
        "allowed": ["dev_agent"],
        "self_only": True,
    }


def test_cancel_report_serialises_children():
    report = C.CancelReport(run_id="root", cancelled=True,
                           previous_status=C.STATUS_RUNNING,
                           reason="requested", children=["c1", "c2"])
    payload = report.to_dict()
    assert payload["children"] == ["c1", "c2"]
    assert payload["reason"] == "requested"
    assert payload["previous_status"] == C.STATUS_RUNNING


# ─── documented defaults ─────────────────────────────────────────────────────


def test_delegation_defaults_match_the_contract():
    assert C.DEFAULT_MAX_DEPTH == 1
    assert C.DEFAULT_MAX_ACTIVE_SUBAGENTS == 3
    assert C.DEFAULT_SUBAGENT_MAX_STEPS == 20
    assert C.DEFAULT_SUBAGENT_BUDGET_TOKENS == 0  # 0 = unlimited
    assert C.DEFAULT_SUBAGENT_TIMEOUT_SECONDS == 600
    assert C.DEFAULT_TOTAL_SUBAGENT_RUNS == 32


def test_error_codes_are_stable_strings():
    codes = {
        C.ERR_AGENT_NOT_ALLOWED: "agent_not_allowed",
        C.ERR_ASSISTANT_NOT_ALLOWED: "assistant_not_allowed",
        C.ERR_UNKNOWN_AGENT: "unknown_agent",
        C.ERR_UNKNOWN_ASSISTANT: "unknown_assistant",
        C.ERR_INVALID_BRIEF: "invalid_brief",
        C.ERR_INVALID_ARGS: "invalid_args",
        C.ERR_CANCELLED_BY_PARENT: "cancelled_by_parent",
        C.ERR_TIMEOUT: "timeout",
        C.ERR_MAX_STEPS: "max_steps",
        C.ERR_CONTEXT_TOO_LARGE: "context_too_large",
        C.ERR_NOT_IMPLEMENTED: "not_implemented",
        C.ERR_LIMIT_EXCEEDED: "limit_exceeded",
    }
    for actual, expected in codes.items():
        assert actual == expected
