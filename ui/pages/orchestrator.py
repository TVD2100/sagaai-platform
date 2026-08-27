# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator - reusable orchestrator chat page.

Parameterised by orchestrator slug.  Both DevAgent and custom orchestrators
render through this module. History and Settings live on dedicated pages
(ui.pages.history and ui.pages.orchestrator_settings).

All user-facing strings go through t(key, lang=lang).
"""
import os
import streamlit as st
import json
from typing import Any, Dict, List, Optional, Callable

from core.i18n import t
from core.services import (
    get_services,
    service_supports_reasoning_effort,
    get_model_reasoning_effort_options,
    default_reasoning_effort,
)
from core.config import load_config
from core.files import (
    get_file_uploader_types, extract_file_content, check_context, estimate_tokens,
    check_upload_tokens, MAX_UPLOAD_TOKENS,
    build_attachment_metadata, build_attachments_context, build_saved_files_registry,
)
from core.fs import combine_nonempty
from core.api_layer import send_request
from core.render import _md_to_txt, clipboard_button, format_token_line, format_ts_label
from core.threads_devagent import (
    create_devagent_thread, load_thread_messages,
    append_thread_message, load_thread_meta,
    sum_thread_tokens, save_thread_workspace,
)
from dev_agent.universal_agent import UniversalDevAgent
from dev_agent.agent_loop import (
    step_agent_loop, AgentLoopState, _extract_balanced_json_objects,
    build_economy_context, carry_over_economy_cache,
    economy_cache_to_dict, apply_economy_cache,
    approve_pending_confirmation, deny_pending_confirmation,
    approve_sanitized_content, deny_sanitized_content,
)
from dev_agent import workspace_tools as wt

from core.orchestrators import (
    get_orchestrator, save_orchestrator,
    build_assistant_dicts, get_web_search_config, get_economy_config,
    get_economy_tail_messages,
    orch_list_functions, orch_get_function, orch_save_function, orch_delete_function,
    orch_list_instructions, orch_get_instruction, orch_save_instruction, orch_delete_instruction,
    get_enabled_skills, set_enabled_skills,
    get_enabled_connections, set_enabled_connections,
    get_orchestrator_rag_bases, set_orchestrator_rag_bases,
    DEVAGENT_SLUG,
)
from core.skills_library import list_skills as list_library_skills
from core.rag import list_bases_with_activity
from core.connectors import list_connections


# ─── Session-state helpers ────────────────────────────────────────────────────

def _chat_pref_config_keys() -> Dict[str, tuple]:
    """Return {session_key: (config_key, default)} for persisted chat prefs."""
    return {"web_search": ("chat_web_search", False),
            "economy_mode": ("chat_economy_mode", True),
            "safety_mode": ("chat_safety_mode", True)}


def _chat_prefs(slug: str) -> Dict[str, bool]:
    orch = get_orchestrator(slug)
    cfg = orch.get("config", {}) if orch else {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {key: bool(cfg.get(conf_key, default)) for key, (conf_key, default) in _chat_pref_config_keys().items()}


def _save_chat_pref(slug: str, key: str, value: bool) -> bool:
    mapping = _chat_pref_config_keys()
    if key not in mapping:
        return False
    conf_key, _ = mapping[key]
    orch = get_orchestrator(slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg[conf_key] = bool(value)
    return save_orchestrator(slug, config=cfg)


def _make_state_keys(slug: str) -> dict:
    """Return a dict of session-state key -> default for this orchestrator."""
    prefs = _chat_prefs(slug)
    return {
        f"orch_{slug}_history": [],
        f"orch_{slug}_thread_id": None,
        f"orch_{slug}_loop_state": None,
        # Cache-friendly economy window parameters (anchor/meta/tail), persisted
        # separately from loop_state so the accumulation window survives
        # loop-state clearing between turns.
        f"orch_{slug}_economy_cache": None,
        f"orch_{slug}_auto_apply": True,
        f"orch_{slug}_attached": [],
        f"orch_{slug}_upload_counter": 0,
        f"orch_{slug}_web_search": prefs["web_search"],
        f"orch_{slug}_economy_mode": prefs["economy_mode"],
        f"orch_{slug}_stop_requested": False,
        f"orch_{slug}_dispatcher": None,
        f"orch_{slug}_saved_msg_count": 0,
        f"orch_{slug}_scroll_to": None,
        f"orch_{slug}_show_func_form": False,
        f"orch_{slug}_edit_func": None,
        f"orch_{slug}_show_oinstr_form": False,
        f"orch_{slug}_edit_oinstr": None,
        f"orch_{slug}_safety_mode": prefs["safety_mode"],
    }


def _init_orch_state(slug: str) -> None:
    """Initialise session_state keys for the given orchestrator slug."""
    for k, v in _make_state_keys(slug).items():
        if k not in st.session_state:
            st.session_state[k] = v


def _sk(slug: str, key: str) -> str:
    """Return a slug-prefixed session-state key."""
    return f"orch_{slug}_{key}"


def _ss(slug: str, key: str):
    """Get a slug-prefixed session-state value."""
    return st.session_state.get(_sk(slug, key))


def _set_ss(slug: str, key: str, value):
    st.session_state[_sk(slug, key)] = value


def _pop_ss(slug: str, key: str):
    return st.session_state.pop(_sk(slug, key), None)


def _save_economy_cache(slug: str, state) -> None:
    """Persist the cache-friendly economy window parameters.

    The loop state itself gets cleared for terminal statuses (done, applied,
    discarded, error, ...), but the anchor must survive so the next turn
    continues the accumulation window instead of restarting from the
    bare tail.

    A state whose anchor was never established (None) carries no
    accumulation to preserve; skipping the save leaves an existing
    persisted anchor intact instead of clobbering it.
    """
    if state is None:
        return
    if getattr(state, "economy_anchor", None) is None:
        return
    _set_ss(slug, "economy_cache", economy_cache_to_dict(state))


def _load_economy_cache(slug: str, state) -> None:
    """Restore persisted economy window parameters onto a fresh loop state."""
    cache = _ss(slug, "economy_cache")
    if isinstance(cache, dict):
        apply_economy_cache(cache, state)


def _attachments_manifest_path(ws_root: str, tid: str) -> str:
    return os.path.join(ws_root, ".dev_agent", "attachments", str(tid), ".manifest.json")


def _load_attachments_manifest(ws_root: str, tid: str) -> list:
    """Load the dialog's saved-file registry, or [] if absent/corrupt."""
    try:
        with open(_attachments_manifest_path(ws_root, tid), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _append_attachment_manifest(ws_root: str, tid: str, entry: dict) -> None:
    """Register one saved file in the dialog manifest (name is unique)."""
    manifest = _load_attachments_manifest(ws_root, tid)
    manifest = [e for e in manifest if e.get("name") != entry.get("name")]
    manifest.append(entry)
    dest = _attachments_manifest_path(ws_root, tid)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)


def _save_attachment_to_workspace(root: str, tid: str, att: dict) -> str:
    """Persist an uploaded file inside the current workspace.

    Returns a RELATIVE path inside .dev_agent/attachments/<tid>/ so the
    agent can open it with the normal read_file tool from any message of
    the dialog. The file is registered in a sidecar manifest.
    """
    attach_dir = os.path.join(root, ".dev_agent", "attachments", str(tid))
    os.makedirs(attach_dir, exist_ok=True)
    safe_name = os.path.basename(att.get("name", "attachment.txt")) or "attachment.txt"
    dest = os.path.join(attach_dir, safe_name)
    content = att.get("content", "")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(content)
    rel = os.path.join(".dev_agent", "attachments", str(tid), safe_name)
    _append_attachment_manifest(root, tid, {
        "name": safe_name,
        "path": rel,
        "chars": len(content),
        "tokens": att.get("tokens", 0),
    })
    return rel


# ─── Scroll helpers ───────────────────────────────────────────────────────────

def _scroll_page(target: str) -> None:
    """Emit a small JS snippet that scrolls the main Streamlit container to top/bottom.

    Tries several selectors (new and legacy Streamlit layouts) as well as the
    plain window/body scroll because the correct scroll container varies between
    Streamlit versions.
    """
    import streamlit.components.v1 as components
    if target == "bottom":
        scroller = "el.scrollTop = el.scrollHeight;"
        win_scroll = "window.parent.scrollTo(0, doc.body.scrollHeight || doc.documentElement.scrollHeight);"
    else:
        scroller = "el.scrollTop = 0;"
        win_scroll = "window.parent.scrollTo(0, 0);"
    components.html(
        f"""
        <script>
        (function() {{
            function doScroll() {{
                try {{
                    var doc = window.parent.document;
                    var selectors = [
                        '[data-testid="stMain"]',
                        '[data-testid="stMainBlockContainer"]',
                        '.main',
                        '.block-container',
                        'body',
                        'html'
                    ];
                    for (var i = 0; i < selectors.length; i++) {{
                        var nodes = doc.querySelectorAll(selectors[i]);
                        for (var j = 0; j < nodes.length; j++) {{
                            var el = nodes[j];
                            {scroller}
                        }}
                    }}
                    {win_scroll}
                }} catch (e) {{}}
            }}
            if (document.readyState === 'complete' || document.readyState === 'interactive') {{
                setTimeout(doScroll, 60);
            }} else {{
                window.addEventListener('DOMContentLoaded', function() {{ setTimeout(doScroll, 60); }});
            }}
        }})();
        </script>
        """,
        height=0,
        scrolling=False,
    )


# ─── Adapters & dispatcher ────────────────────────────────────────────────────

