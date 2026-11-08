# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_chat - Chat tab: feed, confirmations, message rendering and controls.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""

from core.orchestrator_strong import DEVAGENT_SLUG, resolve_strong_service
from core.render import (
    _md_to_txt,
    clipboard_button,
    format_ts_label,
)
from dev_agent.agent_loop import (
    approve_pending_confirmation,
    approve_sanitized_content,
    deny_pending_confirmation,
    deny_sanitized_content,
)
from ui.pages.orchestrator_chat_input import _handle_chat_submission
from ui.pages.orchestrator_feed import _render_events
from ui.pages.orchestrator_loop import (
    _cancel_dialog_tree,
    _render_subagent_status,
)
from ui.pages.orchestrator_shared import late, st_proxy
from ui.pages.orchestrator_state import (
    CHAT_PAGE_SIZE,
    ECONOMY_MODE_ALWAYS_ON,
    _pop_ss,
)
from ui.pages.orchestrator_text import (
    _is_agent_active,
    _last_assistant_index,
    _strip_tool_calls_for_message,
)

st = st_proxy()
_assistant_has_api_key = late("_assistant_has_api_key")
_do_step = late("_do_step")
_make_dispatcher = late("_make_dispatcher")
_make_send_adapter = late("_make_send_adapter")
_render_token_line = late("_render_token_line")
_set_ss = late("_set_ss")
_ss = late("_ss")
get_orchestrator = late("get_orchestrator")
render_autoscroll = late("render_autoscroll")
t = late("t")


def _render_chat_tab(slug: str, lang: str) -> None:
    """Render the chat tab: feed, confirmations, input and live status.

    The function is a plain sequence of steps; each step lives in its own
    helper below. Every helper stays in THIS module on purpose: the tests and
    the page itself resolve ``st`` and ``t`` through the module globals, so a
    helper moved elsewhere would read its own namespace and stop seeing a
    patch installed here.
    """
    orch = get_orchestrator(slug)
    orch_name = orch.get("name", slug) if orch else slug

    _render_chat_styles()

    economy_enabled = ECONOMY_MODE_ALWAYS_ON

    if not _render_chat_header(orch, orch_name, lang):
        return

    _pending_task = _pop_ss(slug, "pending_task")
    if _pending_task and _ss(slug, "loop_state") is None:
        _set_ss(slug, "user_message", _pending_task)
        _do_step(slug=slug, lang=lang)
        st.rerun()

    history = _ss(slug, "history") or []

    if not history:
        _render_chat_welcome(slug, orch_name, lang)

    # Read the loop status before rendering messages so we know whether the
    # agent is still active and which assistant message is the final one.
    loop_state = _ss(slug, "loop_state")
    agent_is_active = _is_agent_active(loop_state)

    _apply_autoscroll(slug, agent_is_active)

    last_assistant_idx = _last_assistant_index(history)

    show_count = _chat_show_count(slug, history)
    _render_show_earlier(slug, lang, history, show_count)
    _render_chat_feed(slug, lang, history, show_count, last_assistant_idx, agent_is_active)

    _render_loop_status(slug, lang, loop_state)

    _render_subagent_status(slug, lang)

    if not agent_is_active:
        _handle_chat_submission(slug, lang, orch)

    # Token usage indicator (single line, after Upload). The heavy
    # context/economy computation is cached in _render_token_line and only
    # rerun when one of its inputs (history, economy config, loop state,
    # prompt, attachments, services) actually changes.
    _render_token_line(slug, history, economy_enabled, loop_state, lang)

    if agent_is_active:
        _render_active_controls(slug, lang, loop_state)


def _render_chat_header(orch, orch_name: str, lang: str) -> bool:
    """Render the page caption and check that the strong model has a key.

    The caption is neutral on purpose: this page hosts ANY employee, so it
    shows the employee's own description and falls back to the display name
    rather than the legacy "developer assistant" wording.  Returns False when
    the strong service has no API key - the caller stops rendering, the same
    way the page always did.
    """
    _desc = (orch.get("description") or "").strip() if orch else ""
    st.caption(_desc if _desc else orch_name)
    st.markdown("---")

    orch_cfg = orch.get("config", {}) if orch else {}
    if not isinstance(orch_cfg, dict):
        orch_cfg = {}
    strong_svc = resolve_strong_service(orch_cfg)
    if not strong_svc or not _assistant_has_api_key(strong_svc):
        st.warning(t("orch_no_api_key", lang=lang))
        return False
    return True


