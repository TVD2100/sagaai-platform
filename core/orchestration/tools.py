# -*- coding: utf-8 -*-
"""
core.orchestration.tools - the delegation tools exposed to the model.

Two tools, one contract:

- spawn_agent launches a CHILD ORCHESTRATOR: a full agent with its own
  tools, its own thread and its own journal. Blocking in v1 - it returns the
  child result once the child finishes.
- invoke_assistant makes ONE model call to an existing assistant (or an
  ephemeral prompt): no tools, no RAG bases, no agent loop.

Rights are enforced here, next to the arguments, so the same refusal is
produced whether the tool is reached through the loop branch or through
dispatch. The rule set is small and explicit:

- spawn_agent is ALWAYS callable: self-delegation needs no configuration, so
  the orchestrator's own slug is allowed by definition. The allowlist
  narrows the circle of OTHER agents only.
- an empty allowlist therefore never produces an empty ``allowed`` list;
  it produces ``allowed: [self]``, ``self_only: true`` and a hint, so the
  model does not spend steps probing for other names.
- invoke_assistant is registered only when the allowlist is non-empty:
  there are no assistants by default, unlike the right to call yourself.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from core.orchestration.contracts import (
    AgentBrief,
    ERR_AGENT_NOT_ALLOWED,
    ERR_ASSISTANT_NOT_ALLOWED,
    ERR_INTERNAL_ERROR,
    ERR_INVALID_ARGS,
    ERR_INVALID_BRIEF,
    ERR_MAX_STEPS,
    ERR_NOT_IMPLEMENTED,
    ERR_PROVIDER_ERROR,
    ERR_RESUME_UNAVAILABLE,
    ERR_RUN_NOT_FOUND,
    ERR_TIMEOUT,
    ERR_UNKNOWN_AGENT,
    ERR_UNKNOWN_ASSISTANT,
    InvokeRequest,
    SpawnRequest,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    error_result,
)
from core.orchestration.resume import RESUMABLE_STATUSES

#: Hard cap of ``subagent_status(wait_seconds=...)``. A tool call that blocks
#: for minutes looks like a hung dialog and spends the parent's steps waiting.
MAX_STATUS_WAIT_SECONDS = 30.0

#: How much journal text one ``read_subagent_state`` call may return.
MAX_STATE_CHARS = 6000


def run_note(run: Dict[str, Any], *, timed_out: bool = False) -> str:
    """Explain what a run status means for the NEXT decision.

    A raw status word is not self-describing: ``failed`` may mean "try again"
    or "never try this again", and ``running`` next to an expired wait looks
    exactly like a hang. The note is attached to tool results instead of
    living in a system prompt, so it reaches every orchestrator without
    anybody editing prompts per agent - and it stays next to the data it
    describes.

    Returns an empty string when the status needs no explanation.
    """
    status = str(run.get("status") or "").strip()
    code = str(run.get("error_code") or "").strip()
    retryable = bool(run.get("retryable", True))
    try:
        steps = int(run.get("steps_used") or 0)
    except (TypeError, ValueError):
        steps = 0

    if timed_out and status == STATUS_RUNNING:
        return ("The wait window expired - the run is STILL RUNNING, not hung. "
                "Call again with wait_seconds, or keep working and check later.")
    if status == STATUS_RUNNING:
        return ("The run is still working. Wait with wait_seconds or check "
                "again later; a background child reports through the mailbox.")
    if status == STATUS_FAILED:
        if code == ERR_INTERNAL_ERROR or not retryable:
            return ("The child loop itself raised after " + str(steps) + " "
                    "step(s): this is a platform defect, not a provider hiccup. "
                    "Re-running the same call fails the same way - do not spawn "
                    "it again; fix the cause or report it as blocked.")
        if code == ERR_MAX_STEPS:
            return ("The child used its whole step budget. Split the task into "
                    "smaller pieces or raise subagent_max_steps: resuming does "
                    "not add steps.")
        if code == ERR_TIMEOUT:
            return ("The child hit its time limit. Narrow the task or raise "
                    "subagent_timeout_seconds.")
        if retryable:
            return ("The child failed and the run is marked retryable: one "
                    "attempt may pass, but change something (narrow the brief) "
                    "instead of repeating the call verbatim.")
        return "The child failed; the run is marked not retryable."
    if status == STATUS_AWAITING_INPUT:
        return ("The run stopped with work unfinished. Read its journal with "
                "read_subagent_state, then continue with resume_subagent.")
    if status == STATUS_CANCELLED:
        return ("The run was cancelled: its partial result is all there is, "
                "and resuming starts a new attempt.")
    if status == STATUS_DONE:
        return "The run finished: read the summary and artifacts."
    return ""


def _attach_note(payload: Dict[str, Any], note: str) -> Dict[str, Any]:
    """Return *payload* with a ``note`` key added (a no-op for empty notes)."""
    if not note or not isinstance(payload, dict) or payload.get("note"):
        return payload
    out = dict(payload)
    out["note"] = note
    return out


def _note_for_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the explanation of a spawn/resume result to its own payload."""
    if not isinstance(payload, dict):
        return payload
    result = payload.get("result")
    if isinstance(result, dict):
        return _attach_note(payload, run_note(result))
    return payload