def _make_send_adapter(lang: str, slug: str) -> Callable:
    """Return a send_request wrapper using the orchestrator's strong model."""
    strong_assistant, _ = build_assistant_dicts(slug)
    if not strong_assistant.get("service") or not strong_assistant.get("model"):
        orch = get_orchestrator(slug)
        cfg = orch.get("config", {}) if orch else {}
        strong_assistant = {
            "text": orch.get("prompt_text", "") if orch else "",
            "service": cfg.get("strong_service", "DeepSeek"),
            "model": cfg.get("strong_model", "deepseek-v4-pro"),
            "temperature": float(cfg.get("strong_temperature", 0.2) or 0.2),
        }

    def _adapter(user_message: str, system: str = "", history: List[Dict[str, Any]] = None) -> str:
        assistant = dict(strong_assistant)
        if system:
            assistant["text"] = system
        safety_mode = _ss(slug, "safety_mode")
        if safety_mode is None:
            safety_mode = True
        return send_request(
            user_message=user_message,
            assistant=assistant,
            file_context="",
            history=history or [],
            lang=lang,
            enable_injection_protection=bool(safety_mode),
        )

    return _adapter


def _make_dispatcher(slug: str) -> UniversalDevAgent:
    """Return or create the UniversalDevAgent for this orchestrator."""
    dk = _sk(slug, "dispatcher")
    dispatcher = st.session_state.get(dk)
    if dispatcher is None:
        dispatcher = UniversalDevAgent(workspace=None, target_file=None)
        st.session_state[dk] = dispatcher
    dispatcher.core._web_search_enabled = _ss(slug, "web_search") or False
    dispatcher.core._web_search_config = get_web_search_config(slug)
    safety_mode = _ss(slug, "safety_mode")
    if safety_mode is None:
        safety_mode = True
    dispatcher.core._safety_enabled = bool(safety_mode)
    # Load custom orchestrator functions into the dispatcher so the LLM can
    # call them by name as if they were built-in tools.
    dispatcher.attach_orchestrator(slug)
    return dispatcher


def _assistant_has_api_key(svc_name: str) -> bool:
    if not svc_name:
        return False
    services = get_services()
    svc = services.get(svc_name)
    if not svc:
        return False
    cfg = load_config()
    key_field = svc.get("config_key", "")
    key_val = cfg.get(key_field, "")
    if isinstance(key_val, str):
        key_val = key_val.strip()
    return bool(key_val)


def _strip_html_details_tags(text: str) -> str:
    """Remove HTML <details>/<summary> wrapper tags, keeping inner text.

    Some models wrap reasoning/progress notes in <details><summary>...</summary>
    ...</details>; without stripping, the raw tags leak into the chat display.
    """
    import re
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
    import re
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
            rf"<\s*{name}(?:\s[^>]*?)?\s*>.*?<\s*/\s*{name}\s*>",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            rf"<\s*/?\s*{name}\b[^>]*?>",
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


# ─── Event rendering ──────────────────────────────────────────────────────────

_HIDDEN_EVENT_TYPES = {}


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


def _render_tool_result(ev: dict, lang: str, call_ev: Optional[dict] = None) -> None:
    """Render one tool_result event inside an expander.

    The expander header keeps the existing compact format (e.g.
    ``✅ tool_result: read_file → path 322 строки``). Inside, the call
    arguments and the first two lines of the result are shown; when the
    result is longer, the full content is available in a nested expander.
    """
    result_data = ev.get("result", {})
    tool = ev.get("tool", "?")
    ok_val = result_data.get("ok")
    path = result_data.get("path", "")

    header_lines = []
    if path:
        status_icon = "✅" if ok_val else "❌"
        header_lines.append(f"{status_icon} **tool_result**: {tool} → `{path}`")
    else:
        header_lines.append(f"{'\u2705' if ok_val else '\u274c'} **tool_result**: {tool}")
    if ok_val is False:
        error = result_data.get("error", "")
        if error:
            header_lines.append(f"⚠️ {error[:100]}")
        else:
            header_lines.append(t("event_tool_result_failed", lang=lang))
    else:
        total_lines = result_data.get("total_lines")
        bytes_val = result_data.get("bytes", 0)
        note = result_data.get("note", "")
        applied = result_data.get("applied")
        backup_ver = result_data.get("backup_version")
        parts = []
        if total_lines:
            parts.append(t("event_tool_result_lines", lang=lang, lines=total_lines))
        elif bytes_val:
            parts.append(t("event_tool_result_bytes", lang=lang, bytes=bytes_val))
        if applied:
            parts.append(t("event_tool_result_applied", lang=lang))
        if backup_ver:
            parts.append(f"v{backup_ver}")
        if note:
            parts.append(t("event_tool_result_note", lang=lang, note=note))
        header_lines.append(" | ".join(parts) if parts else t("event_tool_result_ok", lang=lang))
    header = "\n".join(header_lines)

    with st.expander(header, expanded=False):
        call_args = None
        if call_ev is not None:
            call_args = call_ev.get("args", {})
        elif ev.get("args"):
            call_args = ev.get("args", {})
        if call_args is not None:
            st.markdown(f"**{t('event_tool_call_title', lang=lang)}**")
            call_preview = _format_call_args_preview(call_args)
            st.code(call_preview, language="json")
            full_args_json = json.dumps(call_args, ensure_ascii=False, indent=2)
            if any(isinstance(v, str) and len(v) > 300 for v in call_args.values()):
                with st.expander(t("event_tool_result_show_more", lang=lang), expanded=False):
                    st.code(full_args_json, language="json")

        st.markdown(f"**{t('event_tool_result_title', lang=lang)}**")
        body, is_json_body, full_json = _extract_result_body(result_data)
        if is_json_body:
            st.code(full_json, language="json")
        else:
            preview, full = _first_two_lines(body)
            if preview:
                st.code(preview, language="")
            else:
                st.caption(t("event_tool_result_empty", lang=lang))
            if full != preview:
                with st.expander(t("event_tool_result_show_more", lang=lang), expanded=False):
                    st.code(full, language="")


def _render_events(events: list, lang: str) -> None:
    """Render tool events, pairing each tool_call with its tool_result.

    A tool_call that has no following tool_result (e.g. interrupted loop)
    is rendered standalone by ``_render_event``.
    """
    pending_call = None
    for ev in events:
        etype = ev.get("type")
        if etype == "tool_call":
            pending_call = ev
        elif etype == "tool_result":
            _render_tool_result(ev, lang, call_ev=pending_call)
            pending_call = None
        else:
            _render_event(ev, lang)
    if pending_call is not None:
        _render_event(pending_call, lang)


def _render_event(ev: dict, lang: str) -> None:
    etype = ev.get("type")
    if etype in _HIDDEN_EVENT_TYPES:
        return
    if etype == "tool_call":
        tool = ev.get("tool", "?")
        args = ev.get("args", {})
        display_args = dict(args)
        for key in list(display_args.keys()):
            val = display_args[key]
            if isinstance(val, str) and len(val) > 200:
                display_args[key] = val[:200] + "…"
        st.info(f"🔧 **{tool}** `{display_args}`")
    elif etype == "tool_result":
        _render_tool_result(ev, lang)
    elif etype == "applied":
        path = ev.get("path", "?")
        st.success(t("devagent_applied", lang=lang, path=path))
    elif etype == "assistant_created":
        skill_name = ev.get("skill_name", "?")
        eval_text = ev.get("evaluation", "")
        st.success(t("event_skill_created", lang=lang, skill=skill_name))
        if eval_text:
            st.caption(eval_text[:200])
    elif etype == "phase":
        phase = ev.get("phase", "")
        if phase in ("task_classification", "creating_skill", "calling_llm", "parsing", "executing"):
            labels = {
                "init": t("event_phase_init", lang=lang),
                "calling_llm": t("event_phase_calling_llm", lang=lang),
                "parsing": t("event_phase_parsing", lang=lang),
                "executing": t("event_phase_executing", lang=lang),
            }
            st.caption(labels.get(phase, t("event_phase_unknown", lang=lang, phase=phase)))
    elif etype == "sanitized_detected":
        events = ev.get("events", [])
        for info in events:
            reason = info.get("reason", "")
            tool_name = info.get("tool", "")
            file_path = info.get("path", "")
            if file_path:
                st.warning(f"🛡️ **Prompt-injection protection**: `{file_path}`\n\n{reason}")
            elif tool_name:
                st.warning(f"🛡️ **Prompt-injection protection**: tool `{tool_name}`\n\n{reason}")
            else:
                st.warning(f"🛡️ **Prompt-injection protection**: {reason}")
    elif etype == "error":
        st.error(ev.get("error", t("orch_unknown_error", lang=lang)))


# ─── Agent step ───────────────────────────────────────────────────────────────

