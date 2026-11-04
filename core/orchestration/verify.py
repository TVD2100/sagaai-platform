# -*- coding: utf-8 -*-
"""
core.orchestration.verify - the independent verifier of a delegated result.

A fan-out returns several results in one step, but nothing checks that they
are right: the parent believes what a child reports. This module adds the
missing step WITHOUT touching the fan-out contract - the parent verifies one
finished run of its own tree with a separately configured agent and receives
a structured verdict.

The verifier is independent because of what it does NOT see: the transcript
of the target, its journal and the reasoning that produced the result. It
receives the RESULT - summary, artifacts, error code, usage counters - and
the criteria, and reads any artifact it needs with its own tools by the
absolute path. A verifier reading the executor's train of thought would
agree with it instead of checking the outcome.

The launch goes through ``service.run`` - the same path as ``spawn_agent`` -
so depth, pool and total-run limits apply to a verifier exactly as they
apply to any other child. No second launch mechanism exists.

Vocabulary:

- ``passed`` - every criterion holds on the evidence the verifier checked;
- ``failed`` - the evidence contradicts a criterion;
- ``inconclusive`` - the result could not be checked: missing evidence, an
  unevaluable criterion or an answer that is not a parsable verdict.

``inconclusive`` is an ANSWER, not an error: it means "could not tell", and
a caller must never present it as success.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.orchestration.contracts import (
    ERR_INVALID_ARGS,
    ERR_RUN_NOT_FINISHED,
    ERR_RUN_NOT_FOUND,
    ERR_VERIFIER_NOT_CONFIGURED,
    STATUS_DONE,
    TERMINAL_STATUSES,
    AgentBrief,
    AgentRunResult,
    Artifact,
    SpawnRequest,
    error_result,
)

#: The three values a verdict may take. Anything else is inconclusive.
VERDICT_PASSED = "passed"
VERDICT_FAILED = "failed"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICTS = (VERDICT_PASSED, VERDICT_FAILED, VERDICT_INCONCLUSIVE)

#: How much of the target summary reaches the brief. A runaway summary must
#: not turn one verification into a context overflow; the artifacts stay
#: reachable by path, so a cut summary does not hide the evidence itself.
MAX_SUMMARY_CHARS = 6000

#: Backslash as a character code: keeps the JSON scanner readable without a
#: literal escape sequence, which is the one construct a hand-written tool
#: call keeps mangling.
BACKSLASH = chr(92)

#: The answer shape the verifier is asked for. It travels inside the brief,
#: so the requirement lives next to the parser that enforces it.
ANSWER_SPEC = (
    "Answer with ONE JSON object and nothing after it:",
    '{"verdict": "passed" | "failed" | "inconclusive",',
    ' "reasons": ["..."],',
    ' "checked": ["..."]}',
    'Rules: "passed" only when every criterion is satisfied on the evidence',
    'you checked yourself; "inconclusive" when the evidence is missing or a',
    'criterion cannot be evaluated; "failed" when the evidence contradicts a',
    'criterion. Never put a verdict you did not verify yourself.',
)


@dataclass(frozen=True)
class Verdict:
    """Outcome of one verification.

    ``target_run_id`` names the result that was checked, ``verifier_run_id``
    the run that checked it (empty when the verifier never started, e.g. a
    launch failure). ``summary`` keeps the raw answer of the verifier so a
    questionable verdict can be read back without asking it again.
    """

    target_run_id: str
    verifier_run_id: str = ""
    verdict: str = VERDICT_INCONCLUSIVE
    reasons: List[str] = field(default_factory=list)
    checked: List[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return the tool-shaped dict form of this verdict."""
        return {
            "target_run_id": self.target_run_id,
            "verifier_run_id": self.verifier_run_id,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "checked": list(self.checked),
            "summary": self.summary,
        }