def _journal_note(sections: Dict[str, str], status: str) -> str:
    """Explain how to read a child journal that came back thin or empty.

    A journal is the child's own record, not the runtime's: a run that was
    told to work read-only, or one that crashed before its first
    ``task_state_init``, answers with scaffolding only. Without this line the
    parent reads "did nothing" where the truth is "never wrote it down" - and
    the runtime paragraph explaining the actual outcome may not be written
    yet while the run is still working.
    """
    real = {name: text for name, text in (sections or {}).items()
            if text and text.strip() not in ("", "_(not set)_")}
    if not real:
        return ("The journal carries no real content. This does not prove the "
                "run did nothing: a child writes a journal only when it "
                "decides to. Check summary and artifacts with "
                "subagent_status first; once the run is finished, a runtime "
                "outcome paragraph is recorded here automatically.")
    if status == STATUS_RUNNING:
        return ("The run is still working: this is a snapshot, not a final "
                "record. The outcome paragraph appears under Handoff when the "
                "run finishes.")
    if "Runtime outcome" not in (sections.get("handoff") or ""):
        return ("The journal has no runtime outcome paragraph: read it together "
                "with the run summary instead of treating the handoff as the "
                "whole story.")
    return ""


def _allowlist_context(parent_slug: str) -> Dict[str, Any]:
    """Return the allowlist view of a parent orchestrator.

    The result always contains the parent itself in ``allowed`` (self-
    delegation is not configurable) plus every configured agent. The
    ``self_only`` flag is set when the configured list is empty: it exists
    to tell the model not to probe for other slugs.
    """
    try:
        from core.orchestrators import get_allowed_agents
        configured = get_allowed_agents(parent_slug)
    except Exception:
        configured = []
    allowed: List[str] = [parent_slug]
    for raw in configured:
        # Normalise here rather than trusting the source: a config written by
        # an import, an older version or a hand-edited folder can carry
        # blanks and duplicates, and this list is what the model is told is
        # callable - a whitespace entry in it is a dead end for the caller.
        slug = str(raw or "").strip()
        if slug and slug not in allowed:
            allowed.append(slug)
    return {"allowed": allowed, "self_only": len(allowed) == 1}


def _available_agents(parent_slug: str) -> List[str]:
    """Return the slugs the parent may address, for an ``unknown_agent`` reply."""
    return list(_allowlist_context(parent_slug)["allowed"])


def check_agent_allowed(parent_slug: str, agent_slug: str) -> Optional[Dict[str, Any]]:
    """Return a refusal payload when *agent_slug* may not be spawned, else None.

    An empty agent slug is a ``invalid_args`` error (the model must name an
    agent); an agent outside the allowlist is ``agent_not_allowed``. The
    refusal always carries ``allowed``; a self-only setup adds ``self_only``
    and ``hint`` so the model sees WHY the list is short.
    """
    slug = (agent_slug or "").strip()
    if not slug:
        return error_result(
            "", "spawn_agent needs agent_slug: the slug of an orchestrator.",
            ERR_INVALID_ARGS, missing=["agent_slug"])
    ctx = _allowlist_context(parent_slug)
    if slug in ctx["allowed"]:
        return None
    details: Dict[str, Any] = {"agent": slug, "allowed": ctx["allowed"]}
    if ctx["self_only"]:
        details["self_only"] = True
        details["hint"] = ("Only your own agent is available. Add other "
                           "orchestrators to allowed_agents to delegate to them.")
    return error_result("", "Agent not allowed: " + slug + ".",
                        ERR_AGENT_NOT_ALLOWED, **details)


