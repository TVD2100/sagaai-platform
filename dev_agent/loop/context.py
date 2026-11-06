# -*- coding: utf-8 -*-
"""
dev_agent.loop.context -- what the loop injects into the request and how much.

Two jobs that both decide what the model sees on the next step:

* **injected context** -- the per-thread task-state journal and the dialog's
  artifacts folder. Both are appended to ``state.history`` as hidden system
  snapshots and only when their content changed, so the wire sequence of
  request N+1 is a strict extension of request N and a provider prefix cache
  stays reusable. A snapshot that slid out of the sent window is re-appended
  at the tail, never rewritten in place.
* **economy mode** -- the reduced history, its compact metadata message, the
  token budget with hysteresis and the cache-window anchor that survives the
  loop state between user turns.

Names that a test (or the UI) may replace ON THE FACADE are read through
``_facade_attr`` at call time: ``_maybe_task_state_context``,
``_maybe_thread_context`` and ``_get_economy_tail_messages`` are patched
there, and a snapshot refresher that called its own local copy would ignore
the replacement and inject the real journal into a test that asked for none.
"""
from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from dev_agent.loop.results import _TOOL_RESULT_PREFIX as _TOOL_RESULT_PREFIX
from dev_agent.loop.signals import _FINAL_PHRASE as _FINAL_PHRASE
from dev_agent.loop.signals import _prose_contains_progress as _prose_contains_progress

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from dev_agent.loop.state import AgentLoopState


def _facade_attr(name: str, default: Any) -> Any:
    """Return an attribute of the loop facade, or *default* when it is gone.

    The facade re-exports the helpers of this module and the tests replace
    some of them there. Reading the attribute at call time keeps that
    replacement effective without reaching into the facade at import time.
    """
    try:
        from dev_agent import agent_loop
    except Exception:  # pragma: no cover - the facade is always importable here
        return default
    return getattr(agent_loop, name, default)


# ── Task-state external memory (per-thread journal) ───────────────────────────
# For every task the agent keeps a per-thread task-state journal
# (.dev_agent/task_states/TASK_STATE__<thread_id>.md: Active Task +
# Task History). The Active Task is injected as a system message at the
# END of every request, so the agent always knows where it is even when
# economy mode truncates history.

def _maybe_task_state_context() -> Optional[str]:
    """Return the task-state block for context injection, or None."""
    try:
        from dev_agent import task_state as ts
        return ts.task_state_for_context()
    except Exception:
        return None


# ── Append-only context snapshots (task state / thread context) ───────────
# Cache-friendly design: snapshots are NOT re-injected between the economy
# window and the step payload on every request (that ephemeral seam broke
# the byte-stable prefix providers cache). Instead each snapshot is appended
# to ``state.history`` as a hidden system message, and only when its content
# actually changed, so the wire sequence of request N+1 is a strict
# extension of request N. If the latest snapshot of a kind slides out of the
# sent window, one fresh copy is appended to the tail again (append-only:
# past messages are never rewritten).

_SNAPSHOT_TS = "task_state"
_SNAPSHOT_TC = "thread_context"
_SNAPSHOT_TS_HEAD = "CURRENT TASK STATE"
_SNAPSHOT_TC_HEAD = "## CURRENT THREAD ARTIFACTS DIR"
_SNAPSHOT_SUPERSEDE_NOTE = (
    "\n\nNote: if several snapshots of this kind appear, the "
    "LAST one is current."
)


def _snapshot_kind_of(msg: Dict[str, Any]) -> Optional[str]:
    """Return the snapshot kind of a history message, or None.

    In memory the kind comes from the ``_snapshot_kind`` marker; after a
    DB round-trip (the marker is not persisted) it is recognised by the
    stable content prefix of the block.
    """
    if not isinstance(msg, dict) or msg.get("role") != "system":
        return None
    kind = msg.get("_snapshot_kind")
    if kind in (_SNAPSHOT_TS, _SNAPSHOT_TC):
        return kind
    content = str(msg.get("content") or "")
    if content.startswith(_SNAPSHOT_TS_HEAD):
        return _SNAPSHOT_TS
    if content.startswith(_SNAPSHOT_TC_HEAD):
        return _SNAPSHOT_TC
    return None


