# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_feed - Step feed: event and tool-result rendering.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""
import json
import os
from typing import Optional

from ui.pages.orchestrator_shared import late, st_proxy
from ui.pages.orchestrator_text import (
    _extract_result_body,
    _first_two_lines,
    _format_call_args_preview,
    _split_events_by_run,
    _tool_result_title,
)

st = st_proxy()
_render_event = late("_render_event")
_render_tool_result = late("_render_tool_result")
t = late("t")


_VISIBLE_PHASES = ("task_classification", "creating_skill", "calling_llm",
                   "parsing", "executing")


_HIDDEN_EVENT_TYPES = {}


def _render_generated_image(result_data: dict, path: str, lang: str) -> None:
    """Show a generated image with a download button in a tool result.

    Called for successful ``generate_image`` results that carry a saved
    file path. A missing or unreadable file renders an actionable caption
    instead of breaking the whole feed render.
    """
    if not os.path.isfile(path):
        st.caption(t("event_tool_result_image_missing", lang=lang, path=path))
        return
    st.caption(t("event_tool_result_image", lang=lang))
    st.image(path)
    try:
        with open(path, "rb") as f:
            image_bytes = f.read()
    except OSError:
        st.caption(t("event_tool_result_image_missing", lang=lang, path=path))
        return
    st.download_button(
        t("event_tool_result_image_download", lang=lang),
        data=image_bytes,
        file_name=os.path.basename(path),
        mime=str(result_data.get("mime") or "image/jpeg"),
        key=f"orch_dl_img_{os.path.basename(path)}",
        use_container_width=True,
    )


def _render_parked_question(question: dict, lang: str) -> None:
    """Show a subagent's question, its options and what it is blocked on.

    The question is the only part of a parked result the reader has to act on,
    so it is rendered above the raw result body instead of being buried in a
    JSON dump. Everything the child wrote is data: the text is rendered as
    plain markdown, never interpreted as instructions.
    """
    text = " ".join(str(question.get("text") or "").split())
    if text:
        st.markdown(text)
    options = question.get("options")
    if isinstance(options, (list, tuple)) and options:
        for option in list(options)[:8]:
            st.markdown("- " + str(option))
    done = " ".join(str(question.get("done") or "").split())
    if done:
        st.caption(done)
    blocked = " ".join(str(question.get("blocked") or "").split())
    if blocked:
        st.caption(blocked)


def _tool_result_summary(result_data: dict, lang: str, parked_question: dict) -> str:
    """Return the outcome summary line shown after the title.

    The parked-question label replaces the generic "ok": the result of
    ``ask_parent`` is a question to a human, not a completed operation.
    """
    error = result_data.get("error", "")
    if result_data.get("ok") is False:
        if error:
            return f"⚠️ {error[:100]}"
        return t("event_tool_result_failed", lang=lang)
    total_lines = result_data.get("total_lines")
    bytes_val = result_data.get("bytes", 0)
    note = result_data.get("note", "")
    applied = result_data.get("applied")
    backup_ver = result_data.get("backup_version")
    parts = []
    if parked_question:
        parts.append(t("orch_subagent_question", lang=lang))
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
    return " | ".join(parts) if parts else t("event_tool_result_ok", lang=lang)


def _tool_result_header(ev: dict, lang: str, result_data: dict, tool: str,
                        parked_question: dict) -> str:
    """Return the expander header: title, optional assistant card, summary."""
    ok_val = result_data.get("ok")
    path = result_data.get("path", "")
    header_lines = [_tool_result_title(tool, ok_val, path)]

    # An invoke_assistant result carries the assistant card: the feed names
    # WHO answered instead of showing an eight-character id, which is the
    # only way a reader can tell two one-shot calls apart.
    inner_result = result_data.get("result")
    if isinstance(inner_result, dict) and isinstance(inner_result.get("assistant"), dict):
        assistant_meta = inner_result["assistant"]
        name = str(assistant_meta.get("name") or "").strip()
        aid = str(assistant_meta.get("id") or "").strip()
        header_lines.append(t("event_tool_result_assistant", lang=lang,
                              name=name or aid or "?", id=aid or "?"))

    header_lines.append(_tool_result_summary(result_data, lang, parked_question))
    return "\n".join(header_lines)