def check_assistant_allowed(parent_slug: str,
                            assistant_id: str) -> Optional[Dict[str, Any]]:
    """Return a refusal payload when the assistant may not be called, else None."""
    aid = (assistant_id or "").strip()
    try:
        from core.orchestrators import get_allowed_assistants
        allowed = get_allowed_assistants(parent_slug)
    except Exception:
        allowed = []
    if aid in allowed:
        return None
    return error_result("", "Assistant not allowed: " + aid + ".",
                        ERR_ASSISTANT_NOT_ALLOWED, assistant=aid,
                        allowed=list(allowed))


def _validate_brief(request: Any) -> Optional[Dict[str, Any]]:
    """Return a refusal payload for a malformed brief, else None."""
    if not isinstance(request, AgentBrief):
        return error_result(
            "", "spawn_agent needs a brief object with at least a goal.",
            ERR_INVALID_BRIEF, expected=["goal", "brief", "artifacts_in"])
    if not str(request.goal or "").strip():
        return error_result("", "The brief has an empty goal: say what the subagent must do.",
                            ERR_INVALID_BRIEF, missing=["goal"])
    return None


def spawn_agent(
    agent_slug: Optional[str] = None,
    request: Optional[AgentBrief] = None,
    inline_orchestrator: Optional[dict] = None,
    *,
    background: bool = False,
    resume_run_id: Optional[str] = None,
    parent_slug: str = "dev_agent",
    parent_run_id: Optional[str] = None,
    thread_id: str = "",
    parent_config: Optional[Dict[str, Any]] = None,
    base_dir: Optional[str] = None,
    lang: Optional[str] = None,
    service=None,
    enable_injection_protection: Optional[bool] = None,
) -> Dict[str, Any]:
    """Launch a child orchestrator and return its structured result.

    Blocking by default: the call returns once the child finishes, so the
    parent sees a predictable result in the same step. With ``background=True``
    the child runs on its own thread and the call returns its ``run_id`` at
    once; the finish reaches the dialog later as a mailbox notice, and the
    caller observes it through ``subagent_status``. Either way the refusals
    (depth, tree total, pool slot) are raised synchronously, so a caller never
    receives a run id for work that will not happen.

    ``resume_run_id`` turns the call into a continuation of that run: the child
    inherits the tree, the depth and the budget of the original, and continues
    from the handoff in its own journal instead of starting over.

    ``inline_orchestrator`` is reserved for phase 2 and answers
    ``not_implemented``; the parameter exists so the phase-2 contract does
    not change the call shape.

    ``enable_injection_protection`` is the parent's prompt-injection gate
    setting. It is an internal caller argument, not a model-facing one: the
    agent loop reads it from the live parent state and passes it here, so a
    child inherits the mode of the run that started it. ``None`` leaves the
    decision to the service (resume inheritance or the safe default).
    """
    if inline_orchestrator is not None:
        return error_result(
            "", "inline_orchestrator is not implemented in v1: pass the slug "
            "of an existing orchestrator in agent_slug.",
            ERR_NOT_IMPLEMENTED, planned="phase 2")

    refusal = check_agent_allowed(parent_slug, agent_slug or "")
    if refusal is not None:
        return refusal

    brief_refusal = _validate_brief(request)
    if brief_refusal is not None:
        return brief_refusal
    brief: AgentBrief = request  # type: ignore[assignment]

    # An unknown slug is a caller mistake, not a rights problem: report it
    # with the list of slugs this caller may actually address.
    try:
        from core.orchestrators import get_orchestrator
        target = get_orchestrator((agent_slug or "").strip())
    except Exception:
        target = None
    if not target:
        return error_result(
            "", "Agent not found: " + str(agent_slug) + ".",
            ERR_UNKNOWN_AGENT, agent=agent_slug,
            available=_available_agents(parent_slug))

    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    spawn = SpawnRequest(
        agent_slug=(agent_slug or "").strip(),
        request=brief,
        thread_id=thread_id or "",
        parent_run_id=parent_run_id,
        background=bool(background),
        resume_run_id=(resume_run_id or "").strip() or None,
        enable_injection_protection=enable_injection_protection,
    )
    if background:
        # ``start`` validates synchronously and only then moves the child to
        # its own thread, so a refusal still reaches the caller in this step.
        out = service.start(spawn, parent_config=parent_config,
                            base_dir=base_dir, lang=lang)
        if isinstance(out, dict) and out.get("ok"):
            out = _attach_note(out, "The child runs in the background: observe it "
                                     "with subagent_status(run_id=...) or wait for "
                                     "the finish notice. Do not spawn it again.")
        return out
    return _note_for_payload(service.run(spawn, parent_config=parent_config,
                                         base_dir=base_dir, lang=lang))


