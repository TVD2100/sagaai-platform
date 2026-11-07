# -*- coding: utf-8 -*-
"""core.orchestration.tools_catalog - catalog entries of the tools.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Dict


def ask_parent_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``ask_parent``.

    The description states the three things a child gets wrong about asking:
    it stops the turn, the answer continues the SAME run, and the question is
    not a status report.
    """
    return {
        "name": "ask_parent",
        "desc": (
            "Ask the agent that launched you a question you cannot answer "
            "from the brief, and stop at this step boundary until it replies. "
            "Args: question (the decision you need, short), [options] (the "
            "answers you can act on), [done] (what is already finished), "
            "[blocked] (what you cannot do without the answer). Your run is "
            "parked, not finished: the answer arrives as a user message and "
            "you continue with the same files, context and step budget. Use "
            "it for a decision, not for a progress report; the number of "
            "question/answer pairs per run is limited."
        ),
    }

def answer_child_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``answer_child``."""
    return {
        "name": "answer_child",
        "desc": (
            "Answer a subagent that parked its run with ask_parent and let it "
            "continue the SAME run. Args: run_id (the parked subagent), answer "
            "(the decision or the missing fact; be concrete - the subagent "
            "acts on it). Returns either the subagent result or its next "
            "question. If you do not know the answer, ask the user yourself "
            "first and pass their decision on: the subagent cannot reach the "
            "user. Only runs of your own tree can be answered."
        ),
    }

def spawn_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``spawn_agent``.

    The description states the boundaries the model must know: the child is a
    full agent (unlike a one-shot assistant call), the call is blocking unless
    ``background`` is set, and self-delegation needs no configuration.
    """
    return {
        "name": "spawn_agent",
        "desc": (
            "Launch a subagent (a full agent with its own tools, "
            "thread and journal) and get its structured result. Args: "
            "agent_slug (the orchestrator slug; your own slug is always "
            "allowed), request {goal, brief, artifacts_in}, "
            "[background], [resume_run_id]. Blocking by default: returns "
            "once the subagent finishes. With background=true it returns a "
            "run_id at once and the outcome arrives as a "
            "[SUBAGENT_NOTICE:run:status] message - use subagent_status to "
            "poll it. Several calls in ONE message run in parallel. The "
            "subagent gets a CLEAN history - put everything it needs into "
            "goal and brief, and give it ABSOLUTE paths: the subagent "
            "starts in your workspace and its own dialog has no uploads, so "
            "a relative path or a bare file name is a dead end. Limits "
            "(max_depth, max_active_subagents, total_subagent_runs) are read "
            "from your config, not from the call."
        ),
    }

def subagent_status_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``subagent_status``."""
    return {
        "name": "subagent_status",
        "desc": (
            "Check a subagent run. Args: [run_id] (one run), "
            "[root_run_id] (every run of a tree), [wait_seconds] (block up "
            "to 30s for a terminal status when run_id is given). A timeout "
            "is not an error: the run comes back with timed_out=true. Use "
            "this for background children instead of calling spawn_agent "
            "again."
        ),
    }

def read_subagent_state_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``read_subagent_state``."""
    return {
        "name": "read_subagent_state",
        "desc": (
            "Read the task journal of a subagent run: what it planned, what "
            "it finished, what it left open. Args: run_id, [section] "
            "(task/architecture/plan/progress/handoff/analysis/requests). "
            "Read it before resuming a child or when its summary is not "
            "enough detail - it is the child's own record, not a guess."
        ),
    }

def resume_subagent_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``resume_subagent``."""
    return {
        "name": "resume_subagent",
        "desc": (
            "Continue a finished subagent run from its own journal instead "
            "of starting over: already-finished work is not redone. Args: "
            "run_id, [request {goal, brief}] (omit to continue the original "
            "goal), [background]. The new run keeps the original tree, depth "
            "and run counters; a resume chain is capped, so repeated "
            "resuming is refused with resume_chain_limit."
        ),
    }

def read_subagent_thread_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``read_subagent_thread``."""
    return {
        "name": "read_subagent_thread",
        "desc": (
            "Read the CONVERSATION of a subagent run - what it actually saw "
            "and said, not just what it planned, and it survives the death "
            "of the process that ran the child. Args: run_id, [mode=window], "
            "[offset=0], [limit=50], [include_tool_results=false], [index], "
            "[before=5], [after=5]. Modes: 'window' pages from offset; "
            "'tail' returns the LAST messages (use it to see where a failed "
            "or cancelled child stopped); 'around' returns the messages "
            "around an absolute [index] (pair it with a search hit); 'meta' "
            "returns counts, sizes and token cost without reading content. "
            "Windows carry absolute message indices for pagination. Only "
            "runs of your own tree are readable."
        ),
    }