def _do_step(slug: str, lang: str) -> None:
    """Run one agent loop step and update session state.

    If a completed loop is waiting for user input (awaiting_user,
    awaiting_approval, awaiting_confirmation, sanitized_required) and a new
    user message has arrived, a fresh AgentLoopState is created so the new
    message is processed instead of being swallowed by the finished loop.
    """
    dispatcher = _make_dispatcher(slug)
    dispatcher.core.set_send_request(_make_send_adapter(lang, slug))

    events: list = []
    def on_event(ev: dict) -> None:
        events.append(ev)

    state = _ss(slug, "loop_state")
    user_message = _pop_ss(slug, "user_message")

    economy_mode_ss = _ss(slug, "economy_mode")
    economy_mode = economy_mode_ss if economy_mode_ss is not None else True
    web_search_ss = _ss(slug, "web_search")
    web_search_enabled = web_search_ss if web_search_ss is not None else False
    safety_mode = _ss(slug, "safety_mode")
    if safety_mode is None:
        safety_mode = True

    # Start a brand-new loop either when there is no loop yet, or when the
    # previous loop has finished and the user has submitted a new message.
    if state is None or (user_message and state.phase in ("done", "error")):
        if not user_message or not user_message.strip():
            return
        prev_state = state  # may be None or a completed loop
        strong_assistant, weak_assistant = build_assistant_dicts(slug)
        orch = get_orchestrator(slug)
        economy_config = get_economy_config(slug)
        state = AgentLoopState(
            task=user_message,
            max_steps=orch.get("max_steps", 100) if orch else 100,
            auto_apply=True,
            strong_assistant=strong_assistant,
            weak_assistant=weak_assistant,
            history=_ss(slug, "history") or [],
            file_context=_pop_ss(slug, "file_ctx") or "",
            file_name=_pop_ss(slug, "file_name") or "",
            thread_id=_ss(slug, "thread_id") or "",
            economy_mode=economy_mode,
            web_search_enabled=web_search_enabled,
            economy_cache_enabled=bool(economy_config.get("cache_enabled", False)),
            economy_cache_multiplier=int(economy_config.get("cache_multiplier", 1)),
            economy_tail_messages=int(economy_config.get("tail_messages", 0)) or None,
            enable_injection_protection=bool(safety_mode),
        )
        # The economy window anchor must survive loop-state clearing.
        # carry_over handles the case when the previous loop state was kept
        # (e.g. awaiting_user); when it was cleared (terminal status), restore
        # the previously persisted cache parameters.
        carry_over_economy_cache(prev_state, state)
        if prev_state is None:
            _load_economy_cache(slug, state)
        # Runtime-only _index/_category fields are not persisted by the UI,
        # so entries coming back from session_state (or a loaded thread)
        # lack them. Re-number by position to keep the history tools in sync.
        for i, hist_msg in enumerate(state.history):
            if isinstance(hist_msg, dict):
                if "_index" not in hist_msg:
                    hist_msg["_index"] = i
                if "_category" not in hist_msg:
                    from dev_agent.agent_loop import _classify_message
                    hist_msg["_category"] = _classify_message(hist_msg)
        _set_ss(slug, "loop_state", state)
        _set_ss(slug, "stop_requested", False)

    dispatcher.core.set_history(state.history)

    if _ss(slug, "stop_requested"):
        state.phase = "done"
        state.final_status = "stopped_by_user"
        state.final_text = t("devagent_stopped_by_user", lang=lang)
        _save_economy_cache(slug, state)
        _set_ss(slug, "loop_state", None)
        _set_ss(slug, "stop_requested", False)
        return

    # Keep the running loop in sync with the Safe mode checkbox even if the
    # user toggled it mid-dialog. Do NOT override the one-step sanitization
    # bypass created by approve_sanitized_content().
    if not getattr(state, "injection_protection_bypassed", False):
        state.enable_injection_protection = bool(safety_mode)
    dispatcher.core._safety_enabled = bool(safety_mode)

    try:
        state = step_agent_loop(state, dispatcher=dispatcher, lang=lang, on_event=on_event)
    except Exception as exc:
        state.phase = "error"
        state.error_message = str(exc)
        events.append({"type": "error", "error": str(exc)})

    _set_ss(slug, "history", state.history)

    tid = _ss(slug, "thread_id")
    if tid and state.history:
        hist = list(state.history)
        if hist and events:
            for i in range(len(hist) - 1, -1, -1):
                if hist[i].get("role") == "assistant":
                    hist[i]["_events"] = events
                    break
        saved_count = _ss(slug, "saved_msg_count") or 0
        new_msgs = hist[saved_count:]
        for msg in new_msgs:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            file_name = msg.get("file_name", "")
            file_chars = msg.get("file_chars", 0)
            msg_events = msg.get("_events")
            msg_tokens = msg.get("_tokens")
            append_thread_message(tid, role, content,
                                  file_name=file_name, file_chars=file_chars,
                                  events=msg_events, tokens=msg_tokens)
        _set_ss(slug, "saved_msg_count", len(hist))

        # Persist the LAST active workspace / target_file so reopening the
        # thread from history restores the correct project folder.
        try:
            ws_info = wt.current_workspace()
            save_thread_workspace(
                tid,
                workspace=ws_info.get("root", ""),
                target_file=ws_info.get("target_file", "") if ws_info.get("single_file_mode") else None,
            )
        except Exception:
            pass

    # Keep the loop state when the agent is waiting for the user to act
    # (plan approval, dangerous-operation confirmation, sanitation approval)
    # For terminal statuses it is cleared.
    if state.phase in ("done", "error"):
        # Persist the economy window BEFORE the loop state may be cleared:
        # terminal statuses clear loop_state, but the anchor must survive.
        _save_economy_cache(slug, state)
        if getattr(state, "final_status", "") in ("awaiting_user", "awaiting_approval",
                                                    "awaiting_confirmation", "sanitized_required"):
            _set_ss(slug, "loop_state", state)
        else:
            _set_ss(slug, "loop_state", None)
    else:
        _set_ss(slug, "loop_state", state)


# ─── Reset helpers ────────────────────────────────────────────────────────────

def _reset_dialog(slug: str) -> None:
    """Reset dialog state for an orchestrator."""
    _set_ss(slug, "history", [])
    _set_ss(slug, "loop_state", None)
    _set_ss(slug, "economy_cache", None)
    _set_ss(slug, "thread_id", None)
    _set_ss(slug, "stop_requested", False)
    _set_ss(slug, "attached", [])
    _set_ss(slug, "upload_counter", 0)
    _set_ss(slug, "saved_msg_count", 0)
    _set_ss(slug, "scroll_to", None)
    if _sk(slug, "dispatcher") in st.session_state:
        del st.session_state[_sk(slug, "dispatcher")]


def _load_thread(slug: str, tid: str) -> None:
    """Load a thread into the current session and restore its workspace."""
    _reset_dialog(slug)

    # Restore the LAST workspace recorded for this thread so DevAgent works
    # on the same project folder as before.
    meta = load_thread_meta(tid)
    workspace = meta.get("workspace") or ""
    target_file = meta.get("target_file") or ""

    try:
        if target_file and os.path.isfile(target_file):
            wt.set_target_file(target_file)
        elif workspace and os.path.isdir(workspace):
            wt.set_workspace(workspace)
    except Exception:
        pass  # If the saved workspace is missing, keep the current one.

    msgs = load_thread_messages(tid)
    _set_ss(slug, "history", msgs)
    _set_ss(slug, "thread_id", tid)
    _set_ss(slug, "saved_msg_count", len(msgs))


# ─── Chat toolbar (single, sticky top) ────────────────────────────────────────

def _chat_toolbar_widget_key(slug: str, pref: str) -> str:
    """Return the widget key for a chat-pref checkbox.

    The toolbar is rendered once at the top of the chat.  The widget key IS
    the canonical ``orch_<slug>_<pref>`` session slot, so the checkbox always
    reads and writes the single source of truth.
    """
    return _sk(slug, pref)


def _sync_chat_pref_checkbox(slug: str, pref: str, value: bool) -> None:
    """Write one chat preference into the canonical slot.

    The toolbar renders a single checkbox widget whose key IS the canonical
    session slot; this helper persists the changed preference into the
    orchestrator config via ``_save_chat_pref`` and, for safety_mode,
    updates the live dispatcher immediately.
    """
    _set_ss(slug, pref, value)
    _save_chat_pref(slug, pref, value)
    if pref == "safety_mode":
        try:
            dispatcher = _make_dispatcher(slug)
            dispatcher.core._safety_enabled = bool(value)
        except Exception:
            pass


def _chat_toolbar_pref_changed(slug: str, pref: str) -> None:
    """Persist a chat preference after the user toggles its checkbox.

    Streamlit calls this callback AFTER updating the canonical
    ``orch_<slug>_<pref>`` session-state slot, so the current value is
    read from that slot instead of comparing a pre-render snapshot.
    """
    _sync_chat_pref_checkbox(slug, pref, bool(_ss(slug, pref)))