def invoke_assistant(
    assistant_id: Optional[str] = None,
    user: str = "",
    context_files: Optional[List[str]] = None,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    *,
    parent_slug: str = "dev_agent",
    parent_run_id: Optional[str] = None,
    parent_config: Optional[Dict[str, Any]] = None,
    base_dir: Optional[str] = None,
    lang: Optional[str] = None,
    service=None,
    invoke_fn=None,
) -> Dict[str, Any]:
    """Make one model call to an assistant or to an ephemeral prompt.

    One step, no agent loop: the assistant's tools and RAG bases are NOT
    applied, and nothing is truncated - an oversized context is refused with
    ``context_too_large``. The result carries ``assistant: {id, name}`` so the
    feed shows who answered instead of an eight-character id.
    """
    if assistant_id and system is not None:
        return error_result(
            "", "assistant_id and system are mutually exclusive.",
            ERR_INVALID_ARGS, conflicting=["assistant_id", "system"])
    if not assistant_id and system is None:
        return error_result(
            "", "Pass assistant_id (an existing assistant) or system (an "
            "ephemeral prompt); at least one is required.",
            ERR_INVALID_ARGS, missing=["assistant_id|system"])
    if not str(user or "").strip() and not context_files:
        return error_result(
            "", "Nothing to analyse: pass user text and/or context_files.",
            ERR_INVALID_ARGS, missing=["user|context_files"])

    if assistant_id:
        refusal = check_assistant_allowed(parent_slug, assistant_id)
        if refusal is not None:
            return refusal
        try:
            from core.assistants import get_assistant_by_id
            card = get_assistant_by_id((assistant_id or "").strip())
        except Exception:
            card = None
        if not card:
            return error_result(
                "", "Unknown assistant id: " + str(assistant_id) + ".",
                ERR_UNKNOWN_ASSISTANT, assistant=assistant_id)

    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    call = InvokeRequest(
        assistant_id=(assistant_id or "").strip() or None,
        user=str(user or ""),
        context_files=list(context_files or []),
        system=system,
        model=model,
        max_tokens=max_tokens,
        parent_run_id=parent_run_id,
    )
    return service.invoke(call, parent_config=parent_config,
                          parent_slug=parent_slug, base_dir=base_dir,
                          lang=lang, invoke_fn=invoke_fn)