def _json_objects(text: str) -> List[str]:
    """Return every balanced ``{...}`` run of *text*, string literals honoured."""
    objects: List[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == BACKSLASH:
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    objects.append(text[start:index + 1])
                    start = -1
    return objects


def _last_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Return the last parsable JSON object of *text*, or None.

    The verifier is asked for one JSON object, but a model may preface it with
    reasoning that itself contains braces. Scanning for balanced objects and
    taking the last one that PARSES is what makes the reader survive that. An
    answer without a parsable object yields None and becomes an inconclusive
    verdict instead of an exception.
    """
    for chunk in reversed(_json_objects(text or "")):
        try:
            parsed = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _string_list(value: Any) -> Optional[List[str]]:
    """Return *value* as a list of non-empty strings, or None when not a list."""
    if not isinstance(value, list):
        return None
    out: List[str] = []
    for item in value:
        text = str(item if item is not None else "").strip()
        if text:
            out.append(text)
    return out


def parse_verdict(summary: str) -> Tuple[str, List[str], List[str]]:
    """Extract ``(verdict, reasons, checked)`` from the verifier answer.

    A free-form reply cannot be acted on: a caller must tell "checked and it
    holds" from "checked and it does not" from "could not check". The rules
    are conservative on purpose, and one of them outranks the rest:

    - no parsable JSON object - inconclusive, the reason names the parsing;
    - a verdict outside the three values - inconclusive naming what came;
    - ``reasons`` that is not a list - ``passed`` cannot stand and becomes
      inconclusive (a verdict without its evidence is not a verdict);
    - ``passed`` next to a non-empty ``reasons`` - FAILED: a verifier must not
      say "all good" and list objections in the same breath, and the
      objections are the part that carries information.
    """
    parsed = _last_json_object(summary or "")
    if parsed is None:
        return (VERDICT_INCONCLUSIVE,
                ["answer is not a parsable verdict"], [])

    checked = _string_list(parsed.get("checked")) or []
    raw_verdict = parsed.get("verdict")
    verdict = str(raw_verdict if raw_verdict is not None else "").strip().lower()
    if verdict not in VERDICTS:
        return (VERDICT_INCONCLUSIVE,
                ["answer names an unsupported verdict: " + repr(raw_verdict)],
                checked)

    reasons = _string_list(parsed.get("reasons"))
    if reasons is None:
        if verdict == VERDICT_PASSED:
            return (VERDICT_INCONCLUSIVE,
                    ["a passed verdict carries no reasons list"], checked)
        return (verdict, [], checked)
    if verdict == VERDICT_PASSED and reasons:
        return (VERDICT_FAILED,
                ["the verdict was passed but reasons were listed"] + reasons,
                checked)
    return (verdict, reasons, checked)


#: The goal prefix of a verifier run. It marks the run as a CHECK, so a
#: second verification of that run can be refused by looking at the record
#: alone - even after the verifier setting was pointed at another agent.
VERIFIER_GOAL_PREFIX = "Verify the result of run "


#: What the verifier is told about its own field of view. Without these lines
#: an agent that is used to reading a child journal would fetch the
#: transcript first and judge the reasoning instead of the result.
BOUNDARY_NOTE = (
    "You see the RESULT of that run: its summary, artifacts, error code and "
    "usage counters. You do NOT see its transcript, its journal or the "
    "reasoning that produced it, and that is deliberate: judge the result, "
    "not the story around it. Read every artifact you need yourself, by the "
    "absolute path given below - do not assume a file matches its description."
)


def resolve_verifier(parent_slug: str) -> str:
    """Return the configured verifier slug, or "" when none is configured.

    The setting is deliberately separate from ``allowed_agents``: the right to
    CHECK a result is its own right, and folding it into the call allowlist
    would make verification silently depend on an unrelated list. A config
    read failure answers "" - the caller then refuses with
    ``verifier_not_configured`` instead of guessing an agent.
    """
    try:
        from core.orchestrators import get_verifier_agent
        return str(get_verifier_agent(parent_slug) or "").strip()
    except Exception:
        return ""


def _result_from_record(record: Dict[str, Any]) -> AgentRunResult:
    """Build an ``AgentRunResult`` view of a stored or live run record.

    The durable store does not keep artifacts (they are produced by the run
    and the record is rewritten on every heartbeat), while a live record does.
    Both shapes are accepted here so the brief never fails on the difference:
    a stored record simply contributes no artifact list.
    """
    raw_artifacts = record.get("artifacts") or []
    artifacts: List[Artifact] = []
    for item in raw_artifacts:
        if isinstance(item, Artifact):
            artifacts.append(item)
        elif isinstance(item, dict) and str(item.get("path") or "").strip():
            artifacts.append(Artifact(
                path=str(item.get("path") or ""),
                kind=str(item.get("kind") or "file"),
                digest=str(item.get("digest") or ""),
            ))
    tokens = record.get("tokens_used")
    return AgentRunResult(
        run_id=str(record.get("run_id") or ""),
        status=str(record.get("status") or ""),
        stop_reason=record.get("stop_reason"),
        summary=str(record.get("summary") or ""),
        artifacts=artifacts,
        error_code=record.get("error_code"),
        steps_used=int(record.get("steps_used") or 0),
        tokens_used=dict(tokens) if isinstance(tokens, dict) else {},
        child_thread_id=str(record.get("child_thread_id") or ""),
        retryable=bool(record.get("retryable", True)),
    )


def build_verifier_brief(target: AgentRunResult, criteria: str) -> AgentBrief:
    """Build the brief for the verifier from the target RESULT.

    Everything the verifier may know travels here: the verdict-bearing fields
    of the result, the artifacts with their absolute paths, the criteria and
    the answer format. The transcript is absent by construction - this
    function only ever receives an ``AgentRunResult``, which by contract
    carries no conversation.
    """
    summary = str(target.summary or "")
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + "\n[...summary truncated]"

    lines: List[str] = [
        "## Result to verify",
        "",
        "- run_id: " + str(target.run_id or ""),
        "- status: " + str(target.status or ""),
        "- stop_reason: " + str(target.stop_reason or ""),
        "- error_code: " + str(target.error_code or ""),
        "- steps_used: " + str(int(target.steps_used or 0)),
    ]
    if target.tokens_used:
        pairs = ", ".join("%s=%s" % (str(k), str(v))
                          for k, v in sorted(target.tokens_used.items()))
        lines.append("- tokens_used: " + pairs)
    lines.extend(["", "### Summary of the run", "", summary or "(the run left no summary)"])

    lines.extend(["", "### Artifacts", ""])
    if target.artifacts:
        for artifact in target.artifacts:
            detail = "- " + str(artifact.path)
            if artifact.kind and artifact.kind != "file":
                detail += " (" + str(artifact.kind) + ")"
            if artifact.digest:
                detail += " digest=" + str(artifact.digest)
            lines.append(detail)
    else:
        lines.append("The run reported no artifacts.")

    lines.extend(["", "### Criteria", ""])
    text = str(criteria or "").strip()
    lines.append(text or (
        "No explicit criteria were given. Check that the summary is consistent "
        "with the artifacts, that every artifact the summary relies on exists "
        "and that nothing in the summary is contradicted by the files."))

    lines.extend(["", "### What you see", "", BOUNDARY_NOTE])
    lines.extend(["", "### Answer format", ""])
    lines.extend(ANSWER_SPEC)

    return AgentBrief(
        goal=VERIFIER_GOAL_PREFIX + str(target.run_id or "")
             + " against the criteria in the brief.",
        brief="\n".join(lines),
        artifacts_in={},
    )


def _load_record(service: Any, run_id: str) -> Optional[Dict[str, Any]]:
    """Return a run record as a dict: the live copy first, the store second."""
    rid = str(run_id or "").strip()
    if not rid:
        return None
    try:
        live = service.get_record(rid)
    except Exception:
        live = None
    if live is not None:
        try:
            data = live.to_record()
            # The durable form drops artifacts and the dialog thread on
            # purpose; a live record still holds both, and the verifier needs
            # the thread to join the SAME run tree (the result it must check
            # lives there) plus the artifacts as its evidence.
            data["artifacts"] = list(getattr(live, "artifacts", []) or [])
            data["thread_id"] = str(getattr(live, "thread_id", "") or "")
            return data
        except Exception:
            if isinstance(live, dict):
                return dict(live)
    try:
        return service.record_from_store(rid)
    except Exception:
        return None


def verify_result(
    run_id: str = "",
    criteria: str = "",
    *,
    parent_slug: str = "dev_agent",
    parent_run_id: Optional[str] = None,
    thread_id: str = "",
    parent_config: Optional[Dict[str, Any]] = None,
    base_dir: Optional[str] = None,
    lang: Optional[str] = None,
    service: Any = None,
) -> Dict[str, Any]:
    """Run the configured verifier against one finished run of this tree.

    The checks happen in a fixed order, and every refusal is a CONTRACT
    violation the caller must fix - never a verdict:

    1. an empty ``run_id`` is ``invalid_args``;
    2. no verifier configured is ``verifier_not_configured`` - an agent is
       never substituted silently, because that would hide the fact that
       nobody is appointed to check anything;
    3. an unknown run is ``run_not_found``;
    4. a run that is not terminal is ``run_not_finished`` - the result of a
       running child does not exist yet;
    5. a verifier run is refused with ``invalid_args``: checking a check is
       forbidden, both because it multiplies runs and because a verifier's own
       answer is already a verdict, not a result;
    6. otherwise the verifier is launched through ``service.run``.

    A verifier that FAILS to do its work is not an error of this call: the
    answer is ``ok: True`` with an ``inconclusive`` verdict and the failure
    code as the reason. Only the contract violations above answer
    ``ok: False``.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return error_result(
            "", "verify_result needs run_id: the finished run to check.",
            ERR_INVALID_ARGS, missing=["run_id"])

    verifier = resolve_verifier(parent_slug)
    if not verifier:
        return error_result(
            rid,
            "No verifier is configured for " + str(parent_slug) + ": set "
            "`verifier_agent` in the orchestrator settings.",
            ERR_VERIFIER_NOT_CONFIGURED,
            hint="Assign an employee as the verifier on the Settings -> "
                 "Delegation tab; the tool stays hidden until then.")

    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    record = _load_record(service, rid)
    if record is None:
        return error_result(
            rid, "Unknown run: " + rid + ".", ERR_RUN_NOT_FOUND)

    status = str(record.get("status") or "")
    if status not in TERMINAL_STATUSES:
        return error_result(
            rid, "Run " + rid + " is not finished yet (status: " + status
                 + "): there is no result to verify.",
            ERR_RUN_NOT_FINISHED, status=status)

    # Defence in depth against verifying a verification: the slot check uses
    # the run's own agent AND the goal marker, so re-pointing the verifier
    # setting does not open the door to check a check.
    goal_text = str(record.get("goal") or "")
    if (str(record.get("agent_slug") or "").strip() == verifier
            or goal_text.startswith(VERIFIER_GOAL_PREFIX)):
        return error_result(
            rid, "Run " + rid + " is a verification itself; cannot verify "
                 "a verifier run.",
            ERR_INVALID_ARGS, verifier=verifier)

    target = _result_from_record(record)
    brief = build_verifier_brief(target, criteria)

    # The verifier must join the tree the RESULT lives in, otherwise the
    # limits would be reset by every verification. The dialog thread is the
    # natural key; a stored record may not carry it (the durable form omits
    # it), and then the tree root is the next best key - a verifier hung off
    # another dialog's tree would escape its limits.
    tree_key = (str(thread_id or "").strip()
                or str(record.get("thread_id") or "").strip()
                or str(record.get("root_run_id") or "").strip()
                or rid)
    spawn = SpawnRequest(
        agent_slug=verifier,
        request=brief,
        thread_id=tree_key,
        parent_run_id=parent_run_id,
        background=False,
    )
    launched = service.run(spawn, parent_config=parent_config,
                           base_dir=base_dir, lang=lang)
    if not isinstance(launched, dict) or not launched.get("ok"):
        # A launch refusal (depth, pool, total runs) belongs to the caller:
        # the verification never happened, and a verdict would be a lie.
        payload = dict(launched) if isinstance(launched, dict) else error_result(
            rid, "The verifier could not be launched.", "provider_error")
        payload.setdefault("verifier", verifier)
        return payload

    result = dict(launched.get("result") or {})
    verifier_run_id = str(result.get("run_id") or "")
    summary = str(result.get("summary") or "")
    result_status = str(result.get("status") or "")

    if result_status != STATUS_DONE:
        # The verifier did not produce an answer: that is "could not check",
        # not a failed result and not an error of this call.
        code = str(result.get("error_code") or result_status or "unknown")
        verdict = Verdict(
            target_run_id=rid,
            verifier_run_id=verifier_run_id,
            verdict=VERDICT_INCONCLUSIVE,
            reasons=["the verifier did not finish: " + code],
            checked=[],
            summary=summary,
        )
        return {"ok": True, "verdict": verdict.to_dict(), "verifier": verifier}

    parsed_verdict, reasons, checked = parse_verdict(summary)
    verdict = Verdict(
        target_run_id=rid,
        verifier_run_id=verifier_run_id,
        verdict=parsed_verdict,
        reasons=reasons,
        checked=checked,
        summary=summary,
    )
    return {"ok": True, "verdict": verdict.to_dict(), "verifier": verifier}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