def _render_chat_styles() -> None:
    """Install the page-level CSS: hidden uploader list and coloured buttons."""
    st.markdown(
        "<style>\n"
        "[data-testid=\"stFileUploader\"] [data-testid=\"stFileUploaderFile\"] { display: none !important; }\n"
        "button[key^=\"orch_reset_\"] { background-color: #28a745 !important; color: white !important; border-color: #28a745 !important; }\n"
        "button[key^=\"orch_reset_\"]:hover { background-color: #218838 !important; border-color: #1e7e34 !important; }\n"
        "button[key^=\"orch_stop_\"] { background-color: #dc3545 !important; color: white !important; border-color: #dc3545 !important; }\n"
        "button[key^=\"orch_stop_\"]:hover { background-color: #c82333 !important; border-color: #bd2130 !important; }\n"
        "</style>",
        unsafe_allow_html=True,
    )


def _render_chat_welcome(slug: str, orch_name: str, lang: str) -> None:
    """Show the opening greeting of an empty dialog."""
    with st.chat_message("assistant"):
        if slug == DEVAGENT_SLUG:
            _welcome = t("orch_welcome_msg_devagent", lang=lang)
        else:
            _welcome = t("orch_welcome_msg", lang=lang, name=orch_name)
        st.markdown(_welcome)


def _apply_autoscroll(slug: str, agent_is_active: bool) -> None:
    """Follow the newest output while the agent works.

    A just-sent user message sets scroll_bottom, which forces one scroll
    regardless of a manual pause; the task feed then follows the output until
    the user scrolls up (see core.feed_autoscroll).
    """
    _force_scroll = bool(_ss(slug, "scroll_bottom"))
    if _force_scroll:
        _set_ss(slug, "scroll_bottom", False)
    if _force_scroll or agent_is_active:
        render_autoscroll(force=_force_scroll)


def _chat_show_count(slug: str, history: list) -> int:
    """Return how many trailing messages the feed renders right now."""
    show_count = int(_ss(slug, "chat_show_count") or CHAT_PAGE_SIZE)
    return max(CHAT_PAGE_SIZE, min(show_count, len(history) or 1))


def _render_show_earlier(slug: str, lang: str, history: list, show_count: int) -> None:
    """Render the "show earlier" button above a paginated feed."""
    if len(history) <= show_count:
        return
    _remaining = len(history) - show_count
    if st.button(
        t("orch_show_earlier", lang=lang, count=_remaining),
        key=f"orch_show_earlier_{slug}",
        use_container_width=True,
    ):
        _set_ss(slug, "chat_show_count",
                min(show_count + CHAT_PAGE_SIZE, len(history)))
        st.rerun()


def _render_chat_feed(slug: str, lang: str, history: list, show_count: int,
                      last_assistant_idx, agent_is_active: bool) -> None:
    """Render the trailing window of the dialog feed."""
    _offset = max(0, len(history) - show_count)
    for idx, msg in enumerate(history[_offset:], start=_offset):
        _render_history_message(slug, lang, idx, msg, last_assistant_idx, agent_is_active)


def _render_history_message(slug: str, lang: str, idx: int, msg: dict,
                            last_assistant_idx, agent_is_active: bool) -> None:
    """Render one stored message: a notice banner, a user line or an answer."""
    if msg.get("_notice"):
        # A finished child reports back as a hidden system message: the
        # model reads it silently, the user sees a compact banner.
        st.info("📬 " + str(msg.get("content") or ""))
        return
    if msg.get("hidden"):
        return
    role = msg.get("role")
    content = msg.get("content", "")
    fname = msg.get("file_name", "")

    # Time display (HH:MM DD.MM.YYYY, same format as the assistant chat)
    ts_display = format_ts_label(msg.get("ts", ""))

    if role == "user":
        _render_user_message(msg, content, fname, ts_display, lang)
    elif role == "assistant":
        _render_assistant_message(slug, lang, idx, msg, content, ts_display,
                                  last_assistant_idx, agent_is_active)


def _render_user_message(msg: dict, content: str, fname: str, ts_display: str, lang: str) -> None:
    """Render a user message with its attachment name and step events."""
    user_events = msg.get("_events", [])
    with st.chat_message("user"):
        if fname:
            st.caption(f"📎 {fname}")
        st.markdown(content)
        if user_events:
            _render_events(user_events, lang)
        if ts_display:
            st.caption(f"🕐 {ts_display}")


def _render_assistant_message(slug: str, lang: str, idx: int, msg: dict, content: str,
                              ts_display: str, last_assistant_idx, agent_is_active: bool) -> None:
    """Render an assistant answer, its step events and its copy controls."""
    if not content:
        return
    display, has_tool_calls = _strip_tool_calls_for_message(content)
    stored_events = msg.get("_events", [])
    with st.chat_message("assistant"):
        if display:
            st.markdown(display)
        elif not stored_events:
            st.caption(t("devagent_agent_step_compact", lang=lang))
        if stored_events:
            _render_events(stored_events, lang)
        # Download/copy controls appear on every plain-prose LLM
        # answer that carries no tool calls; the live step keeps
        # them hidden until the agent loop finishes.
        show_controls = bool(display) and not has_tool_calls
        if show_controls and idx == last_assistant_idx and agent_is_active:
            show_controls = False
        if show_controls:
            _render_message_controls(slug, idx, display, lang)
        if ts_display:
            st.caption(f"🕐 {ts_display}")