def subagent_status(run_id: Optional[str] = None, *,
                    root_run_id: Optional[str] = None,
                    wait_seconds: float = 0.0,
                    service=None) -> Dict[str, Any]:
    """Report the state of one run, of a tree, or wait briefly for it.

    One tool covers three questions the model actually asks, because three
    tools with overlapping answers would make it guess:

    * ``run_id`` alone - what is happening with that child (live record first,
      durable record second, so a child that outlived its tree is still
      answered);
    * ``root_run_id`` alone - every run of that tree, newest first;
    * ``run_id`` with ``wait_seconds`` - block up to that many seconds for a
      terminal status. Waiting is capped by ``MAX_STATUS_WAIT_SECONDS`` on
      purpose: a tool call that blocks for minutes looks like a hung dialog
      and burns the parent's step budget while it waits.

    A timeout is not an error: the run comes back with ``timed_out: True`` so
    the model can decide to wait again, do something else, or report progress.
    """
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    rid = str(run_id or "").strip()
    tree = str(root_run_id or "").strip()
    if not rid and not tree:
        return error_result(
            "", "subagent_status needs run_id (one run) or root_run_id (a tree).",
            ERR_INVALID_ARGS, missing=["run_id|root_run_id"])

    if rid and not tree:
        wait_for = max(0.0, min(float(wait_seconds or 0.0), MAX_STATUS_WAIT_SECONDS))
        timed_out = False
        if wait_for > 0:
            items = service.wait([rid], timeout=wait_for)
            item = dict(items[0]) if items else {}
            run = dict(item.get("run") or {})
            timed_out = bool(run.get("timed_out"))
        else:
            item = service.poll(rid)
        run = dict(item.get("run") or {})
        if not item.get("ok"):
            # A refusal from poll is the documented run_not_found payload.
            return item if item.get("error_code") else error_result(
                "", str(item.get("error") or "Unknown run."), ERR_RUN_NOT_FOUND)
        payload = {"ok": True, "run": run, "status": str(run.get("status") or ""),
                   "background": bool(run.get("background")),
                   "resumable": str(run.get("status") or "") in RESUMABLE_STATUSES}
        return _attach_note(payload, run_note(run, timed_out=timed_out))

    runs = service.list_runs(tree or None)
    tree = tree or (str((runs[0] or {}).get("root_run_id") or "") if runs else "")
    if rid:
        runs = [r for r in runs if str(r.get("run_id") or "") == rid]
    payload = {"ok": True, "root_run_id": tree, "count": len(runs), "runs": runs}
    unfinished = [r for r in runs
                  if str(r.get("status") or "") not in (STATUS_DONE, STATUS_FAILED,
                                                         STATUS_CANCELLED)]
    if unfinished:
        payload = _attach_note(payload, str(len(unfinished)) + " of " + str(len(runs))
                               + " run(s) of this tree are not terminal yet: the tree "
                               "cannot be reported as finished while they run.")
    return payload


def read_subagent_state(run_id: Optional[str] = None, *,
                        section: Optional[str] = None,
                        service=None) -> Dict[str, Any]:
    """Read the journal of a finished (or running) child run.

    Without this the parent can only see a summary string: the steps the
    child took, what it decided and what it left open live in the child's own
    journal, whose path the record carries. Reading it by absolute path is
    the whole point - the ordinary journal reader resolves the file from the
    process-wide active thread id and would hand back the PARENT's journal.

    ``section`` narrows the answer to one of task/architecture/plan/progress/
    handoff/analysis/requests; without it the sections are returned whole,
    trimmed to ``MAX_STATE_CHARS`` so a grown journal cannot flood the
    parent's context.
    """
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()
    rid = str(run_id or "").strip()
    if not rid:
        return error_result("", "read_subagent_state needs run_id: the run whose "
                            "journal you want to read.", ERR_INVALID_ARGS,
                            missing=["run_id"])
    record = None
    live = service.get_record(rid)
    if live is not None:
        record = live.to_record()
    if record is None:
        record = service.record_from_store(rid)
    if record is None:
        return error_result(rid, "Unknown run: " + rid + ".", ERR_RUN_NOT_FOUND)

    path = str(record.get("journal_path") or "").strip()
    if not path or not os.path.isfile(path):
        return error_result(
            rid, "Run " + rid + " has no journal file yet.",
            ERR_RESUME_UNAVAILABLE, journal_path=path,
            note=("A child writes its journal only when it decides to. An empty "
                  "result here does not prove it did nothing: check the run "
                  "summary and artifacts with subagent_status first, and read "
                  "the journal once the run has finished - its outcome is "
                  "recorded there at the end."))

    try:
        from dev_agent.task_state import read_task_state_at
        state = read_task_state_at(path)
    except Exception as exc:
        return error_result(rid, "Journal read failed: " + str(exc),
                            ERR_PROVIDER_ERROR, journal_path=path)
    if not state.get("ok"):
        return error_result(rid, str(state.get("error") or "Journal unreadable."),
                            ERR_PROVIDER_ERROR, journal_path=path)

    sections = dict(state.get("sections") or {})
    wanted = str(section or "").strip().lower()
    if wanted:
        if wanted not in sections:
            return error_result(
                "", "Unknown journal section: " + wanted + ".",
                ERR_INVALID_ARGS, section=wanted,
                available=sorted(sections.keys()))
        return {"ok": True, "run_id": rid, "journal_path": path,
                "section": wanted, "text": sections.get(wanted, "")}

    trimmed: Dict[str, str] = {}
    budget = MAX_STATE_CHARS
    for name in ("task", "architecture", "plan", "progress", "handoff",
                 "analysis", "requests"):
        text = str(sections.get(name) or "")
        if not text:
            continue
        take = min(len(text), max(0, budget))
        trimmed[name] = text[:take]
        budget -= take
        if budget <= 0:
            break
    return {"ok": True, "run_id": rid, "journal_path": path,
            "sections": trimmed, "step_ids": list(state.get("step_ids") or []),
            "status": str(record.get("status") or ""),
            "note": _journal_note(trimmed, str(record.get("status") or ""))}