def cancel_subagent_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``cancel_subagent``."""
    return {
        "name": "cancel_subagent",
        "desc": (
            "Stop a running subagent instead of waiting for its timeout. "
            "Args: run_id, [cascade=true] (also stop its descendants), "
            "[reason] (why - it travels into the notice). Use it when a "
            "child misunderstood the task, loops on a wrong path or is no "
            "longer needed. Cancellation is a flag checked at step "
            "boundaries: a tool call already in flight finishes first. You "
            "may only cancel runs of your own tree."
        ),
    }

def subagent_result_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``subagent_result``.

    The description separates the two questions the model would otherwise
    merge: ``subagent_status`` says what is happening, this tool says what the
    run PRODUCED. It also states the one rule the answer enforces - an
    unfinished run returns no result - because a model that expects a partial
    summary would read ``ready: false`` as a failure.
    """
    return {
        "name": "subagent_result",
        "desc": (
            "Fetch what a FINISHED subagent run produced: the final text, "
            "the artifacts and the resources used - the same result object a "
            "blocking spawn_agent returns, for a background or blocking run "
            "alike. Args: run_id, [wait_seconds] (block up to 30s for the run "
            "to finish). While the run is not terminal the answer is "
            "ready=false with its current status and NO result: partial work "
            "is never returned as a result. Use subagent_status to watch a "
            "run, this tool to collect it."
        ),
    }

def sleep_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``sleep``.

    The description answers the question the model asks right after a
    background launch: what to do while the child works. It states the two
    stop conditions, the identifier-only result and the fact that a timeout
    ends the wait without touching the child.
    """
    return {
        "name": "sleep",
        "desc": (
            "Sleep until one of your background children finishes, or until the "
            "timeout expires. Args: seconds (the ceiling, max 3600 = one "
            "hour; a child agent can work for many minutes), until "
            "(\"any_child\" by default, \"all_children\" to wait for the whole "
            "fan-out at once, or \"timeout\" to wait the full time "
            "regardless of events). Returns woken_by (event | all_done | "
            "timeout | cancelled) and finished: the run_ids that became terminal while "
            "you slept. It does NOT return their results - fetch those with "
            "subagent_status(run_id=...) or read_subagent_state(run_id=...). "
            "A timeout does NOT cancel anything: it only ends your wait. "
            "Prefer this over polling subagent_status in a loop."
        ),
    }

def verify_result_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``verify_result``.

    The description carries the two boundaries the model would otherwise get
    wrong: the verifier sees the RESULT, not the reasoning (so vague criteria
    are useless), and ``inconclusive`` is not a success. Without those two
    sentences the tool turns into a rubber stamp.
    """
    return {
        "name": "verify_result",
        "desc": (
            "Verify the result of one finished subagent run with a separate "
            "verifier agent. Args: run_id (the run to check), criteria (what "
            "the result must satisfy; be concrete - the verifier sees only "
            "the result, not the reasoning). The verifier does not see the "
            "target's transcript; it reads the artifacts itself. Returns "
            "verdict: passed | failed | inconclusive. \"inconclusive\" means "
            "the result could not be checked - never report it as success."
        ),
    }

def invoke_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``invoke_assistant``.

    The description spells out the one-shot boundary: no tools, no RAG bases,
    no agent loop. Without that sentence the behaviour would drift silently
    as soon as someone moves code around.
    """
    return {
        "name": "invoke_assistant",
        "desc": (
            "One-shot call to an existing assistant or to an ephemeral prompt: "
            "a SINGLE model call, no agent loop. Args: assistant_id (an "
            "assistant from allowed_assistants) OR system (ephemeral prompt; "
            "mutually exclusive with assistant_id), user, [context_files], "
            "[model], [max_tokens]. The assistant's tools and knowledge "
            "bases are NOT applied; for tool work use spawn_agent with an "
            "orchestrator. Pass a document as a file in context_files with "
            "an ABSOLUTE path (PDF, DOCX, PPTX, XLSX, RTF, ODT, HTML, text) "
            "instead of pasting its text or splitting it into several calls: "
            "one file - one call, the whole document reaches the assistant. "
            "An oversized context is refused, never truncated."
        ),
    }

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