def _render_chat_toolbar(slug: str, lang: str) -> None:
    """Render the single chat toolbar (web search / economy / safety /
    settings / new dialog / copy URL).

    The toolbar is rendered ONCE at the top of the chat and stays on screen
    while the history scrolls (the host container is sticky - see
    ``_render_chat_tab``).  Checkbox widget keys are the same canonical
    session slots used by the agent loop, so there is a single source of
    truth for each preference.
    """
    cols = st.columns([1, 1, 1, 1, 1, 0.8])
    pref_specs = (
        ("web_search", t("orch_web_search_label", lang=lang),
         t("orch_web_search_help", lang=lang)),
        ("economy_mode", t("orch_economy_label", lang=lang),
         t("orch_economy_help", lang=lang)),
        ("safety_mode", t("orch_safety_label", lang=lang),
         t("orch_safety_help", lang=lang)),
    )
    for col, (pref, label, help_text) in zip(cols[:3], pref_specs):
        with col:
            widget_key = _chat_toolbar_widget_key(slug, pref)
            st.checkbox(
                label,
                key=widget_key,
                help=help_text,
                on_change=_chat_toolbar_pref_changed,
                args=(slug, pref),
            )

    with cols[3]:
        if st.button("⚙️ " + t("nav_settings", lang=lang),
                     key=f"orch_settings_{slug}",
                     use_container_width=True, type="secondary"):
            st.session_state["current_page"] = f"orchestrator_settings:{slug}"
            st.rerun()
    with cols[4]:
        if st.button(t("orch_new_dialog", lang=lang),
                     key=f"orch_reset_{slug}",
                     use_container_width=True, type="secondary"):
            _reset_dialog(slug)
            st.rerun()
    with cols[5]:
        url_params = {"orchestrator": slug}
        tid = _ss(slug, "thread_id")
        if tid:
            url_params["thread"] = str(tid)
        clipboard_button(
            text="", key=f"orch_cp_url_{slug}",
            label=t("orch_copy_url", lang=lang),
            copy_url_params=url_params,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB: CHAT
# ═══════════════════════════════════════════════════════════════════════════════

def _render_chat_tab(slug: str, lang: str) -> None:
    orch = get_orchestrator(slug)
    orch_name = orch.get("name", slug) if orch else slug

    # Execute pending scroll request (if any).
    scroll_to = _ss(slug, "scroll_to")
    if scroll_to:
        _set_ss(slug, "scroll_to", None)
        _scroll_page(scroll_to)

    st.markdown(
        "<style>\n"
        "[data-testid=\"stFileUploader\"] [data-testid=\"stFileUploaderFile\"] { display: none !important; }\n"
        f"button[key^=\"orch_reset_\"] {{ background-color: #28a745 !important; color: white !important; border-color: #28a745 !important; }}\n"
        f"button[key^=\"orch_reset_\"]:hover {{ background-color: #218838 !important; border-color: #1e7e34 !important; }}\n"
        f"button[key^=\"orch_stop_\"] {{ background-color: #dc3545 !important; color: white !important; border-color: #dc3545 !important; }}\n"
        f"button[key^=\"orch_stop_\"]:hover {{ background-color: #c82333 !important; border-color: #bd2130 !important; }}\n"
        "</style>",
        unsafe_allow_html=True,
    )

    economy_ss_val = _ss(slug, "economy_mode")
    economy_enabled = economy_ss_val if economy_ss_val is not None else True

    # Neutral caption: this page hosts ANY employee (not only developer
    # agents), so show the employee's own description instead of the legacy
    # "developer assistant" wording.  Fall back to the display name.
    _desc = (orch.get("description") or "").strip() if orch else ""
    st.caption(_desc if _desc else orch_name)
    st.markdown("---")

    # Check API key
    orch_cfg = orch.get("config", {}) if orch else {}
    if not isinstance(orch_cfg, dict):
        orch_cfg = {}
    strong_svc = orch_cfg.get("strong_service", "")
    if not strong_svc or not _assistant_has_api_key(strong_svc):
        st.warning(t("orch_no_api_key", lang=lang))
        return

    _pending_task = _pop_ss(slug, "pending_task")
    if _pending_task and _ss(slug, "loop_state") is None:
        _set_ss(slug, "user_message", _pending_task)
        _do_step(slug=slug, lang=lang)
        st.rerun()

    history = _ss(slug, "history") or []

    if not history:
        with st.chat_message("assistant"):
            if slug == DEVAGENT_SLUG:
                _welcome = t("orch_welcome_msg_devagent", lang=lang)
            else:
                _welcome = t("orch_welcome_msg", lang=lang, name=orch_name)
            st.markdown(_welcome)

    # Read the loop status before rendering messages so we know whether the
    # agent is still active and which assistant message is the final one.
    loop_state = _ss(slug, "loop_state")
    agent_is_active = (loop_state is not None and loop_state.phase not in ("done", "error"))
    last_assistant_idx = None
    for _idx, _msg in enumerate(history):
        if _msg.get("role") == "assistant" and not _msg.get("hidden") and _msg.get("content"):
            last_assistant_idx = _idx

    for idx, msg in enumerate(history):
        if msg.get("hidden"):
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        fname = msg.get("file_name", "")

        # Time display (HH:MM DD.MM.YYYY, same format as the assistant chat)
        ts_display = format_ts_label(msg.get("ts", ""))

        if role == "user":
            with st.chat_message("user"):
                if fname:
                    st.caption(f"📎 {fname}")
                st.markdown(content)
                if ts_display:
                    st.caption(f"🕐 {ts_display}")
        elif role == "assistant":
            if not content:
                continue
            display = _strip_tool_calls(content)
            stored_events = msg.get("_events", [])
            with st.chat_message("assistant"):
                if display:
                    st.markdown(display)
                elif not stored_events:
                    st.caption(t("devagent_agent_step_compact", lang=lang))
                if stored_events:
                    _render_events(stored_events, lang)
                # Show download/copy controls only on the final assistant
                # message and only after the agent loop has finished.
                if display and idx == last_assistant_idx and not agent_is_active:
                    dl_fname = f"orchestrator_message_{slug}_{idx}"
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.download_button(
                            t("chat_dl_md", lang=lang), data=display.encode("utf-8"),
                            file_name=f"{dl_fname}.md", mime="text/markdown",
                            key=f"orch_dl_md_{slug}_{idx}", use_container_width=True,
                        )
                    with col2:
                        st.download_button(
                            t("chat_dl_txt", lang=lang), data=_md_to_txt(display).encode("utf-8"),
                            file_name=f"{dl_fname}.txt", mime="text/plain",
                            key=f"orch_dl_txt_{slug}_{idx}", use_container_width=True,
                        )
                    with col3:
                        clipboard_button(display, key=f"orch_cp_md_{slug}_{idx}", label=t("chat_cp_md", lang=lang))
                    with col4:
                        clipboard_button(_md_to_txt(display), key=f"orch_cp_txt_{slug}_{idx}", label=t("chat_cp_txt", lang=lang))
                if ts_display:
                    st.caption(f"🕐 {ts_display}")

    # Agent status bar / confirmation dialog.
    if loop_state is None:
        pass
    elif loop_state.phase == "error":
        st.error(loop_state.error_message or t("orch_unknown_error", lang=lang))
    elif loop_state.phase == "done":
        status = loop_state.final_status
        if status == "stopped_by_user":
            st.warning(t("orch_stopped_by_user", lang=lang))
        elif status == "stopped_max_steps":
            st.warning(t("orch_stopped_max_steps", lang=lang))
        elif status == "awaiting_user":
            st.info(t("orch_awaiting_user", lang=lang))
        elif status == "awaiting_approval":
            st.info(t("orch_awaiting_approval", lang=lang))
        elif status == "awaiting_confirmation":
            # ── Dangerous-operation confirmation dialog ────────────────────
            tool = getattr(loop_state, "pending_confirmation_tool", None)
            reasons = getattr(loop_state, "pending_confirmation_reasons", [])
            code_snippet = getattr(loop_state, "pending_confirmation_code", "")
            if tool:
                st.warning(
                    t("orch_confirm_title", lang=lang) + "\n\n"
                    + t("orch_confirm_desc", lang=lang) + "\n\n"
                    + t("orch_confirm_tool", lang=lang, tool=tool)
                )
                if code_snippet:
                    st.code(code_snippet, language="python", line_numbers=True)
                if reasons:
                    st.markdown(t("orch_confirm_reasons_title", lang=lang))
                    for r in reasons:
                        st.markdown(f"- {r}")
                st.markdown("---")
                col_allow, col_deny = st.columns(2)
                with col_allow:
                    if st.button(
                        t("orch_allow_execute", lang=lang),
                        key=f"orch_allow_danger_{slug}",
                        type="primary",
                        use_container_width=True,
                    ):
                        dispatcher = _make_dispatcher(slug)
                        dispatcher.core.set_send_request(_make_send_adapter(lang, slug))
                        events: list = []
                        def on_event(ev):
                            events.append(ev)
                        new_state = approve_pending_confirmation(
                            loop_state, dispatcher, lang=lang, on_event=on_event
                        )
                        # Attach events to the last assistant message for history.
                        if new_state.history and events:
                            for i in range(len(new_state.history) - 1, -1, -1):
                                if new_state.history[i].get("role") == "assistant":
                                    new_state.history[i]["_events"] = events
                                    break
                        _set_ss(slug, "loop_state", new_state)
                        _set_ss(slug, "history", new_state.history)
                        # Continue the loop.
                        _do_step(slug=slug, lang=lang)
                        st.rerun()
                with col_deny:
                    if st.button(
                        t("orch_deny_execute", lang=lang),
                        key=f"orch_deny_danger_{slug}",
                        type="secondary",
                        use_container_width=True,
                    ):
                        dispatcher = _make_dispatcher(slug)
                        events: list = []
                        def on_event(ev):
                            events.append(ev)
                        new_state = deny_pending_confirmation(
                            loop_state, lang=lang, on_event=on_event
                        )
                        if new_state.history and events:
                            for i in range(len(new_state.history) - 1, -1, -1):
                                if new_state.history[i].get("role") == "assistant":
                                    new_state.history[i]["_events"] = events
                                    break
                        _set_ss(slug, "loop_state", new_state)
                        _set_ss(slug, "history", new_state.history)
                        _do_step(slug=slug, lang=lang)
                        st.rerun()
        elif status == "sanitized_required":
            # ── Sanitization confirmation dialog ───────────────────────────
            sanitized_events = getattr(loop_state, "sanitized_events", [])
            if sanitized_events:
                st.warning(
                    t("orch_sanitized_title", lang=lang) + "\n\n"
                    + t("orch_sanitized_desc", lang=lang)
                )
                for info in sanitized_events:
                    reason = info.get("reason", "")
                    tool_name = info.get("tool", "")
                    file_path = info.get("path", "")
                    detail_parts = [reason]
                    if file_path:
                        detail_parts.insert(0, t("orch_sanitized_file_line", lang=lang).format(file_path=file_path))
                    if tool_name:
                        detail_parts.insert(0, t("orch_sanitized_tool_line", lang=lang).format(tool_name=tool_name))
                    st.code("\n".join(detail_parts), language="")
                st.markdown("---")
                col_allow, col_deny = st.columns(2)
                with col_allow:
                    if st.button(
                        t("orch_allow_view", lang=lang),
                        key=f"orch_allow_sanitized_{slug}",
                        type="primary",
                        use_container_width=True,
                    ):
                        dispatcher = _make_dispatcher(slug)
                        dispatcher.core.set_send_request(_make_send_adapter(lang, slug))
                        events: list = []
                        def on_event(ev):
                            events.append(ev)
                        new_state = approve_sanitized_content(
                            loop_state, dispatcher, lang=lang, on_event=on_event
                        )
                        if new_state.history and events:
                            for i in range(len(new_state.history) - 1, -1, -1):
                                if new_state.history[i].get("role") == "assistant":
                                    new_state.history[i]["_events"] = events
                                    break
                        _set_ss(slug, "loop_state", new_state)
                        _set_ss(slug, "history", new_state.history)
                        _do_step(slug=slug, lang=lang)
                        st.rerun()
                with col_deny:
                    if st.button(
                        t("orch_deny_view", lang=lang),
                        key=f"orch_deny_sanitized_{slug}",
                        type="secondary",
                        use_container_width=True,
                    ):
                        dispatcher = _make_dispatcher(slug)
                        events: list = []
                        def on_event(ev):
                            events.append(ev)
                        new_state = deny_sanitized_content(
                            loop_state, lang=lang, on_event=on_event
                        )
                        if new_state.history and events:
                            for i in range(len(new_state.history) - 1, -1, -1):
                                if new_state.history[i].get("role") == "assistant":
                                    new_state.history[i]["_events"] = events
                                    break
                        _set_ss(slug, "loop_state", new_state)
                        _set_ss(slug, "history", new_state.history)
                        _do_step(slug=slug, lang=lang)
                        st.rerun()

    if not agent_is_active:
        att_files: list = _ss(slug, "attached") or []

        if att_files:
            with st.container():
                for i, f in enumerate(att_files):
                    col_name, col_btn = st.columns([5, 1])
                    with col_name:
                        st.caption(f"📎 {f['name']}")
                    with col_btn:
                        if st.button("✕", key=f"orch_att_rm_{slug}_{i}"):
                            att_files.pop(i)
                            _set_ss(slug, "upload_counter",
                                    int(_ss(slug, "upload_counter") or 0) + 1)
                            _set_ss(slug, "attached", att_files)
                            st.rerun()

        with st.bottom:
            user_input = st.chat_input(t("orch_input_placeholder", lang=lang))
            _render_chat_toolbar(slug, lang)

        uploaded_files = st.file_uploader(
            t("orch_attach_label", lang=lang),
            type=get_file_uploader_types(),
            accept_multiple_files=True,
            key=f"orch_upload_{slug}_{int(_ss(slug, 'upload_counter') or 0)}",
            label_visibility="collapsed",
        )
        st.caption(t("file_uploader_types", lang=lang,
                     types=", ".join(get_file_uploader_types())))

        if uploaded_files:
            existing_names = {f["name"] for f in att_files}
            added = False
            for uf in uploaded_files:
                if uf.name in existing_names:
                    continue
                try:
                    content = extract_file_content(uf)
                except Exception:
                    continue
                ok_tokens, tokens = check_upload_tokens(content)
                if not ok_tokens:
                    st.error(t("file_too_large_tokens", lang=lang,
                               tokens=tokens, max_tokens=MAX_UPLOAD_TOKENS))
                    continue
                att_files.append(build_attachment_metadata(uf.name, content))
                added = True
            _set_ss(slug, "attached", att_files)
            # Recreate the uploader widget after processing so the files just
            # uploaded are dropped from the widget state and do not re-trigger
            # this block on the next rerun (which used to block the chat).
            _set_ss(slug, "upload_counter",
                    int(_ss(slug, "upload_counter") or 0) + 1)
            if added:
                st.rerun()

        if user_input and user_input.strip():
            if not _ss(slug, "thread_id"):
                # Create thread associated with this orchestrator
                orch_obj = orch or {}
                ws_info = wt.current_workspace()
                tid = create_devagent_thread(
                    title=user_input.strip(),
                    orchestrator_slug=slug,
                    orchestrator_name=orch_obj.get("name", slug),
                    workspace=ws_info.get("root", ""),
                    target_file=ws_info.get("target_file", "") if ws_info.get("single_file_mode") else None,
                )
                _set_ss(slug, "thread_id", tid)
                _set_ss(slug, "saved_msg_count", 0)

            ctx_text, name = "", ""
            tid = _ss(slug, "thread_id")
            if tid:
                ws_root = wt.current_workspace().get("root", "")
                # Persist EVERY attachment on disk (small or large) so it
                # stays available for the whole dialog. Small files keep
                # their full inline content in this first message too.
                for f in att_files:
                    if not f.get("path"):
                        try:
                            f["path"] = _save_attachment_to_workspace(ws_root, tid, f)
                        except Exception:
                            f["path"] = ""
                if att_files:
                    ctx_text, name = build_attachments_context(att_files)
                # Re-announce ALL files of this dialog on every message so
                # the agent still knows them when economy mode has dropped
                # the beginning of the conversation.
                registry = build_saved_files_registry(_load_attachments_manifest(ws_root, tid))
                if registry:
                    ctx_text = f"{ctx_text}\n\n{registry}" if ctx_text else registry
            _set_ss(slug, "file_ctx", ctx_text)
            _set_ss(slug, "file_name", name)
            _set_ss(slug, "user_message", user_input.strip())
            _set_ss(slug, "attached", [])
            # Recreate the uploader widget after sending so the files just
            # sent are not re-attached from the widget state on rerun.
            _set_ss(slug, "upload_counter",
                    int(_ss(slug, "upload_counter") or 0) + 1)
            _do_step(slug=slug, lang=lang)
            st.rerun()

    # ── Token usage indicator (single line, after Upload) ─────────────────
    _orch = get_orchestrator(slug)
    _full_sys = _orch.get("prompt_text", "") if _orch else ""
    _effective_history = history
    _economy_meta = ""
    if economy_enabled and history:
        _economy_config = get_economy_config(slug)
        _temp_state = AgentLoopState(
            task="",
            history=list(history),
            economy_mode=True,
            thread_id=_ss(slug, "thread_id") or "",
            economy_cache_enabled=bool(_economy_config.get("cache_enabled", False)),
            economy_cache_multiplier=int(_economy_config.get("cache_multiplier", 1)),
        )
        # Reflect the real window that will be sent next: reuse the live
        # loop state anchor (if any) so the indicator shows the gradual
        # growth of the cache-friendly window instead of always the bare tail.
        if loop_state is not None:
            _temp_state.workspace_info = getattr(loop_state, "workspace_info", "")
            _temp_state.economy_anchor = getattr(loop_state, "economy_anchor", None)
            _temp_state.economy_meta_key = getattr(loop_state, "economy_meta_key", "")
            _temp_state.economy_tail_messages = getattr(loop_state, "economy_tail_messages", None)
            _temp_state.web_search_enabled = bool(getattr(loop_state, "web_search_enabled", False))
        else:
            _ec = _ss(slug, "economy_cache")
            if isinstance(_ec, dict):
                apply_economy_cache(_ec, _temp_state)
        from dev_agent.agent_loop import _classify_message, _index_message
        for i, _msg in enumerate(history):
            if "_index" not in _msg:
                _cat = _classify_message(_msg)
                _index_message(_msg, i, _cat)
        _effective_history = build_economy_context(_temp_state)
        _raw_total = len(history)
        # build_economy_context() prepends one hidden meta message; the remaining
        # entries are the exact history messages actually sent to the model.
        _sent_total = max(0, len(_effective_history) - 1)
        _economy_meta = f" 💡 economy ({_sent_total}/{_raw_total} msgs)"

    _effective_text = " ".join(
        m.get("content", "") for m in _effective_history if not m.get("hidden")
    )
    _file_text = " ".join(
        f.get("content", "") if not f.get("stored") else f.get("preview", "")
        for f in (_ss(slug, "attached") or [])
    )
    _strong_assistant, _ = build_assistant_dicts(slug)
    _ctx = check_context(
        _full_sys, "", combine_nonempty([_effective_text, _file_text]),
        _strong_assistant, get_services(),
    )
    _current_tokens = _ctx["total_tokens"]
    _tok_in, _tok_out, _tok_cache = sum_thread_tokens(history)
    st.markdown(
        f'<div style="font-size:0.75rem;color:#555;margin-top:6px">'
        f'{format_token_line(_current_tokens, _tok_in, _tok_out, _economy_meta, tokens_cache=_tok_cache)}</div>',
        unsafe_allow_html=True,
    )

    if agent_is_active:
        if st.button(t("orch_stop_btn", lang=lang), key=f"orch_stop_{slug}",
                     type="secondary", use_container_width=True):
            _set_ss(slug, "stop_requested", True)
            st.rerun()
        with st.spinner(t("orch_thinking", lang=lang)):
            _do_step(slug=slug, lang=lang)
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB: SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

def _services_with_web_search() -> dict:
    all_svc = get_services()
    result = {}
    for name, svc in all_svc.items():
        tools = svc.get("tools_options", [])
        for t in tools:
            if t.get("key") == "web_search":
                result[name] = svc
                break
    return result


def _temp_slider(svc_def: dict, lang: str, label: str, default_val: float, widget_key: str) -> float:
    t_min = float(svc_def.get("temp_min", 0.0))
    t_max = float(svc_def.get("temp_max", 1.0))
    t_step = float(svc_def.get("temp_step", 0.05))
    clamped = max(t_min, min(t_max, default_val))
    return st.slider(label, min_value=t_min, max_value=t_max, value=clamped, step=t_step,
                     key=widget_key, help=t("orch_temperature_label_help", lang=lang))


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


def _render_models_settings(slug: str, lang: str) -> None:
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return
    cfg = orch.get("config", {})
    services = get_services()
    service_names = list(services.keys())
    if not service_names:
        st.info(t("orch_no_services", lang=lang))
        return

    # Strong model
    st.markdown(t("orch_strong_model_section", lang=lang))
    cur_strong_svc = cfg.get("strong_service", "") or service_names[0]
    if cur_strong_svc not in service_names:
        cur_strong_svc = service_names[0]
    cur_strong_mdl = cfg.get("strong_model", "")
    cur_strong_temp = float(cfg.get("strong_temperature", 0.4) or 0.4)

    saved_strong_svc = cfg.get("strong_service", "")
    saved_strong_mdl = cfg.get("strong_model", "")
    if saved_strong_svc and saved_strong_svc not in service_names:
        st.warning(t("orch_service_unavailable", lang=lang, service=saved_strong_svc))
    elif saved_strong_svc in service_names:
        saved_strong_models = [
            m["id"] if isinstance(m, dict) else m
            for m in services.get(saved_strong_svc, {}).get("models", [])
        ]
        if saved_strong_mdl and saved_strong_mdl not in saved_strong_models:
            st.warning(t("orch_model_unavailable", lang=lang,
                         model=saved_strong_mdl, service=saved_strong_svc))

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        sel_strong_svc = st.selectbox(t("orch_service_label", lang=lang), options=service_names,
                                       index=service_names.index(cur_strong_svc),
                                       key=f"orch_set_strong_svc_{slug}",
                                       help=t("orch_service_label_help", lang=lang))
    with col_s2:
        svc_def = services.get(sel_strong_svc, {})
        models = svc_def.get("models", [])
        model_ids = [m["id"] if isinstance(m, dict) else m for m in models]
        if cur_strong_mdl not in model_ids and model_ids:
            cur_strong_mdl = model_ids[0]
        mdl_idx = model_ids.index(cur_strong_mdl) if cur_strong_mdl in model_ids else 0
        sel_strong_mdl = st.selectbox(t("orch_model_label", lang=lang), options=model_ids if model_ids else [cur_strong_mdl],
                                       index=mdl_idx, key=f"orch_set_strong_mdl_{slug}",
                                       help=t("orch_model_label_help", lang=lang))

    strong_svc_def = services.get(sel_strong_svc, {})
    sel_strong_temp = _temp_slider(strong_svc_def, lang, t("orch_temperature_label", lang=lang), cur_strong_temp,
                                     f"orch_set_strong_temp_{slug}")

    cur_strong_re = str(cfg.get("strong_reasoning_effort", "") or "").strip()
    sel_strong_re = ""
    if service_supports_reasoning_effort(strong_svc_def):
        re_opts = get_model_reasoning_effort_options(strong_svc_def, sel_strong_mdl)
        if not cur_strong_re or cur_strong_re not in re_opts:
            cur_strong_re = default_reasoning_effort(strong_svc_def, strong=True, model=sel_strong_mdl) or (re_opts[0] if re_opts else "")
        if re_opts:
            sel_strong_re = st.selectbox(
                t("orch_reasoning_effort_label", lang=lang),
                options=re_opts,
                index=re_opts.index(cur_strong_re) if cur_strong_re in re_opts else 0,
                key=f"orch_set_strong_re_{slug}",
                help=t("orch_reasoning_effort_label_help", lang=lang),
            )

    cur_strong_max_tokens = int(cfg.get("strong_max_tokens", 0) or 0)
    strong_limit = _get_max_tokens_limit(strong_svc_def, sel_strong_mdl)
    if cur_strong_max_tokens > strong_limit:
        cur_strong_max_tokens = strong_limit
    sel_strong_max_tokens = st.number_input(t("orch_max_tokens", lang=lang), min_value=0, max_value=strong_limit,
                                             value=cur_strong_max_tokens, step=256,
                                             help=t("orch_max_tokens_help", lang=lang),
                                             key=f"orch_set_strong_max_tokens_{slug}")
    st.caption(t("orch_max_tokens_hint", lang=lang, max=f"{strong_limit:,}"))

    # Weak model
    st.markdown(t("orch_weak_model_section", lang=lang))
    cur_weak_svc = cfg.get("weak_service", "") or cur_strong_svc
    if cur_weak_svc not in service_names:
        cur_weak_svc = service_names[0]
    cur_weak_mdl = cfg.get("weak_model", "")
    cur_weak_temp = float(cfg.get("weak_temperature", 0.4) or 0.4)

    saved_weak_svc = cfg.get("weak_service", "")
    saved_weak_mdl = cfg.get("weak_model", "")
    if saved_weak_svc and saved_weak_svc not in service_names:
        st.warning(t("orch_service_unavailable", lang=lang, service=saved_weak_svc))
    elif saved_weak_svc in service_names:
        saved_weak_models = [
            m["id"] if isinstance(m, dict) else m
            for m in services.get(saved_weak_svc, {}).get("models", [])
        ]
        if saved_weak_mdl and saved_weak_mdl not in saved_weak_models:
            st.warning(t("orch_model_unavailable", lang=lang,
                         model=saved_weak_mdl, service=saved_weak_svc))

    col_w1, col_w2 = st.columns(2)
    with col_w1:
        sel_weak_svc = st.selectbox(t("orch_service_label", lang=lang), options=service_names,
                                     index=service_names.index(cur_weak_svc),
                                     key=f"orch_set_weak_svc_{slug}",
                                     help=t("orch_service_label_help", lang=lang))
    with col_w2:
        svc_def = services.get(sel_weak_svc, {})
        models = svc_def.get("models", [])
        model_ids_w = [m["id"] if isinstance(m, dict) else m for m in models]
        if cur_weak_mdl not in model_ids_w and model_ids_w:
            cur_weak_mdl = model_ids_w[0]
        mdl_idx_w = model_ids_w.index(cur_weak_mdl) if cur_weak_mdl in model_ids_w else 0
        sel_weak_mdl = st.selectbox(t("orch_model_label", lang=lang), options=model_ids_w if model_ids_w else [cur_weak_mdl],
                                     index=mdl_idx_w, key=f"orch_set_weak_mdl_{slug}",
                                     help=t("orch_model_label_help", lang=lang))

    weak_svc_def = services.get(sel_weak_svc, {})
    sel_weak_temp = _temp_slider(weak_svc_def, lang, t("orch_temperature_label", lang=lang), cur_weak_temp,
                                   f"orch_set_weak_temp_{slug}")

    cur_weak_re = str(cfg.get("weak_reasoning_effort", "") or "").strip()
    sel_weak_re = ""
    if service_supports_reasoning_effort(weak_svc_def):
        re_opts = get_model_reasoning_effort_options(weak_svc_def, sel_weak_mdl)
        if not cur_weak_re or cur_weak_re not in re_opts:
            cur_weak_re = default_reasoning_effort(weak_svc_def, strong=False, model=sel_weak_mdl) or (re_opts[0] if re_opts else "")
        if re_opts:
            sel_weak_re = st.selectbox(
                t("orch_reasoning_effort_label", lang=lang),
                options=re_opts,
                index=re_opts.index(cur_weak_re) if cur_weak_re in re_opts else 0,
                key=f"orch_set_weak_re_{slug}",
                help=t("orch_reasoning_effort_label_help", lang=lang),
            )

    cur_weak_max_tokens = int(cfg.get("weak_max_tokens", 0) or 0)
    weak_limit = _get_max_tokens_limit(weak_svc_def, sel_weak_mdl)
    if cur_weak_max_tokens > weak_limit:
        cur_weak_max_tokens = weak_limit
    sel_weak_max_tokens = st.number_input(t("orch_max_tokens", lang=lang), min_value=0, max_value=weak_limit,
                                           value=cur_weak_max_tokens, step=256,
                                           help=t("orch_max_tokens_help", lang=lang),
                                           key=f"orch_set_weak_max_tokens_{slug}")
    st.caption(t("orch_max_tokens_hint", lang=lang, max=f"{weak_limit:,}"))

    # Web-search model
    st.markdown(t("orch_search_model_section", lang=lang))
    search_services = _services_with_web_search()
    search_svc_names = list(search_services.keys())
    cur_search_svc = cfg.get("search_service", "")
    cur_search_mdl = cfg.get("search_model", "")
    cur_search_temp = float(cfg.get("search_temperature", 0.3) or 0.3)
    cur_search_mtc = int(cfg.get("search_max_tool_calls", 3) or 3)

    if not search_svc_names:
        st.warning(t("orch_no_search_services", lang=lang))
        sel_search_svc = ""
        sel_search_mdl = ""
        sel_search_temp = 0.3
        sel_search_mtc = 3
        sel_search_re = ""
    else:
        if cur_search_svc not in search_svc_names:
            cur_search_svc = search_svc_names[0] if search_svc_names else ""

        saved_search_svc = cfg.get("search_service", "")
        saved_search_mdl = cfg.get("search_model", "")
        if saved_search_svc and saved_search_svc not in search_svc_names:
            st.warning(t("orch_service_unavailable", lang=lang, service=saved_search_svc))
        elif saved_search_svc in search_svc_names:
            saved_search_models = [
                m["id"] if isinstance(m, dict) else m
                for m in search_services.get(saved_search_svc, {}).get("models", [])
            ]
            if saved_search_mdl and saved_search_mdl not in saved_search_models:
                st.warning(t("orch_model_unavailable", lang=lang,
                             model=saved_search_mdl, service=saved_search_svc))

        col_wb1, col_wb2 = st.columns(2)
        with col_wb1:
            sel_search_svc = st.selectbox(t("orch_service_label", lang=lang), options=search_svc_names,
                                           index=search_svc_names.index(cur_search_svc) if cur_search_svc in search_svc_names else 0,
                                           key=f"orch_set_search_svc_{slug}",
                                           help=t("orch_service_label_help", lang=lang))
        with col_wb2:
            svc_def = search_services.get(sel_search_svc, {})
            models = svc_def.get("models", [])
            model_ids_search = [m["id"] if isinstance(m, dict) else m for m in models]
            if not model_ids_search:
                model_ids_search = [cur_search_mdl] if cur_search_mdl else []
            if cur_search_mdl not in model_ids_search and model_ids_search:
                cur_search_mdl = model_ids_search[0]
            mdl_idx_s = model_ids_search.index(cur_search_mdl) if cur_search_mdl in model_ids_search else 0
            sel_search_mdl = st.selectbox(t("orch_model_label", lang=lang), options=model_ids_search,
                                           index=mdl_idx_s, key=f"orch_set_search_mdl_{slug}",
                                           help=t("orch_model_label_help", lang=lang))

        search_svc_def = search_services.get(sel_search_svc, {})
        sel_search_temp = _temp_slider(search_svc_def, lang, t("orch_temperature_label", lang=lang), cur_search_temp,
                                        f"orch_set_search_temp_{slug}")
        sel_search_mtc = st.slider(t("orch_max_tool_calls", lang=lang), min_value=1, max_value=7,
                                    value=cur_search_mtc, step=1, key=f"orch_set_search_mtc_{slug}",
                                    help=t("orch_max_tool_calls_help", lang=lang))
        cur_search_re = str(cfg.get("search_reasoning_effort", "") or "").strip()
        sel_search_re = ""
        if service_supports_reasoning_effort(search_svc_def):
            re_opts = get_model_reasoning_effort_options(search_svc_def, sel_search_mdl)
            if not cur_search_re or cur_search_re not in re_opts:
                cur_search_re = default_reasoning_effort(search_svc_def, strong=False, model=sel_search_mdl) or (re_opts[0] if re_opts else "")
            if re_opts:
                sel_search_re = st.selectbox(
                    t("orch_reasoning_effort_label", lang=lang),
                    options=re_opts,
                    index=re_opts.index(cur_search_re) if cur_search_re in re_opts else 0,
                    key=f"orch_set_search_re_{slug}",
                    help=t("orch_reasoning_effort_label_help", lang=lang),
                )

    cur_search_prompt = str(cfg.get("web_search_prompt", "") or "").strip()
    if not cur_search_prompt:
        cur_search_prompt = get_web_search_config(slug).get("prompt", "")
    sel_search_prompt = st.text_area(
        t("orch_search_prompt_label", lang=lang),
        value=cur_search_prompt,
        height=120,
        key=f"orch_set_search_prompt_{slug}",
        help=t("orch_search_prompt_help", lang=lang),
    )

    # Save models
    if st.button(t("orch_save_models_btn", lang=lang), key=f"orch_save_models_{slug}", type="primary"):
        new_cfg = dict(cfg)
        new_cfg["strong_service"] = sel_strong_svc
        new_cfg["strong_model"] = sel_strong_mdl
        new_cfg["strong_temperature"] = sel_strong_temp
        new_cfg["strong_max_tokens"] = sel_strong_max_tokens
        new_cfg["strong_reasoning_effort"] = sel_strong_re
        new_cfg["weak_service"] = sel_weak_svc
        new_cfg["weak_model"] = sel_weak_mdl
        new_cfg["weak_temperature"] = sel_weak_temp
        new_cfg["weak_max_tokens"] = sel_weak_max_tokens
        new_cfg["weak_reasoning_effort"] = sel_weak_re
        new_cfg["search_service"] = sel_search_svc
        new_cfg["search_model"] = sel_search_mdl
        new_cfg["search_temperature"] = sel_search_temp
        new_cfg["search_max_tool_calls"] = sel_search_mtc
        new_cfg["search_reasoning_effort"] = sel_search_re
        new_cfg["web_search_prompt"] = sel_search_prompt
        save_orchestrator(slug, config=new_cfg)
        st.success(t("orch_save_models_ok", lang=lang))


def _render_prompt_settings(slug: str, lang: str) -> None:
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return
    prompt_text = orch.get("prompt_text", "")

    st.markdown(t("orch_prompt_section", lang=lang))
    st.caption(t("orch_prompt_label_help", lang=lang))
    new_prompt = st.text_area(
        label=t("orch_prompt_label", lang=lang),
        value=prompt_text,
        height=400,
        key=f"orch_prompt_{slug}",
        label_visibility="collapsed",
    )
    if st.button(t("orch_save_prompt_btn", lang=lang), key=f"orch_save_prompt_{slug}", type="primary"):
        save_orchestrator(slug, prompt_text=new_prompt)
        st.success(t("orch_save_prompt_ok", lang=lang))


def _render_economy_settings(slug: str, lang: str) -> None:
    orch = get_orchestrator(slug)
    if orch is None:
        st.error(t("orch_not_found", lang=lang))
        return
    economy_config = get_economy_config(slug)
    cur_tail = int(economy_config.get("tail_messages", get_economy_tail_messages(slug)))
    cur_cache_enabled = bool(economy_config.get("cache_enabled", False))
    cur_multiplier = int(economy_config.get("cache_multiplier", 2))

    st.markdown(t("orch_economy_section", lang=lang))
    st.markdown(t("orch_economy_desc", lang=lang))

    col_t1, col_t2 = st.columns([2, 1])
    with col_t1:
        sel_tail = st.slider(
            t("orch_economy_tail_label", lang=lang),
            min_value=4, max_value=30, value=cur_tail, step=2,
            key=f"orch_economy_tail_{slug}",
            help=t("orch_economy_tail_help", lang=lang),
        )
    with col_t2:
        st.metric("L", sel_tail)

    sel_cache_enabled = st.checkbox(
        t("orch_economy_cache_label", lang=lang),
        value=cur_cache_enabled,
        key=f"orch_economy_cache_{slug}",
        help=t("orch_economy_cache_help", lang=lang),
    )

    multiplier_options = [1, 2, 3, 4, 5]
    if cur_multiplier not in multiplier_options:
        cur_multiplier = 2
    sel_multiplier = st.selectbox(
        t("orch_economy_multiplier_label", lang=lang),
        options=multiplier_options,
        index=multiplier_options.index(cur_multiplier),
        format_func=lambda x: f"x{x}",
        key=f"orch_economy_multiplier_{slug}",
        help=t("orch_economy_multiplier_help", lang=lang),
    )

    if st.button(t("orch_save_btn", lang=lang), key=f"orch_save_economy_{slug}", type="primary"):
        cfg = orch.get("config", {})
        new_cfg = dict(cfg)
        new_cfg["economy_tail_messages"] = sel_tail
        new_cfg["economy_cache_enabled"] = bool(sel_cache_enabled)
        new_cfg["economy_cache_multiplier"] = int(sel_multiplier)
        save_orchestrator(slug, config=new_cfg)
        st.success(t("orch_economy_saved", lang=lang, tail=sel_tail))


def _render_orchestrator_functions_settings(slug: str, lang: str) -> None:
    """Manage custom Python functions of this orchestrator (stored in folder)."""
    st.markdown(t("orch_func_section_title", lang=lang))
    st.markdown(t("orch_func_section_desc", lang=lang))

    show_form = _ss(slug, "show_func_form") or False
    edit_name = _ss(slug, "edit_func") or None

    if show_form:
        edit_data = orch_get_function(slug, edit_name) if edit_name else None
        with st.container(border=True):
            st.subheader(t("orch_func_edit_title", lang=lang) if edit_data else t("orch_func_create_title", lang=lang))
            name = st.text_input(
                t("orch_func_name_label", lang=lang),
                value=edit_data.get("name", "") if edit_data else "",
                disabled=bool(edit_data),
                key=f"orch_func_name_{slug}",
                help=t("orch_func_name_help", lang=lang),
            )
            code = st.text_area(
                t("orch_func_code_label", lang=lang),
                value=edit_data.get("code", "") if edit_data else "",
                height=250,
                key=f"orch_func_code_{slug}",
                help=t("orch_func_code_help", lang=lang),
            )
            st.caption(t("orch_func_code_hint", lang=lang))
            c1, c2 = st.columns([1, 4])
            with c1:
                if st.button(t("orch_instr_save_btn", lang=lang), type="primary",
                             key=f"orch_func_save_{slug}"):
                    if not name.strip() or not code.strip():
                        st.error(t("orch_func_required", lang=lang))
                    else:
                        ok = orch_save_function(slug, name.strip(), code)
                        if ok:
                            _set_ss(slug, "show_func_form", False)
                            _set_ss(slug, "edit_func", None)
                            st.success(t("orch_func_saved", lang=lang))
                            st.rerun()
                        else:
                            st.error(t("orch_func_save_error", lang=lang))
            with c2:
                if st.button(t("orch_instr_cancel_btn", lang=lang), key=f"orch_func_cancel_{slug}"):
                    _set_ss(slug, "show_func_form", False)
                    _set_ss(slug, "edit_func", None)
                    st.rerun()
        return

    if st.button(t("orch_func_create_btn", lang=lang), key=f"orch_func_create_{slug}", type="primary"):
        _set_ss(slug, "show_func_form", True)
        _set_ss(slug, "edit_func", None)
        st.rerun()

    functions = orch_list_functions(slug)
    if not functions:
        st.info(t("orch_func_empty", lang=lang))
        return

    for fn in functions:
        with st.container(border=True):
            c_info, c_edit, c_del = st.columns([7, 1, 1])
            with c_info:
                st.markdown(f"**{fn.get('name', '?')}**")
                st.caption(f"📄 {fn.get('path', '')} · {fn.get('size_bytes', 0)} B")
            with c_edit:
                if st.button("✏️", key=f"orch_func_edit_{slug}_{fn['name']}"):
                    _set_ss(slug, "show_func_form", True)
                    _set_ss(slug, "edit_func", fn["name"])
                    st.rerun()
            with c_del:
                if st.button("🗑", key=f"orch_func_del_{slug}_{fn['name']}"):
                    orch_delete_function(slug, fn["name"])
                    st.success(t("orch_func_deleted", lang=lang))
                    st.rerun()


def _render_orchestrator_instructions_settings(slug: str, lang: str) -> None:
    """Manage orchestrator-specific instructions (stored in folder)."""
    st.markdown(t("orch_oinstr_section_title", lang=lang))
    st.markdown(t("orch_oinstr_section_desc", lang=lang))

    show_form = _ss(slug, "show_oinstr_form") or False
    edit_id = _ss(slug, "edit_oinstr") or None

    if show_form:
        edit_data = orch_get_instruction(slug, edit_id) if edit_id else None
        with st.container(border=True):
            st.subheader(t("orch_edit_instr_title", lang=lang) if edit_data else t("orch_create_instr_title", lang=lang))
            name = st.text_input(t("orch_instr_name_label", lang=lang), value=edit_data.get("name", "") if edit_data else "",
                                 key=f"orch_oinstr_name_{slug}",
                                 help=t("orch_instr_name_help", lang=lang))
            description = st.text_input(t("orch_instr_desc_label", lang=lang), value=edit_data.get("description", "") if edit_data else "",
                                        max_chars=200, key=f"orch_oinstr_desc_{slug}",
                                        help=t("orch_instr_desc_help", lang=lang))
            prompt_text = st.text_area(t("orch_instr_prompt_label", lang=lang),
                                       value=edit_data.get("text", "") if edit_data else "",
                                       height=250, key=f"orch_oinstr_prompt_{slug}",
                                       help=t("orch_instr_prompt_help", lang=lang))
            c1, c2 = st.columns([1, 4])
            with c1:
                if st.button(t("orch_instr_save_btn", lang=lang), type="primary", key=f"orch_oinstr_save_{slug}"):
                    if not name.strip() or not prompt_text.strip():
                        st.error(t("orch_instr_required", lang=lang))
                    else:
                        ok = orch_save_instruction(
                            slug, instruction_id=edit_id or "", name=name.strip(),
                            description=description.strip(), prompt_text=prompt_text,
                        )
                        if ok:
                            _set_ss(slug, "show_oinstr_form", False)
                            _set_ss(slug, "edit_oinstr", None)
                            st.success(t("orch_instr_saved", lang=lang))
                            st.rerun()
            with c2:
                if st.button(t("orch_instr_cancel_btn", lang=lang), key=f"orch_oinstr_cancel_{slug}"):
                    _set_ss(slug, "show_oinstr_form", False)
                    _set_ss(slug, "edit_oinstr", None)
                    st.rerun()
        return

    if st.button(t("orch_instr_create_btn", lang=lang), key=f"orch_oinstr_create_{slug}", type="primary"):
        _set_ss(slug, "show_oinstr_form", True)
        _set_ss(slug, "edit_oinstr", None)
        st.rerun()

    instructions = orch_list_instructions(slug)
    if not instructions:
        st.info(t("orch_oinstr_empty", lang=lang))
        return

    for inst in instructions:
        with st.container(border=True):
            c_info, c_edit, c_del = st.columns([7, 1, 1])
            with c_info:
                st.markdown(f"**{inst.get('name', '?')}**")
                if inst.get("description"):
                    st.caption(f"📝 {inst['description']}")
            with c_edit:
                if st.button("✏️", key=f"orch_oinstr_edit_{slug}_{inst['id']}"):
                    _set_ss(slug, "show_oinstr_form", True)
                    _set_ss(slug, "edit_oinstr", inst["id"])
                    st.rerun()
            with c_del:
                if st.button("🗑", key=f"orch_oinstr_del_{slug}_{inst['id']}"):
                    orch_delete_instruction(slug, inst["id"])
                    st.success(t("orch_instr_deleted", lang=lang))
                    st.rerun()


def _render_orch_skills_settings(slug: str, lang: str) -> None:
    """Manage enabled standardized skills (Skills library) for this orchestrator."""
    st.markdown(t("orch_skills_section_title", lang=lang))
    st.markdown(t("orch_skills_section_desc", lang=lang))

    try:
        # Only skills adapted for SagaAI can be assigned to an orchestrator;
        # non-adapted third-party skills remain visible in the Skills library
        # page where they can be adapted.
        all_skills = list_library_skills(adapted_only=True)
    except Exception:
        all_skills = []

    if not all_skills:
        st.info(t("orch_skills_empty", lang=lang))
        st.markdown(t("orch_skills_empty_hint", lang=lang))
        return

    enabled = set(get_enabled_skills(slug))
    selected = []
    for skill_item in all_skills:
        skill_id = skill_item["id"]
        checked = st.checkbox(
            f"**{skill_item['name']}** `{skill_id}`",
            value=(skill_id in enabled),
            key=f"orch_skill_{slug}_{skill_id}",
        )
        if skill_item.get("description"):
            st.caption(f"📝 {skill_item['description']}")
        st.caption(f"📁 {skill_item.get('folder', '')}")
        if checked:
            selected.append(skill_id)

    st.markdown("---")
    if st.button(t("orch_skills_save_btn", lang=lang), key=f"orch_save_skills_{slug}",
                 type="primary"):
        if set_enabled_skills(slug, selected):
            st.success(t("orch_skills_saved", lang=lang))
        else:
            st.error(t("orch_skills_save_error", lang=lang))


def _render_orch_rag_bases(slug: str, lang: str) -> None:
    """Assign RAG knowledge bases this orchestrator may use via rag_search."""
    st.markdown(t("orch_rag_bases_section_title", lang=lang))
    st.markdown(t("orch_rag_bases_section_desc", lang=lang))

    try:
        bases = list_bases_with_activity()
    except Exception:
        bases = []

    if not bases:
        st.info(t("orch_rag_bases_empty", lang=lang))
        return

    if slug == DEVAGENT_SLUG:
        st.info(t("orch_rag_bases_all_hint", lang=lang))
        for b in bases:
            bslug = str(b.get("slug") or "")
            if not bslug:
                continue
            st.markdown(f"**{b.get('name') or bslug}** `{bslug}` "
                        f"({str(b.get('status') or 'draft')})")
            if not bool(b.get("active", True)):
                st.caption("⚠ " + t("orch_rag_bases_inactive_hint", lang=lang))
        return

    assigned = set(get_orchestrator_rag_bases(slug))
    if not assigned:
        st.info(t("orch_rag_bases_none_allowed", lang=lang))
    selected = []
    for b in bases:
        bslug = str(b.get("slug") or "")
        if not bslug:
            continue
        checked = st.checkbox(
            f"**{b.get('name') or bslug}** `{bslug}`",
            value=(bslug in assigned),
            key=f"orch_rag_base_{slug}_{bslug}",
        )
        if not bool(b.get("active", True)):
            st.caption("⚠ " + t("orch_rag_bases_inactive_hint", lang=lang))
        if checked:
            selected.append(bslug)

    st.markdown("---")
    if st.button(t("orch_rag_bases_save_btn", lang=lang), key=f"orch_save_rag_bases_{slug}",
                 type="primary"):
        if set_orchestrator_rag_bases(slug, selected):
            st.success(t("orch_rag_bases_saved", lang=lang))
        else:
            st.error(t("orch_rag_bases_save_error", lang=lang))


def _render_orch_connections(slug: str, lang: str) -> None:
    """Manage enabled external-service connections for this orchestrator."""
    st.markdown(t("orch_connections_section_title", lang=lang))
    st.markdown(t("orch_connections_section_desc", lang=lang))

    try:
        all_conns = list_connections()
    except Exception:
        all_conns = []

    if not all_conns:
        st.info(t("orch_connections_empty", lang=lang))
        return

    enabled = set(get_enabled_connections(slug))
    selected = []
    for conn in all_conns:
        conn_id = str(conn.get("id") or "")
        if not conn_id:
            continue
        name = str(conn.get("name") or conn_id)
        svc = str(conn.get("service") or "?")
        account = str(conn.get("account") or "")
        label = f"**{name}** `{conn_id}` ({svc})"
        if account:
            label += f" · {account}"
        checked = st.checkbox(
            label,
            value=(conn_id in enabled),
            key=f"orch_conn_{slug}_{conn_id}",
        )
        if checked:
            selected.append(conn_id)

    st.markdown("---")
    if st.button(t("orch_connections_save_btn", lang=lang),
                 key=f"orch_save_connections_{slug}",
                 type="primary"):
        if set_enabled_connections(slug, selected):
            st.success(t("orch_connections_saved", lang=lang))
        else:
            st.error(t("orch_connections_save_error", lang=lang))


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN PAGE
# ═══════════════════════════════════════════════════════════════════════════════

def page_orchestrator(slug: str) -> None:
    _init_orch_state(slug)
    lang = st.session_state.get("ui_lang")
    orch = get_orchestrator(slug)
    orch_name = orch.get("name", slug) if orch else slug

    # DevAgent has its own icon, custom employees share the robots icon.
    icon = "🛠️" if slug == DEVAGENT_SLUG else "🤖🤖"
    st.title(f"{icon} {orch_name}")

    _render_chat_tab(slug, lang)


# ─── Backward-compat entry for DevAgent ───────────────────────────────────────

def page_devagent() -> None:
    """DevAgent page - delegates to the generic orchestrator page."""
    page_orchestrator(DEVAGENT_SLUG)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