def resume_subagent(run_id: Optional[str] = None, *,
                    request: Optional[AgentBrief] = None,
                    background: bool = False,
                    parent_slug: str = "dev_agent",
                    parent_run_id: Optional[str] = None,
                    thread_id: str = "",
                    parent_config: Optional[Dict[str, Any]] = None,
                    base_dir: Optional[str] = None,
                    lang: Optional[str] = None,
                    service=None,
                    enable_injection_protection: Optional[bool] = None,
                    ) -> Dict[str, Any]:
    """Continue a finished child run from its own journal.

    The new run inherits the tree of the original (see ``resume_run_id`` in the
    service), so resuming never resets the depth or the total-run counter, and
    the chain is capped by ``MAX_RESUME_CHAIN``. ``request`` is optional: a
    caller that supplies nothing continues the original goal with the built
    continuation hint, which is the common case ("the child stopped at a
    question, let it continue").
    """
    from core.orchestration.resume import build_resume

    if service is None:
        from core.orchestration.service import get_service
        service = get_service()
    rid = str(run_id or "").strip()
    if not rid:
        return error_result("", "resume_subagent needs run_id: the run to continue.",
                            ERR_INVALID_ARGS, missing=["run_id"])

    record = None
    live = service.get_record(rid)
    if live is not None:
        record = live.to_record()
    if record is None:
        record = service.record_from_store(rid)
    if record is None:
        return error_result(rid, "Nothing to resume: run " + rid + " is unknown.",
                            ERR_RESUME_UNAVAILABLE)

    try:
        from dev_agent.task_state import read_task_state_at
        built = build_resume(record, read_state=read_task_state_at)
    except Exception as exc:  # pragma: no cover - defensive
        return error_result(rid, "Resume planning failed: " + str(exc),
                            ERR_PROVIDER_ERROR)
    if not built.get("ok"):
        payload = error_result(rid,
                               str(built.get("error") or "Nothing to resume."),
                               str(built.get("error_code") or ERR_RESUME_UNAVAILABLE))
        payload.update({k: v for k, v in built.items()
                        if k not in ("ok", "error", "error_code")})
        code = str(built.get("error_code") or "")
        if code == "resume_chain_limit":
            payload = _attach_note(
                payload, "The resume chain reached its ceiling: continuing this "
                         "line of work needs a fresh spawn with a narrowed brief "
                         "or a decision from the user, not another resume.")
        elif code == ERR_RESUME_UNAVAILABLE:
            payload = _attach_note(
                payload, "There is no journal to continue from. Start a fresh "
                         "spawn and put everything the child needs into goal and "
                         "brief - a resume cannot invent the missing state.")
        else:
            payload = _attach_note(
                payload, "Resume was refused. Read the code above: a retry with "
                         "the same request will be refused the same way.")
        return payload

    agent = str((request.goal if request else "") or "").strip()
    if request is not None and agent:
        brief = request
    else:
        original = request or AgentBrief(goal=str(built.get("goal") or ""))
        brief = AgentBrief(
            goal=str(original.goal or built.get("goal") or "Continue the task."),
            brief=str(built.get("resume_hint") or ""),
            artifacts_in=dict(getattr(original, "artifacts_in", {}) or {}),
        )

    return _note_for_payload(spawn_agent(
        agent_slug=str(record.get("agent_slug") or "") or parent_slug,
        request=brief,
        background=background,
        resume_run_id=rid,
        parent_slug=parent_slug,
        parent_run_id=parent_run_id,
        thread_id=thread_id,
        parent_config=parent_config,
        base_dir=base_dir,
        lang=lang,
        service=service,
        enable_injection_protection=enable_injection_protection,
    ))


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
