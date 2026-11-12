# ruff: noqa: F401
# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_text - pure text and event helpers of the chat page.

These helpers carry no Streamlit calls and no translated strings, so they can
live outside the page module and be tested on their own. The page module
re-exports every name from here: the chat feed, the tests and the tool-result
renderer all resolve them through ``ui.pages.orchestrator``.

Every function is a pure transformation of its arguments: text in, text out,
or a regrouping of an event list. Nothing reads session state.
"""
import json
import os
import re
from typing import Any, Dict

from core.threads_devagent import save_thread_workspace
from dev_agent import config as dagent_config
from dev_agent import workspace_binding as wb
from dev_agent.agent_loop import _extract_balanced_json_objects, parse_tool_calls


def _strip_html_details_tags(text: str) -> str:
    """Remove HTML <details>/<summary> wrapper tags, keeping inner text.

    Some models wrap reasoning/progress notes in <details><summary>...</summary>
    ...</details>; without stripping, the raw tags leak into the chat display.
    """
    if not text:
        return ""
    text = re.sub(r"<\s*/\s*details\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*details\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*summary\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*summary\b[^>]*>", "", text, flags=re.IGNORECASE)
    return text


def _strip_empty_fenced_blocks(text: str) -> str:
    """Remove multi-line fenced code blocks whose body is empty or blank.

    Scans lines and pairs opening/closing backtick fences explicitly, so a
    closing fence can never be glued together with the next block's opening
    fence. Blocks with a non-empty body are kept verbatim; empty blocks are
    dropped, leaving one blank placeholder line so surrounding prose does
    not merge.
    """
    out: list = []
    inside = False
    opener = ""
    body: list = []
    for line in text.split("\n"):
        if not inside:
            stripped = line.strip()
            if stripped.startswith("```"):
                inside = True
                opener = line
                body = []
            else:
                out.append(line)
            continue
        stripped = line.strip()
        if len(stripped) >= 3 and stripped.strip("`") == "":
            # Closing fence: a line consisting only of backticks.
            body_blank = not "".join(body).strip()
            opener_lang = opener.strip()[3:].strip().lower() if len(opener.strip()) > 3 else ""
            # Tool-call residue: a block whose body repeats the declared
            # language tag (e.g. the legacy glued ``json`` lines).
            residue_only = bool(opener_lang) and "".join(body).strip() and all(
                (ln.strip().lower() == opener_lang) for ln in body if ln.strip()
            )
            if body_blank or residue_only:
                out.append("")  # blank placeholder keeps paragraphs apart
            else:
                out.append(opener)
                out.extend(body)
                out.append(line)
            inside = False
            opener = ""
            body = []
        else:
            body.append(line)
    if inside:
        # Unterminated block: keep it as-is rather than dropping content.
        out.append(opener)
        out.extend(body)
    return "\n".join(out)


# Reserved DSML token models emit before every tag of the fallback tool-call
# dialect (``<｜｜DSML｜｜ parameter ...>``, U+FF5C fullwidth vertical bars).
# The UI must strip such markup together with the plain XML wrappers.
_DSML_TAG_MARK = r"(?:[\s\u2502\uFF5C|:]*DSML[\s\u2502\uFF5C|:]*)?"


def _strip_tool_calls(text: str) -> str:
    """Return assistant text with machine artifacts removed for display.

    The model may wrap tool calls in ```json fences, bare JSON objects, or
    (with some DeepSeek V4 outputs) XMlish wrappers such as
    <json>...</json> and <question>...</question>. All of them are removed
    here so the chat shows only real prose.

    Empty fenced blocks are removed with a line-based pairing scan instead
    of a blanket regex: the old empty-block pattern also matched newlines
    across block boundaries and glued the closing fence of one block to
    the opening fence of the next, leaving stray ``json`` lines in the
    chat.
    """
    if not text:
        return ""
    text = _strip_html_details_tags(text)

    # Remove balanced JSON objects (bare or inside fences): tool payloads.
    for obj in _extract_balanced_json_objects(text):
        text = text.replace(obj, "")

    # Remove known XML/DSML wrappers with their content, then any leftover
    # tags (with or without attributes).
    for name in ("tool_calls", "function_calls", "invoke", "parameter",
                 "json", "question"):
        text = re.sub(
            rf"<\s*{_DSML_TAG_MARK}{name}(?:\s[^>]*?)?\s*>.*?<\s*/\s*{_DSML_TAG_MARK}{name}\s*>",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            rf"<\s*/?\s*{_DSML_TAG_MARK}{name}\b[^>]*?>",
            "",
            text,
            flags=re.IGNORECASE,
        )

    # Bare DSML marker tags left over (e.g. a stray ``<|DSML| calls>`` without
    # a closing partner): drop any tag that mentions the reserved token.
    text = re.sub(
        rf"<\s*/?\s*{_DSML_TAG_MARK}[^>]*?>",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # One-line empty blocks left by removed JSON (```json```, ```json ```).
    text = re.sub(
        r"^[ \t]*`{3,}[^\S\n]*[a-zA-Z0-9_+.-]*[^\S\n]*`{3,}[ \t]*\r?$",
        "",
        text,
        flags=re.MULTILINE,
    )

    # Multi-line fenced blocks with empty/whitespace-only bodies.
    text = _strip_empty_fenced_blocks(text)

    # Collapse blank runs caused by removed blocks.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_tool_calls_for_message(content: str) -> tuple:
    """Return ``(display_text, has_tool_calls)`` for one chat message.

    ``has_tool_calls`` drives the download/copy controls: a message that
    carries machine tool-call payloads never gets them, even when some
    prose remains visible around the removed calls. A cheap prefilter
    skips the full parser for texts that cannot hold a call; the parser
    itself is ``parse_tool_calls`` - the same one the agent loop executes
    calls with. A parser failure counts as 'has calls', so the controls
    never appear on an unverified message.
    """
    text = _strip_tool_calls(content)
    if not content:
        return text, False
    if "{" not in content and "<" not in content:
        return text, False
    try:
        return text, bool(parse_tool_calls(content))
    except Exception:
        return text, True


def _first_two_lines(text: str) -> tuple:
    """Return (preview, full) where preview holds the first two lines of text.

    If the text is two lines or shorter, preview equals full.
    """
    if not text:
        return "", ""
    lines = text.splitlines()
    if len(lines) <= 2:
        return text, text
    return "\n".join(lines[:2]), text


def _format_call_args_preview(args: dict) -> str:
    """Render tool-call args compactly, one line per key, long strings trimmed."""
    if not args:
        return "{}"
    lines = []
    for k, v in args.items():
        if isinstance(v, str):
            if len(v) > 300:
                v = v[:300] + "…"
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines)


_CONTENT_KEYS = ("content", "new_text", "text", "stdout", "stderr", "diff", "error", "code")


def _extract_result_body(result_data: dict) -> tuple:
    """Return (body, is_json_body, full_json) for a tool result dict.

    ``body`` is the meaningful payload (file content, output, diff, ...) when
    a known content-like key is present, otherwise the whole JSON dump.
    """
    full_json = json.dumps(result_data, ensure_ascii=False, indent=2)
    for k in _CONTENT_KEYS:
        v = result_data.get(k)
        if isinstance(v, str) and v:
            return v, False, full_json
    return full_json, True, full_json


def _split_events_by_run(events: list) -> list:
    """Group events by ``run_id``, first-seen order preserved.

    Returns ``[(run_id, depth, agent_slug, [events])]``. A fan-out emits the
    events of several runs into one step feed; grouping keeps each child's
    output together and labels it, so a line can be attributed to the branch
    that produced it instead of reading as one interleaved stream.
    """
    order: list = []
    groups: dict = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        rid = str(ev.get("run_id") or "")
        if rid not in groups:
            groups[rid] = []
            order.append(rid)
        groups[rid].append(ev)
    result = []
    for rid in order:
        chunk = groups[rid]
        depth = min(int(ev.get("depth") or 0) for ev in chunk)
        slug = ""
        for ev in chunk:
            slug = str(ev.get("agent_slug") or "")
            if slug:
                break
        result.append((rid, depth, slug, chunk))
    return result


def _attach_events(history: list, events: list) -> None:
    """Attach *events* to the history message that should render them.

    Normal case: the events of one agent step belong to the last assistant
    message that followed the last visible user message. When the step
    produced no assistant reply (for example the very first LLM call failed
    with a provider error), the events - including the error that explains
    the failure - are attached to the last visible user message instead.
    That keeps first-step failures visible in the chat feed and lets the
    persistence layer carry them into the saved thread.
    """
    if not history or not events:
        return
    last_user_idx = None
    for i, msg in enumerate(history):
        if (isinstance(msg, dict) and msg.get("role") == "user"
                and not msg.get("hidden")):
            last_user_idx = i
    if last_user_idx is None:
        return
    target_idx = last_user_idx
    for i in range(len(history) - 1, last_user_idx, -1):
        msg = history[i]
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            target_idx = i
            break
    history[target_idx]["_events"] = events


# ─── Session-state keys, limits and small pure predicates ─────────────────────
# These helpers read no session state and call no Streamlit API: they compute
# a key name, a numeric bound or a boolean from their arguments. The page
# module re-exports every one of them.

def _sk(slug: str, key: str) -> str:
    """Return a slug-prefixed session-state key."""
    return f"orch_{slug}_{key}"


def settings_tabs_key(slug: str) -> str:
    """Return the session_state key holding the active settings tab."""
    return f"orch_settings_tabs_{slug}"


def settings_section_key(slug: str) -> str:
    """Return the session_state key holding a requested settings section."""
    return f"orch_settings_section_{slug}"


def _chat_toolbar_widget_key(slug: str, pref: str) -> str:
    """Return the widget key for a chat-pref checkbox.

    The toolbar is rendered once at the top of the chat.  The widget key IS
    the canonical ``orch_<slug>_<pref>`` session slot, so the checkbox always
    reads and writes the single source of truth.
    """
    return _sk(slug, pref)


def _chat_pref_config_keys() -> Dict[str, tuple]:
    """Return {session_key: (config_key, default)} for persisted chat prefs.

    Economy mode is deliberately absent: it is a fixed property of the chat
    (see ``ECONOMY_MODE_ALWAYS_ON``), not a user preference. The historical
    ``chat_economy_mode`` config key stays readable but is no longer
    consulted, so an install with the old value saved simply ignores it.
    """
    # Three INDEPENDENT toolbar preferences. "safety_mode" governs execution
    # confirmations only; "injection_protection" governs the sanitisation of
    # external content; "web_search" governs the search tool. Tying them to
    # one checkbox meant that a user asking for fewer confirmations silently
    # lost injection protection as well. The sandbox checkbox is gone: the
    # access setting owns both the environment and the machine boundary, and
    # the toolbar shows it as a badge instead of duplicating it.
    return {"web_search": ("chat_web_search", False),
            "safety_mode": ("chat_safety_mode", True),
            "injection_protection": ("chat_injection_protection", True)}


def _attachments_manifest_path(ws_root: str, tid: str) -> str:
    """Return the path of the dialog's saved-file registry."""
    return os.path.join(ws_root, ".dev_agent", "attachments", str(tid), ".manifest.json")


def _load_attachments_manifest(ws_root: str, tid: str) -> list:
    """Load the dialog's saved-file registry, or [] if absent/corrupt."""
    try:
        with open(_attachments_manifest_path(ws_root, tid), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _model_ids(svc_def: dict) -> list:
    """Return the model ids of a service definition."""
    return [m["id"] if isinstance(m, dict) else m for m in svc_def.get("models", [])]


def _is_agent_active(loop_state) -> bool:
    """Return True while the agent loop of this dialog still runs."""
    return loop_state is not None and loop_state.phase not in ("done", "error")


def _last_assistant_index(history: list):
    """Return the index of the last visible assistant message, or None."""
    last_assistant_idx = None
    for _idx, _msg in enumerate(history):
        if _msg.get("role") == "assistant" and not _msg.get("hidden") and _msg.get("content"):
            last_assistant_idx = _idx
    return last_assistant_idx


def _split_chat_prompt(raw):
    """Normalize an ``st.chat_input`` result into ``(text, files)``.

    Streamlit >= 1.45 returns a ``ChatInputValue``-like object exposing
    ``.text`` and ``.files`` when ``accept_file`` is enabled. Plain strings
    and ``None`` are tolerated so older callers and test doubles keep
    working; non-string text and non-list file payloads degrade to empty.
    """
    if raw is None:
        return "", []
    if isinstance(raw, str):
        return raw, []
    text = getattr(raw, "text", "")
    files = getattr(raw, "files", [])
    if not isinstance(text, str):
        text = ""
    if not isinstance(files, (list, tuple)):
        files = []
    return text, list(files)


def _dialog_limit(limits: Any, key: str, default: int) -> int:
    """Read one dialog limit, falling back to its documented default.

    The resolver always answers with every key, yet the page must survive a
    partial mapping: a missing number would otherwise raise at render time and
    take the whole settings tab down instead of showing a usable field.
    """
    try:
        value = limits.get(key) if hasattr(limits, "get") else None
    except Exception:  # pragma: no cover - defensive
        value = None
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _get_max_tokens_limit(svc_def: dict, model_id: str) -> int:
    """Return the maximum allowed max_tokens value for a service/model.

    Priority:
      1. The model entry's "max_tokens" field (if set).
      2. The service-level "max_tokens_default" field (if set).
      3. A conservative fallback (65536) so legacy behavior is preserved.
    """
    if svc_def:
        for m in svc_def.get("models", []):
            if isinstance(m, dict) and m.get("id") == model_id:
                mt = m.get("max_tokens")
                if mt:
                    return int(mt)
        svc_default = svc_def.get("max_tokens_default")
        if svc_default:
            return int(svc_default)
    return 65536


# ─── Agent-step and tool-result helpers ─────────────────────────────────────
# These operate on plain objects handed to them: a run handle, a loop
# state, a history list. None of them reads session state or calls
# Streamlit, so they live outside the page module.
def _reindex_history(history: list) -> None:
    """Number ``_index``/``_category`` of every history entry by position.

    These fields are runtime-only and are not persisted by the UI, so entries
    coming back from session_state (or a loaded thread) lack them; renumbering
    keeps the history tools in sync with the visible order.
    """
    for i, hist_msg in enumerate(history):
        if isinstance(hist_msg, dict):
            if "_index" not in hist_msg:
                hist_msg["_index"] = i
            if "_category" not in hist_msg:
                from dev_agent.agent_loop import _classify_message
                hist_msg["_category"] = _classify_message(hist_msg)

def _release_step_resources(orchestration, unsubscribe, root_run_id: str, *,
                            release_tree: bool) -> None:
    """Drop the step subscription and, when terminal, release the run tree.

    The subscription is per step, so a listener can never accumulate; the tree
    is per dialog run and is released only on a terminal phase - releasing it
    earlier would drop the records a later step still needs for its children.
    A teardown failure must not turn a finished step into an exception.
    """
    if unsubscribe is not None:
        try:
            unsubscribe()
        except Exception:
            pass
    if orchestration is not None and release_tree:
        try:
            orchestration.release_tree(root_run_id)
        except Exception:
            pass

def _sync_safety_mode(dispatcher, state, safety_mode: bool,
                      injection_protection: bool = True) -> None:
    """Keep the running loop in sync with the two independent protections.

    ``safety_mode`` decides whether a dangerous execution needs a confirmation
    from the user; ``injection_protection`` decides whether external content is
    sanitised. They are separate switches: turning one off must not weaken the
    other. The one-step sanitization bypass created by
    ``approve_sanitized_content`` is never overridden.
    """
    if not getattr(state, "injection_protection_bypassed", False):
        state.enable_injection_protection = bool(injection_protection)
    dispatcher.core._safety_enabled = bool(safety_mode)

def _persist_thread_workspace_meta(tid: str) -> None:
    """Persist the thread's BOUND workspace state into its DB meta.

    The binding registry is the per-thread source of truth: at render time
    the live config may belong to a neighbouring dialog, so it is used only
    as a fallback for a thread without a binding yet. The empty state is
    persisted as cleared fields ('' -> NULL) - the neutral root must never
    be recorded as a chosen folder.
    """
    if not tid:
        return
    try:
        state = wb.get_thread_state(tid)
        if state is not None:
            workspace = state.get("workspace") or ""
            target_file = state.get("target_file") or ""
        else:
            selected = bool(getattr(dagent_config, "WORKSPACE_SELECTED", True))
            workspace = str(dagent_config.PROJECT_ROOT) if selected else ""
            target_file = (dagent_config.TARGET_FILE or "") if selected else ""
        save_thread_workspace(tid, workspace=workspace, target_file=target_file)
    except Exception:
        pass

def _tool_result_title(tool: str, ok_val, path: str) -> str:
    """Return the first header line of a tool result: icon, tool and path."""
    if path:
        status_icon = "✅" if ok_val else "❌"
        return f"{status_icon} **tool_result**: {tool} → `{path}`"
    return f"{'✅' if ok_val else '❌'} **tool_result**: {tool}"


def _select_live_runs(runs: list, root: str) -> list:
    """Return the registry records of the children that are still running.

    A stored ``running`` claim is taken at face value here: the registry itself
    demotes a record this process cannot verify, so the filter only has to drop
    the dialog's own root and every non-running status. An empty result means
    the dialog has nothing to poll and no auto-refresh is drawn at all.
    """
    alive = []
    for rec in runs:
        if not isinstance(rec, dict):
            continue
        run_id = str(rec.get("run_id") or "")
        if run_id == root:
            continue
        if str(rec.get("status") or "") == "running":
            alive.append(rec)
    return alive


def _select_waiting_runs(runs: list, root: str) -> list:
    """Return the child records that park on an unanswered QUESTION.

    Only a question is reported: a run that merely ran out of continuations
    waits for a RESUME, and telling the user to answer it would be wrong. An
    answered or expired question is not a wait any more - the registry sweeps
    expired records, but a record read in the same pass may still carry one.
    """
    waiting = []
    for rec in runs:
        if not isinstance(rec, dict):
            continue
        run_id = str(rec.get("run_id") or "")
        if run_id == root:
            continue
        if str(rec.get("status") or "") != "awaiting_input":
            continue
        if str(rec.get("await_reason") or "") != "question":
            continue
        question = rec.get("question")
        if not isinstance(question, dict):
            continue
        if str(question.get("answer") or "").strip():
            continue
        waiting.append(rec)
    return waiting


def build_state_keys(slug: str, prefs: dict, economy_mode: bool,
                     chat_page_size: int) -> dict:
    """Return the session-state key -> default map of one orchestrator chat.

    ``prefs`` carries the persisted checkbox defaults and ``economy_mode`` /
    ``chat_page_size`` arrive as arguments because the page module owns those
    constants; keeping them out of this module leaves it free of page-level
    configuration.
    """
    return {
        f"orch_{slug}_history": [],
        f"orch_{slug}_thread_id": None,
        f"orch_{slug}_loop_state": None,
        # Cache-friendly economy window parameters (anchor/meta/tail), persisted
        # separately from loop_state so the accumulation window survives
        # loop-state clearing between turns.
        f"orch_{slug}_economy_cache": None,
        # Rendered token/economy indicator line (md5 key + html), so the
        # expensive context estimate is not recomputed on every rerun.
        f"orch_{slug}_token_line_cache": None,
        f"orch_{slug}_auto_apply": True,
        f"orch_{slug}_web_search": prefs["web_search"],
        f"orch_{slug}_economy_mode": economy_mode,
        f"orch_{slug}_stop_requested": False,
        f"orch_{slug}_dispatcher": None,
        f"orch_{slug}_saved_msg_count": 0,
        # Chat feed pagination: at most this many trailing entries shown.
        f"orch_{slug}_chat_show_count": chat_page_size,
        # Set to True right after a user message is sent: the next render
        # forces the feed to the bottom (clears a manual-scroll pause).
        f"orch_{slug}_scroll_bottom": False,
        f"orch_{slug}_show_func_form": False,
        f"orch_{slug}_edit_func": None,
        f"orch_{slug}_show_oinstr_form": False,
        f"orch_{slug}_edit_oinstr": None,
        f"orch_{slug}_safety_mode": prefs["safety_mode"],
        f"orch_{slug}_injection_protection": prefs["injection_protection"],
        # Folder staged in the "folder picker" block ("" = Авто / no explicit
        # choice) plus a widget-key counter used to reset the selector after
        # a send without touching an already-instantiated widget's state.
        f"orch_{slug}_folder_choice": "",
        f"orch_{slug}_folder_picker_counter": 0,
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