def _last_snapshot(history: List[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    """Return the latest snapshot message of *kind* in *history*, or None."""
    for msg in reversed(history):
        if _snapshot_kind_of(msg) == kind:
            return msg
    return None


def _snapshot_content(text: str) -> str:
    """Final content of a snapshot block (block text + supersede note)."""
    return (text or "").rstrip() + _SNAPSHOT_SUPERSEDE_NOTE


def _append_context_snapshot(state: "AgentLoopState", kind: str,
                             text: str) -> Dict[str, Any]:
    """Append ONE hidden system snapshot to the history chain."""
    now_ts = _facade_attr("_now_ts", None)
    entry: Dict[str, Any] = {
        "role": "system",
        "content": _snapshot_content(text),
        "hidden": True,
        "ts": now_ts() if callable(now_ts) else "",
        "_snapshot_kind": kind,
        "_index": state.next_index,
        "_category": "system_notice",
        "_summary": f"context snapshot: {kind}",
    }
    state.next_index += 1
    state.history.append(entry)
    return entry


def refresh_context_snapshots(state: "AgentLoopState") -> int:
    """Append changed task-state / thread-context snapshots to history.

    Called at the start of every LLM step BEFORE the economy window is
    built. Each kind is appended only when its content changed, so an
    unchanged step adds nothing and the request prefix stays byte-stable.
    The latest snapshot is guaranteed to be in the outgoing payload via
    ``_ensure_snapshot_visibility``. Returns the number of appended
    snapshots.
    """
    task_state_ctx = _facade_attr("_maybe_task_state_context",
                                  _maybe_task_state_context)
    thread_ctx = _facade_attr("_maybe_thread_context", _maybe_thread_context)
    appended = 0
    for kind, produce in (
        (_SNAPSHOT_TS, lambda: task_state_ctx()),
        (_SNAPSHOT_TC, lambda: thread_ctx(state)),
    ):
        try:
            text = produce()
        except Exception:
            text = None
        if not text:
            continue
        content = _snapshot_content(text)
        last = _last_snapshot(state.history, kind)
        if last is not None and str(last.get("content") or "") == content:
            continue
        _append_context_snapshot(state, kind, text)
        appended += 1
    return appended


def _ensure_snapshot_visibility(state: "AgentLoopState",
                                effective_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Re-append the latest snapshot of each kind when it fell out of the window.

    Append-only: a fresh copy with the same content goes to the END of both
    the history chain and the outgoing list, so the request prefix grows
    instead of being rewritten. No-op when the snapshot is already present.
    """
    for kind in (_SNAPSHOT_TS, _SNAPSHOT_TC):
        last = _last_snapshot(state.history, kind)
        if last is None or last in effective_history:
            continue
        copy = dict(last)
        copy["_index"] = state.next_index
        state.next_index += 1
        state.history.append(copy)
        effective_history.append(copy)
    return effective_history


# ── Thread context (thread_id + thread files dir) ─────────────────────────
# Injected alongside the task state so the model always knows the folder
# where "non-project" artifacts of the current dialog must be saved (see the
# "Current thread artifacts" section of the system prompt).

def _maybe_thread_context(state: "AgentLoopState") -> Optional[str]:
    """Return the thread context block for injection, or None."""
    tid = (getattr(state, "thread_id", "") or "").strip()
    if not tid:
        return None
    try:
        from core.paths import get_thread_dir
        files_dir = os.path.join(get_thread_dir(tid), "files")
    except Exception:
        files_dir = ""
    lines = [
        "## CURRENT THREAD ARTIFACTS DIR",
        "",
        f"thread_id: {tid}",
    ]
    if files_dir:
        lines.append(f"thread_files_dir: {files_dir}")
    lines.append(
        "Files created during this task that do not belong to any existing "
        "or newly created project folder must be saved into thread_files_dir."
    )
    # Dialog uploads (history/<tid>/files) are reported as FACTS only. The
    # platform never parses their content - the model decides itself when
    # and how to read/extract it (list_thread_files / read_thread_file for
    # text, run_code for other formats).
    try:
        from core.files import build_thread_files_notice
        from core.threads_devagent import list_thread_files
        notice = build_thread_files_notice(
            list_thread_files(tid),
            header="Dialog uploads available to this thread:",
        )
    except Exception:
        notice = ""
    if notice:
        lines.append("")
        lines.append(notice)
    return "\n".join(lines)


# ── Economy mode helpers ─────────────────────────────────────────────────────

_CATEGORY_PLAN = "plan"
_CATEGORY_TOOL_CALL = "tool_call"
_CATEGORY_TOOL_RESULT = "tool_result"
_CATEGORY_PROGRESS = "progress"
_CATEGORY_ERROR = "error"
_CATEGORY_SUMMARY = "summary"
_CATEGORY_USER_TASK = "user_task"
_CATEGORY_OTHER = "other"


def _auto_continue_prefix() -> str:
    """Return the AUTO_CONTINUE marker the facade defines."""
    value = _facade_attr("_AUTO_CONTINUE_PREFIX", "AUTO_CONTINUE:")
    return value if isinstance(value, str) else "AUTO_CONTINUE:"


def _make_short_summary(msg: Dict[str, Any], category: str) -> str:
    """Return a one-line summary of a history message for the index."""
    content = str(msg.get("content", ""))
    auto_prefix = _auto_continue_prefix()
    if content.startswith(_TOOL_RESULT_PREFIX):
        try:
            data = json.loads(content)
            tr = data.get("tool_result", {})
            tool_name = tr.get("tool", "?")
            ok = tr.get("ok", False)
            if not ok:
                return f"Error in {tool_name}: {tr.get('error', 'unknown')[:60]}"
            path = tr.get("path", "")
            if path:
                return f"File {path} {'applied' if tr.get('applied') else 'updated'}"
            return f"{tool_name} completed"
        except Exception:
            pass
        return content[:80]
    if content.startswith(auto_prefix):
        return content[len(auto_prefix):].strip()[:80]
    if msg.get("role") == "assistant":
        lines = [line.strip() for line in content.split("\n") if line.strip()
                 and not line.strip().startswith("```") and not line.strip().startswith("{")]
        if lines:
            return lines[0][:80]
        return content[:80]
    return content[:80]


def _classify_message(msg: Dict[str, Any]) -> str:
    """Return the index category of a history message."""
    role = msg.get("role", "")
    content = str(msg.get("content", ""))
    if role == "user":
        if content.startswith(_TOOL_RESULT_PREFIX):
            try:
                data = json.loads(content)
                tr = data.get("tool_result", {})
                if not tr.get("ok"):
                    return _CATEGORY_ERROR
            except Exception:
                pass
            return _CATEGORY_TOOL_RESULT
        if content.startswith(_auto_continue_prefix()):
            return _CATEGORY_PROGRESS
        return _CATEGORY_USER_TASK
    if _FINAL_PHRASE.search(content):
        return _CATEGORY_PROGRESS
    if _prose_contains_progress(content):
        return _CATEGORY_PROGRESS
    if re.search(r"\b(plan|план)\b", content, re.IGNORECASE) and re.search(r"\d+\.", content):
        return _CATEGORY_PLAN
    return _CATEGORY_OTHER


def _index_message(msg: Dict[str, Any], index: int, category: str) -> None:
    """Attach the index, category and summary to a history message in place."""
    msg["_index"] = index
    msg["_category"] = category
    msg["_summary"] = _make_short_summary(msg, category)


def _get_economy_tail_messages() -> int:
    """Return the economy tail length from the orchestrator configuration.

    Reads the value from the active orchestrator config. Falls back to
    the default from the external JSON config file if not stored yet.
    """
    try:
        from core.orchestrators import get_economy_tail_messages as _g
        return _g()
    except Exception:
        # Bootstrap / import-time fallback
        from core.config import get_default_economy_tail_messages
        return get_default_economy_tail_messages()


def _economy_meta_key(state: "AgentLoopState") -> str:
    """Return a key identifying the meta-parameters of the economy window.

    Used to detect changes in economy-mode environment (workspace or
    web-search flag) that require resetting the cache anchor.
    """
    ws_info = state.workspace_info or "(workspace not yet queried)"
    web_search = "enabled" if state.web_search_enabled else "disabled"
    return f"{ws_info}|{web_search}"


def _make_meta_msg(state: "AgentLoopState") -> Dict[str, Any]:
    """Build the system message prepended in economy mode.

    Contains only metadata that is static within an economy window so the
    prefix stays cacheable: workspace and web-search flag. History counts and
    the pointer to get_history_index / get_history_messages belong in the main
    system prompt and are intentionally omitted here.
    """
    ws_info = state.workspace_info or "(workspace not yet queried)"
    web_search = "enabled" if state.web_search_enabled else "disabled"
    content = (
        "ECONOMY MODE: ENABLED\n"
        f"Current workspace: {ws_info}\n"
        f"Web search: {web_search}\n"
    )
    return {
        "role": "system",
        "content": content,
        "hidden": True,
        "_index": -2,
        "_category": "system_notice",
    }


def _estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """Estimate the token size of a list of API-style messages.

    Counts string contents; list contents (multi-part) are counted per
    text part; non-string contents fall back to repr length.
    """
    try:
        from core.files import estimate_tokens
    except Exception:
        return 0
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            if content:
                total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if isinstance(text, str) and text:
                        total += estimate_tokens(text)
        elif content:
            total += estimate_tokens(str(content))
    return max(1, total)


def _enforce_history_token_budget(messages: List[Dict[str, Any]],
                                  assistant: Optional[Dict[str, Any]] = None,
                                  state: Optional["AgentLoopState"] = None,
                                  tail: Optional[int] = None) -> List[Dict[str, Any]]:
    """Trim the oldest messages so the context fits the token budget.

    Resolves the model's context window and configured output limit for
    *assistant* and asks :func:`core.context_guard.history_cap` for the
    unified input budget (the SAME budget the pre-flight guard shares with
    the economy mode). The head element (the compact metadata system
    message) is always preserved and trimming happens strictly from the
    front, so the most recent turns survive. Returns the list unchanged
    when the window cannot be resolved (unknown service/model).

    Hysteresis: the budget acts as the HIGH watermark - while the payload
    fits it, nothing moves. Once the payload exceeds it, whole leading
    messages are dropped in ONE step and the dropped count is folded into
    ``state.economy_anchor`` (cache-friendly mode, *state* and *tail*
    given) so the sent window front stays put until the payload grows back
    over the budget. In cache-friendly mode the cut resets the front to the
    configured *tail* messages - a large, predictable step - so the token
    limit triggers exactly the same front reset as the ``tail * multiplier``
    window overflow; the payload is cut further only when even the tail
    does not fit the budget. Without the fold the trim re-ran on every step
    and slid the front message by message, invalidating the provider prefix
    cache from the cut point on every request. In legacy mode (*tail* is
    None) the cut stops at the low watermark ``budget * LOW_RATIO`` and
    *state* stays None, because the front is recomputed per call anyway.
    """
    if not assistant or not isinstance(assistant, dict) or len(messages) <= 2:
        return messages
    svc_name = str(assistant.get("service", "") or "").strip()
    if not svc_name:
        return messages
    trim_low_ratio = float(_facade_attr("_ECONOMY_TRIM_LOW_RATIO", 0.75))
    guard_margin = int(_facade_attr("_ECONOMY_GUARD_MARGIN", 2048))
    try:
        from core.api_layer import _clamp_max_tokens, _get_model_max_tokens
        from core.context_guard import history_cap
        from core.files import get_model_context_window
        from core.services import get_services

        services = get_services()
        window = int(get_model_context_window(assistant, services))
        model_id = str(assistant.get("model", "") or "").strip()
        svc = services.get(svc_name, {})
        # The same effective output limit the guard will see in
        # send_request: the configured value falls back to the catalog
        # default and is clamped to the model's declared limit.
        max_out = int(_clamp_max_tokens(
            assistant.get("max_tokens") or _get_model_max_tokens(svc, model_id),
            svc, model_id) or 0)
        # The economy budget IS the guard's unified input budget
        # (history_cap = _INPUT_BUDGET_RATIO * (window - max_out) - prompt),
        # minus a small margin for the per-request user message. One shared
        # formula means the stateless guard trim never slides the
        # sent-window front while this budget holds.
        prompt_tokens = _estimate_messages_tokens(
            [{"role": "system", "content": str(assistant.get("text", "") or "")}])
        budget = max(0, history_cap(window, max_out, prompt_tokens)
                     - guard_margin)
    except Exception:
        return messages
    keep_head = messages[:1]
    body = list(messages[1:])
    used = _estimate_messages_tokens(keep_head) + _estimate_messages_tokens(body)
    if used <= budget:
        # Under the high watermark: nothing moves, the window front stays
        # put and the provider prefix stays reusable.
        return messages
    low = int(budget * trim_low_ratio)
    if tail is not None and tail > 0 and len(body) > tail:
        # Cache-friendly reset - the token overflow drops the front to the
        # configured tail in ONE predictable step, the same front position
        # a ``tail * multiplier`` window overflow produces. This makes the
        # token limit an alternative trigger of the SAME reset instead of a
        # separate, deeper cut.
        del body[:len(body) - tail]
    used = _estimate_messages_tokens(keep_head) + _estimate_messages_tokens(body)
    if used > budget:
        # Still over the high watermark: the tail alone does not fit the
        # budget (or legacy mode without *tail*). Keep dropping whole
        # messages down to the low watermark so the next step does not
        # re-trim immediately.
        while len(body) > 1:
            used = _estimate_messages_tokens(keep_head) + _estimate_messages_tokens(body)
            if used <= low:
                break
            body.pop(0)
    if state is not None:
        # Fold the cut into the sent-window anchor (cache-friendly mode
        # only): the window front IS the anchor, so advancing it by the
        # dropped count keeps the next build from re-including the
        # messages dropped here.
        dropped = len(messages) - 1 - len(body)
        anchor = getattr(state, "economy_anchor", None)
        if dropped and anchor is not None:
            try:
                state.economy_anchor = int(anchor) + dropped
            except Exception:
                pass
    return keep_head + body


def build_economy_context(state: "AgentLoopState",
                          assistant: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Build a reduced history for economy mode.

    Two strategies are supported:

    * Legacy mode (``economy_cache_enabled=False`` or
      ``economy_cache_multiplier <= 1``): only the last *tail* messages are
      kept. The prefix changes on every request, which prevents providers
      from applying prefix caching.

    * Cache-friendly mode (``economy_cache_enabled=True`` and
      ``economy_cache_multiplier > 1``): the beginning of the sent window is
      kept stable so the provider can cache the repeated prefix. While
      ``len(history) <= tail * multiplier`` the full thread is sent from the
      start. Once the window is exceeded, the anchor is fixed at
      ``len(history) - tail`` and stays stable until the sent part grows
      beyond ``tail * multiplier``, at which point it shifts again to
      ``len(history) - tail``. If the workspace or web-search meta parameters
      change, the anchor is reset to ``max(0, total - tail)`` and a new
      accumulation cycle begins.

    Both modes prepend a compact metadata system message. In cache-friendly
    mode that message contains only static fields so it does not break the
    prefix cache.

    Token budget (active when *assistant* is given): before returning, the
    oldest sent messages are additionally trimmed by
    ``_enforce_history_token_budget`` so the estimated input stays within
    the unified input budget ``core.context_guard.history_cap(window,
    max_out, prompt)`` - the SAME budget the pre-flight guard shares. The
    metadata message is always preserved. In cache-friendly mode the trim
    is a threshold operation with hysteresis: it runs only when the payload
    exceeds the budget, then resets the front to the configured *tail* in
    one step (the same reset a window overflow produces) and folds the cut
    into ``economy_anchor``, so the sent-window front (the cached prefix)
    stays put between rare, large cuts. Without *assistant* the budget step
    is skipped.
    """
    history = state.history
    if state.economy_tail_messages is not None and int(state.economy_tail_messages) > 0:
        tail = int(state.economy_tail_messages)
    else:
        tail = int(_facade_attr("_get_economy_tail_messages",
                                _get_economy_tail_messages)())
    total = len(history)

    result: List[Dict[str, Any]] = [_make_meta_msg(state)]

    if not state.economy_cache_enabled or state.economy_cache_multiplier <= 1:
        # Legacy mode: keep only the tail.
        if total <= tail:
            result.extend(history)
        else:
            result.extend(history[-tail:])
        return _enforce_history_token_budget(result, assistant)

    # Cache-friendly mode.
    multiplier = max(1, state.economy_cache_multiplier)
    window = tail * multiplier

    meta_key = _economy_meta_key(state)
    if state.economy_anchor is not None and state.economy_meta_key != meta_key:
        # Meta context changed mid-window. Exception: the first detection of
        # the workspace (previous meta key had no workspace info yet) is the
        # normal initial query, not a workspace switch - keep accumulating.
        if "(workspace not yet queried)" not in (state.economy_meta_key or ""):
            state.economy_anchor = max(0, total - tail)
    state.economy_meta_key = meta_key

    if state.economy_anchor is None:
        # First call: send the full thread while it fits, otherwise start
        # from the tail.
        if total <= window:
            state.economy_anchor = 0
        else:
            state.economy_anchor = max(0, total - tail)
    else:
        sent_len = total - state.economy_anchor
        if sent_len > window:
            # The stable prefix has grown too long; shift the anchor.
            state.economy_anchor = max(0, total - tail)

    result.extend(history[state.economy_anchor:])
    return _enforce_history_token_budget(result, assistant, state=state, tail=tail)


def carry_over_economy_cache(source: Optional["AgentLoopState"],
                             target: "AgentLoopState") -> None:
    """Copy cache-window anchor info from *source* to *target*.

    Called when a fresh AgentLoopState is created for a follow-up user
    message in the same thread. Without this, every new turn would start
    the cache-friendly window from the bare tail (tail messages) and the
    prefix would never grow to tail * multiplier, defeating prefix caching.
    """
    if source is None:
        return
    target.workspace_info = getattr(source, "workspace_info", "")
    target.economy_tail_messages = getattr(source, "economy_tail_messages", None)
    target.economy_anchor = getattr(source, "economy_anchor", None)
    target.economy_meta_key = getattr(source, "economy_meta_key", "")


def economy_cache_to_dict(state: "AgentLoopState") -> Dict[str, Any]:
    """Serialize cache-friendly economy window parameters.

    The UI persists these in session_state so the anchor survives
    loop-state clearing between user turns (terminal statuses like
    done/applied/error clear the loop state, but the anchor must survive
    so the cache-friendly window keeps growing to tail * multiplier).
    """
    return {
        "workspace_info": getattr(state, "workspace_info", ""),
        "economy_tail_messages": getattr(state, "economy_tail_messages", None),
        "economy_anchor": getattr(state, "economy_anchor", None),
        "economy_meta_key": getattr(state, "economy_meta_key", ""),
        "web_search_enabled": getattr(state, "web_search_enabled", False),
    }


def apply_economy_cache(data: Optional[Dict[str, Any]],
                        target: "AgentLoopState") -> None:
    """Restore cache-friendly economy parameters onto *target* from *data*.

    Called when a fresh AgentLoopState is created for a follow-up user
    message and the previous loop_state was cleared. Restoring the anchor
    lets the cache-friendly window continue growing to tail * multiplier
    instead of restarting from the bare tail on every turn.

    ``web_search_enabled`` is intentionally NOT restored here: the target
    state is created with the live session value, and the economy meta key
    will detect a web-search toggle and reset the anchor appropriately.
    """
    if not data:
        return
    target.workspace_info = data.get("workspace_info") or ""
    target.economy_tail_messages = data.get("economy_tail_messages") or None
    target.economy_anchor = data.get("economy_anchor")
    target.economy_meta_key = data.get("economy_meta_key") or ""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