def _render_message_controls(slug: str, idx: int, display: str, lang: str) -> None:
    """Render the four download/copy buttons of one answer."""
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


def _render_loop_status(slug: str, lang: str, loop_state) -> None:
    """Render the status bar and the parked confirmation dialogs."""
    if loop_state is None:
        return
    if loop_state.phase == "error":
        st.error(loop_state.error_message or t("orch_unknown_error", lang=lang))
        return
    if loop_state.phase != "done":
        return
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
        _render_danger_confirmation(slug, lang, loop_state)
    elif status == "sanitized_required":
        _render_sanitized_confirmation(slug, lang, loop_state)


def _resume_after_confirmation(slug: str, lang: str, new_state, events: list) -> None:
    """Store the resumed loop state, attach its events and continue the loop."""
    # Attach events to the last assistant message for history.
    if new_state.history and events:
        for i in range(len(new_state.history) - 1, -1, -1):
            if new_state.history[i].get("role") == "assistant":
                new_state.history[i]["_events"] = events
                break
    _set_ss(slug, "loop_state", new_state)
    _set_ss(slug, "history", new_state.history)
    _do_step(slug=slug, lang=lang)
    st.rerun()


def _render_danger_confirmation(slug: str, lang: str, loop_state) -> None:
    """Render the dangerous-operation confirmation dialog."""
    tool = getattr(loop_state, "pending_confirmation_tool", None)
    reasons = getattr(loop_state, "pending_confirmation_reasons", [])
    code_snippet = getattr(loop_state, "pending_confirmation_code", "")
    if not tool:
        return
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
    _render_confirmation_buttons(
        slug, lang, loop_state,
        approve_fn=approve_pending_confirmation,
        deny_fn=deny_pending_confirmation,
        approve_key=f"orch_allow_danger_{slug}",
        deny_key=f"orch_deny_danger_{slug}",
        approve_label_key="orch_allow_execute",
        deny_label_key="orch_deny_execute",
    )


def _render_sanitized_confirmation(slug: str, lang: str, loop_state) -> None:
    """Render the withheld-content confirmation dialog."""
    sanitized_events = getattr(loop_state, "sanitized_events", [])
    if not sanitized_events:
        return
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
    _render_confirmation_buttons(
        slug, lang, loop_state,
        approve_fn=approve_sanitized_content,
        deny_fn=deny_sanitized_content,
        approve_key=f"orch_allow_sanitized_{slug}",
        deny_key=f"orch_deny_sanitized_{slug}",
        approve_label_key="orch_allow_view",
        deny_label_key="orch_deny_view",
    )


def _render_confirmation_buttons(slug: str, lang: str, loop_state, *,
                                 approve_fn, deny_fn, approve_key: str, deny_key: str,
                                 approve_label_key: str, deny_label_key: str) -> None:
    """Render the allow/deny pair shared by both confirmation dialogs.

    Allowing resumes the parked call with a live dispatcher, so the loop can
    execute the operation it was waiting on; denying resumes it without one.
    """
    col_allow, col_deny = st.columns(2)
    with col_allow:
        if st.button(
            t(approve_label_key, lang=lang),
            key=approve_key,
            type="primary",
            use_container_width=True,
        ):
            dispatcher = _make_dispatcher(slug)
            dispatcher.core.set_send_request(_make_send_adapter(lang, slug))
            events: list = []

            def on_event(ev):
                events.append(ev)

            new_state = approve_fn(loop_state, dispatcher, lang=lang, on_event=on_event)
            _resume_after_confirmation(slug, lang, new_state, events)
    with col_deny:
        if st.button(
            t(deny_label_key, lang=lang),
            key=deny_key,
            type="secondary",
            use_container_width=True,
        ):
            events: list = []

            def on_event(ev):
                events.append(ev)

            new_state = deny_fn(loop_state, lang=lang, on_event=on_event)
            _resume_after_confirmation(slug, lang, new_state, events)


def _render_active_controls(slug: str, lang: str, loop_state) -> None:
    """Render the Stop button and drive the next loop step while active."""
    if st.button(t("orch_stop_btn", lang=lang), key=f"orch_stop_{slug}",
                 type="secondary", use_container_width=True):
        _cancel_dialog_tree(slug, loop_state)
        _set_ss(slug, "stop_requested", True)
        st.rerun()
    with st.spinner(t("orch_thinking", lang=lang)):
        _do_step(slug=slug, lang=lang)
    st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