def _render_tool_call_args(ev: dict, lang: str, call_ev: Optional[dict]) -> None:
    """Render the call arguments of a tool result inside its expander."""
    call_args = None
    if call_ev is not None:
        call_args = call_ev.get("args", {})
    elif ev.get("args"):
        call_args = ev.get("args", {})
    if call_args is None:
        return
    st.markdown(f"**{t('event_tool_call_title', lang=lang)}**")
    st.code(_format_call_args_preview(call_args), language="json")
    if any(isinstance(v, str) and len(v) > 300 for v in call_args.values()):
        full_args_json = json.dumps(call_args, ensure_ascii=False, indent=2)
        with st.expander(t("event_tool_result_show_more", lang=lang), expanded=False):
            st.code(full_args_json, language="json")


def _render_result_payload(result_data: dict, lang: str) -> None:
    """Render the result body: full JSON, or two lines plus a full view."""
    st.markdown(f"**{t('event_tool_result_title', lang=lang)}**")
    body, is_json_body, full_json = _extract_result_body(result_data)
    if is_json_body:
        st.code(full_json, language="json")
        return
    preview, full = _first_two_lines(body)
    if preview:
        st.code(preview, language="")
    else:
        st.caption(t("event_tool_result_empty", lang=lang))
    if full != preview:
        with st.expander(t("event_tool_result_show_more", lang=lang), expanded=False):
            st.code(full, language="")


def _render_tool_result_impl(ev: dict, lang: str, call_ev: Optional[dict] = None) -> None:
    """Render one tool_result event inside an expander.

    The expander header keeps the existing compact format (e.g.
    ``✅ tool_result: read_file → path 322 строки``). Inside, the call
    arguments and the first two lines of the result are shown; when the
    result is longer, the full content is available in a nested expander.
    A successful ``generate_image`` result additionally renders its image
    block in the feed itself, outside the collapsed expander, so the picture
    and its download button are visible without an extra click.
    """
    result_data = ev.get("result", {})
    tool = ev.get("tool", "?")

    # A parked child reports its question through the tool result: the block is
    # labelled as a subagent question, so a reader does not have to decode a
    # run id from a raw result dump to learn that a child waits for them.
    parked_question = {}
    if tool == "ask_parent" and isinstance(result_data.get("question"), dict):
        parked_question = result_data["question"]

    header = _tool_result_header(ev, lang, result_data, tool, parked_question)
    with st.expander(header, expanded=bool(parked_question)):
        _render_tool_call_args(ev, lang, call_ev)
        if parked_question:
            _render_parked_question(parked_question, lang)
        _render_result_payload(result_data, lang)

    # A generated image is shown in the feed itself, outside the collapsed
    # tool-result expander: the picture must be visible without an extra click.
    if tool == "generate_image" and result_data.get("ok") and result_data.get("path"):
        _render_generated_image(result_data, result_data.get("path", ""), lang)


def _render_event_group(events: list, lang: str) -> None:
    """Render the events of ONE run, pairing tool_call with tool_result.

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


def _render_events(events: list, lang: str) -> None:
    """Render the step feed, one block per run.

    Events of a single run (the normal case) are rendered inline exactly as
    before. When a step fanned out, the dialog's own events stay inline and
    every child run is folded into its own expander: the parent must not
    lose its place in the feed, and a running child must stay collapsible
    rather than pushing the dialog's own output off screen.
    """
    groups = _split_events_by_run(events)
    if len(groups) <= 1:
        _render_event_group(events, lang)
        return
    min_depth = min(depth for _rid, depth, _slug, _chunk in groups)
    for rid, depth, agent_slug, chunk in groups:
        if depth == min_depth:
            _render_event_group(chunk, lang)
            continue
        label = t("orch_child_run_header", lang=lang,
                  agent=agent_slug or "?", run=rid[:8], depth=depth)
        with st.expander(label, expanded=False):
            _render_event_group(chunk, lang)


def _render_tool_call_event(ev: dict, lang: str) -> None:
    """Render a tool_call event as one compact info line."""
    tool = ev.get("tool", "?")
    display_args = dict(ev.get("args", {}))
    for key in list(display_args.keys()):
        val = display_args[key]
        if isinstance(val, str) and len(val) > 200:
            display_args[key] = val[:200] + "…"
    st.info(f"🔧 **{tool}** `{display_args}`")


def _render_applied_event(ev: dict, lang: str) -> None:
    """Render an applied event: a file was written to disk."""
    st.success(t("devagent_applied", lang=lang, path=ev.get("path", "?")))


def _render_skill_event(ev: dict, lang: str) -> None:
    """Render an assistant_created event with its optional evaluation note."""
    skill_name = ev.get("skill_name", "?")
    eval_text = ev.get("evaluation", "")
    st.success(t("event_skill_created", lang=lang, skill=skill_name))
    if eval_text:
        st.caption(eval_text[:200])


def _render_phase_event(ev: dict, lang: str) -> None:
    """Render a phase event; unknown phases are reported by name."""
    phase = ev.get("phase", "")
    if phase not in _VISIBLE_PHASES:
        return
    labels = {
        "calling_llm": t("event_phase_calling_llm", lang=lang),
        "parsing": t("event_phase_parsing", lang=lang),
        "executing": t("event_phase_executing", lang=lang),
    }
    st.caption(labels.get(phase, t("event_phase_unknown", lang=lang, phase=phase)))


def _render_retry_event(ev: dict, lang: str) -> None:
    """Render an LLM retry notice; whole-number delays lose the decimal."""
    attempt = ev.get("attempt", "?")
    attempts = ev.get("attempts", "?")
    delay = ev.get("delay", 0)
    if isinstance(delay, float) and delay.is_integer():
        delay = int(delay)
    st.warning(t("orch_retry_llm", lang=lang,
                 attempt=attempt, attempts=attempts, delay=delay))


def _render_incomplete_retry_event(ev: dict, lang: str) -> None:
    """Render a truncated-answer retry notice."""
    reason = ev.get("reason", "") or "max_output_tokens"
    st.warning(t("orch_incomplete_retry", lang=lang, reason=reason))


def _render_sanitized_event(ev: dict, lang: str) -> None:
    """Render one warning per payload withheld by the injection guard."""
    for info in ev.get("events", []):
        reason = info.get("reason", "")
        tool_name = info.get("tool", "")
        file_path = info.get("path", "")
        if file_path:
            st.warning(f"🛡️ **Prompt-injection protection**: `{file_path}`\n\n{reason}")
        elif tool_name:
            st.warning(f"🛡️ **Prompt-injection protection**: tool `{tool_name}`\n\n{reason}")
        else:
            st.warning(f"🛡️ **Prompt-injection protection**: {reason}")


def _render_error_event(ev: dict, lang: str) -> None:
    """Render a loop error event."""
    st.error(ev.get("error", t("orch_unknown_error", lang=lang)))


_EVENT_RENDERERS = {
    "applied": _render_applied_event,
    "assistant_created": _render_skill_event,
    "error": _render_error_event,
    "incomplete_retry": _render_incomplete_retry_event,
    "phase": _render_phase_event,
    "retrying_llm": _render_retry_event,
    "sanitized_detected": _render_sanitized_event,
}


def _render_event_impl(ev: dict, lang: str) -> None:
    """Render one feed event; unknown event types render nothing.

    ``tool_call`` and ``tool_result`` are handled here because the pair is
    what the feed is built around: a call line and the expander with its
    result. Every other type maps to one renderer in ``_EVENT_RENDERERS``.
    """
    etype = ev.get("type")
    if etype in _HIDDEN_EVENT_TYPES:
        return
    if etype == "tool_call":
        _render_tool_call_event(ev, lang)
        return
    if etype == "tool_result":
        _render_tool_result(ev, lang)
        return
    renderer = _EVENT_RENDERERS.get(etype)
    if renderer is not None:
        renderer(ev, lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
