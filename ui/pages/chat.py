"""ui.pages.chat - page_run_query: chat interface for sending queries to AI assistants."""
import streamlit as st

from core.api_errors import APIError, api_error_message
from core.api_layer import send_request
from core.assistants import (
    get_assistant_by_id,
    list_assistant_files,
    load_assistant_files_context,
    load_assistants_index,
)
from core.files import (
    MAX_UPLOAD_TOKENS,
    check_context,
    check_upload_tokens,
    extract_file_content,
    get_file_uploader_types,
)
from core.fs import combine_nonempty
from core.i18n import t
from core.recent_assistants import record_assistant_use
from core.render import _md_to_txt, clipboard_button, format_token_line, format_ts_label
from core.services import get_services
from core.threads import (
    append_thread_message,
    create_thread,
    load_thread_messages,
    load_thread_meta,
    messages_to_api_history,
    save_thread_file,
    sum_thread_tokens,
)


def _get_preselected(assistant_id_by_name: dict):
    """Resolve the preselected assistant id with legacy-key fallback."""
    val = st.session_state.get("selected_assistant_id") or st.session_state.get("selected_skill_id")
    return val if val in assistant_id_by_name else None


def _uploader_counter() -> int:
    """Return the upload-reset counter, bumped on every file detach."""
    return int(st.session_state.get("upload_counter", 0) or 0)


def _uploader_key(base: str) -> str:
    """Return the file-uploader widget key for the chat send form."""
    return f"{base}_{_uploader_counter()}"


def _detach_file() -> None:
    """Clear the attached file and invalidate the uploader widget."""
    st.session_state["attached_file_context"] = ""
    st.session_state["attached_file_name"]    = ""
    st.session_state["upload_counter"] = _uploader_counter() + 1


def page_run_query():
    """Chat page - assistant selector, message history, send form."""
    index = load_assistants_index()
    lang = st.session_state.get("ui_lang")

    if not index:
        st.title(f"\U0001F4AC {t('nav_run', lang=lang)}")
        st.warning(t("no_skills", lang=lang))
        return

    active_tid = st.session_state.get("active_thread_id")
    sel_id, sel_name, preselected_id, id_by_id = _resolve_assistant(index, active_tid, lang)

    assistant = get_assistant_by_id(sel_id)
    if not assistant:
        st.error(t("skill_not_found", lang=lang))
        return

    _warn_saved_model(lang, assistant)
    # Record the assistant as recently used.
    record_assistant_use(sel_id)

    _render_header(lang, active_tid)
    _render_quick_actions(lang, sel_id)
    _render_assistant_info(lang, sel_id, sel_name, preselected_id, id_by_id, active_tid)
    _render_history(lang, active_tid)

    file_context = st.session_state.get("attached_file_context", "")
    file_name = st.session_state.get("attached_file_name", "")
    _render_attached_badge(lang, file_name, file_context)
    send_clicked, user_input = _render_send_form(lang, active_tid)
    _render_token_indicator(lang, assistant, sel_id, active_tid, file_context)

    if send_clicked:
        _handle_send(lang, assistant, sel_id, sel_name, active_tid, user_input)


def _resolve_assistant(index: list, active_tid, lang: str) -> tuple:
    """Return (sel_id, sel_name, preselected_id, id_by_id) for this page."""
    id_by_id = {p["id"]: p for p in index}
    preselected_id = _get_preselected(id_by_id)

    if preselected_id and preselected_id in id_by_id:
        return preselected_id, id_by_id[preselected_id]["name"], preselected_id, id_by_id
    if active_tid:
        meta = load_thread_meta(active_tid)
        first = list(id_by_id.values())[0]
        sel_id = meta.get("assistant_id") or meta.get("skill_id") or first["id"]
        sel_name = meta.get("assistant_name") or meta.get("skill_name") or first["name"]
        return sel_id, sel_name, preselected_id, id_by_id

    opts = {p["name"]: p["id"] for p in index}
    col_assistant, col_info = st.columns([4, 3])
    with col_assistant:
        sel_name = st.selectbox(t("select_skill", lang=lang), list(opts.keys()),
                                key="chat_assistant_select", label_visibility="visible")
        sel_id = opts[sel_name]
    assistant_tmp = get_assistant_by_id(sel_id)
    if assistant_tmp:
        with col_info:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            sf = list_assistant_files(sel_id)
            files_badge = f" \u00b7 \U0001F4CE {len(sf)}" if sf else ""
            st.caption(
                f"\U0001F527 {assistant_tmp.get('service','?')} \u203a {assistant_tmp['model']} "
                f"| T={assistant_tmp['temperature']}{files_badge}"
            )
    return sel_id, sel_name, preselected_id, id_by_id


def _warn_saved_model(lang: str, assistant: dict) -> None:
    """Warn when the assistant's stored provider or model is gone."""
    services = get_services()
    saved_svc = assistant.get("service", "")
    if saved_svc and saved_svc not in services:
        st.warning(t("assistant_service_unavailable", lang=lang, service=saved_svc))
        return
    if saved_svc in services:
        saved_models = [
            m.get("id") if isinstance(m, dict) else m
            for m in services.get(saved_svc, {}).get("models", [])
        ]
        if assistant.get("model") and assistant["model"] not in saved_models:
            st.warning(t("assistant_model_unavailable", lang=lang,
                         model=assistant["model"], service=saved_svc))


def _render_header(lang: str, active_tid) -> None:
    """Render the page title of a new dialog or the header of a thread."""
    if not active_tid:
        st.title(f"\U0001F4AC {t('nav_run', lang=lang)}")
        st.markdown("---")
        return
    meta = load_thread_meta(active_tid)
    title_text = meta.get("title") or t("untitled", lang=lang)
    author = meta.get("assistant_name", "") or meta.get("skill_name", "")
    created = str(meta.get("created_at", ""))[:10]
    header = (
        '<h2 style="margin:0;padding-bottom:2px">\U0001F4AC ' + title_text[:55] + '</h2>'
        + '<p style="color:#888;font-size:0.8rem;margin:0">'
        + '\U0001F527 ' + author + ' &nbsp;|&nbsp; \U0001F4C5 ' + created
        + '</p>'
    )
    st.markdown(header, unsafe_allow_html=True)
    st.markdown("---")


def _render_quick_actions(lang: str, sel_id: str) -> None:
    """Render the settings and new-dialog buttons above the feed."""
    col_set, col_new = st.columns([1, 1])
    with col_set:
        if st.button("\u2699\ufe0f " + t("nav_settings", lang=lang),
                     key=f"chat_settings_{sel_id}", use_container_width=True):
            values = {"last_active_entity_type": "assistant",
                      "last_active_entity_id": sel_id, "selected_assistant_id": sel_id,
                      "selected_skill_id": sel_id, "show_assistant_form": True,
                      "show_skill_form": True, "edit_assistant_id": sel_id,
                      "edit_skill_id": sel_id, "current_page": "skills"}
            st.session_state.update(values)
            st.rerun()
    with col_new:
        if st.button("\U0001F504 " + t("sidebar_new_dialog", lang=lang),
                     key=f"chat_new_dialog_{sel_id}", use_container_width=True):
            st.session_state.update({
                "active_thread_id": None, "attached_file_context": "",
                "attached_file_name": "", "force_send": False,
                "selected_assistant_id": sel_id, "selected_skill_id": sel_id,
                "current_page": "run"})
            st.rerun()


def _render_assistant_info(lang: str, sel_id: str, sel_name: str,
                           preselected_id, id_by_id: dict, active_tid) -> None:
    """Render the collapsible model card, when no selector is on screen."""
    if not ((preselected_id and preselected_id in id_by_id) or active_tid):
        return
    assistant_tmp = get_assistant_by_id(sel_id)
    if not assistant_tmp:
        return
    with st.expander(f"\U0001F527 {sel_name}", expanded=False):
        st.caption(f"{assistant_tmp.get('service','?')} \u203a {assistant_tmp['model']} | T={assistant_tmp['temperature']}")
        sf = list_assistant_files(sel_id)
        if sf:
            st.caption(t("skill_files_caption", lang=lang, files=", ".join(
                fn[:-4] if fn.endswith(".txt") else fn for fn in sf
            )))


def _render_history(lang: str, active_tid) -> None:
    """Render the stored messages of the active thread."""
    if not active_tid:
        return
    for msg_idx, msg in enumerate(load_thread_messages(active_tid)):
        ts_display = format_ts_label(msg.get("ts", ""))
        if msg["role"] == "user":
            with st.chat_message("user"):
                if msg.get("file_name", ""):
                    st.caption(f"\U0001F4CE {msg['file_name']}")
                st.markdown(msg["content"])
                if ts_display:
                    st.caption(f"\U0001F550 {ts_display}")
        else:
            with st.chat_message("assistant"):
                st.markdown(msg["content"])
                _render_message_actions(lang, msg["content"], msg_idx, active_tid, ts_display)


def _render_message_actions(lang: str, content: str, msg_idx: int,
                            active_tid: str, ts_display: str) -> None:
    """Render the download/copy controls and the timestamp of one answer."""
    dl_ts = ts_display.replace(":", "-") if ts_display else str(msg_idx)
    dl_fname = f"response_{dl_ts}_{msg_idx}"
    c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 5])
    with c1:
        st.download_button(
            t("chat_dl_md", lang=lang), data=content.encode("utf-8"),
            file_name=f"{dl_fname}.md", mime="text/markdown",
            key=f"dl_md_{msg_idx}_{active_tid}", use_container_width=True)
    with c2:
        st.download_button(
            t("chat_dl_txt", lang=lang), data=_md_to_txt(content).encode("utf-8"),
            file_name=f"{dl_fname}.txt", mime="text/plain",
            key=f"dl_txt_{msg_idx}_{active_tid}", use_container_width=True)
    with c3:
        clipboard_button(content, key=f"cp_md_{msg_idx}_{active_tid}", label=t("chat_cp_md", lang=lang))
    with c4:
        clipboard_button(_md_to_txt(content), key=f"cp_txt_{msg_idx}_{active_tid}", label=t("chat_cp_txt", lang=lang))
    with c5:
        if ts_display:
            st.caption(f"\U0001F550 {ts_display}")


def _file_badge_html(lang: str, file_name: str, file_context: str) -> str:
    """Return the HTML badge of the attached file."""
    size = f"({len(file_context):,} {t('chars_abbr', lang=lang)})"
    return (
        '<div style="background:#1e3a5f;border-radius:6px;padding:4px 10px;font-size:0.82rem;'
        'color:#7eb8f7;margin-bottom:6px;display:inline-block">\U0001F4CE ' + file_name
        + ' <span style="color:#aaa">' + size + '</span></div>')


def _render_attached_badge(lang: str, file_name: str, file_context: str) -> None:
    """Render the attached-file badge with its detach button."""
    if not file_name:
        return
    c_b, c_r = st.columns([10, 1])
    with c_b:
        st.markdown(_file_badge_html(lang, file_name, file_context), unsafe_allow_html=True)
    with c_r:
        if st.button("\u2715", key="remove_file", help=t("detach_file_help", lang=lang)):
            _detach_file()
            st.rerun()


def _render_uploader_styles() -> None:
    """Shrink the file uploader inside the send form."""
    st.markdown(
        '<style>'
        'div[data-testid="stFileUploader"] section[data-testid="stFileUploaderDropzone"]{'
        'padding:0.12rem 0.38rem !important;min-height:2.05rem !important;border-radius:0.5rem !important;}'
        'div[data-testid="stFileUploader"] button{padding:0.1rem 0.45rem !important;'
        'font-size:0.80rem !important;min-height:1.9rem !important;}'
        'div[data-testid="stFileUploader"] small{display:none !important;}'
        'div[data-testid="stFileUploader"] label{margin-bottom:0 !important;'
        'font-size:0 !important;height:0 !important;min-height:0 !important;}'
        '</style>',
        unsafe_allow_html=True)


def _render_send_form(lang: str, active_tid) -> tuple:
    """Render the send form and return (send_clicked, user_input)."""
    uploader_key = _uploader_key(f"clip_{active_tid or 'new'}")
    st.markdown("<div style='margin-top:6px'></div>", unsafe_allow_html=True)
    with st.form(key=f"qform_{st.session_state.input_key}", clear_on_submit=False):
        _render_uploader_styles()
        col_text, col_side = st.columns([5.2, 1.35])
        with col_text:
            user_input = st.text_area(
                "msg", height=130,
                placeholder=t("input_placeholder", lang=lang),
                label_visibility="collapsed",
                key=f"chat_msg_input_{st.session_state.input_key}",
            )
            label_send = t("btn_reply", lang=lang) if active_tid else t("btn_send", lang=lang)
            send_clicked = st.form_submit_button(label_send, type="primary", use_container_width=False)
        with col_side:
            uploaded = st.file_uploader(
                t("file_uploader_label", lang=lang), type=get_file_uploader_types(),
                key=uploader_key, label_visibility="collapsed")
            st.caption(t("file_uploader_types", lang=lang,
                         types=", ".join(get_file_uploader_types())))
            if uploaded:
                _accept_uploaded_file(lang, uploaded)
            _render_form_file_badge(lang)
    return send_clicked, user_input


def _accept_uploaded_file(lang: str, uploaded) -> None:
    """Extract the uploaded file once and keep it in session state."""
    already_saved = (
        st.session_state.get("attached_file_name") == uploaded.name
        and st.session_state.get("attached_file_context")
    )
    if already_saved:
        return
    try:
        extracted = extract_file_content(uploaded)
        ok_tokens, tokens = check_upload_tokens(extracted)
        if not ok_tokens:
            st.error(t("file_too_large_tokens", lang=lang,
                       tokens=tokens, max_tokens=MAX_UPLOAD_TOKENS))
        else:
            st.session_state["attached_file_context"] = extracted
            st.session_state["attached_file_name"] = uploaded.name
    except Exception as e:
        st.error(t("file_error", lang=lang, error=str(e)))


def _render_form_file_badge(lang: str) -> None:
    """Show the attached file inside the form, with its detach button."""
    fname = st.session_state.get("attached_file_name", "")
    fctx = st.session_state.get("attached_file_context", "")
    if not fname:
        return
    size = f"({len(fctx):,} {t('chars_abbr', lang=lang)})"
    st.markdown(
        '<div style="background:#1e3a5f;border-radius:6px;padding:4px 8px;'
        'font-size:0.78rem;color:#7eb8f7;margin-top:4px">\U0001F4CE ' + fname + ' '
        '<span style="color:#aaa">' + size + '</span></div>',
        unsafe_allow_html=True,
    )
    if st.form_submit_button(t("btn_detach", lang=lang), use_container_width=True):
        _detach_file()
        st.rerun()


def _render_token_indicator(lang: str, assistant: dict, sel_id: str,
                            active_tid, file_context: str) -> None:
    """Render the token line under the form."""
    assistant_ctx = load_assistant_files_context(sel_id) if sel_id else ""
    history_text = " ".join(
        m.get("content", "") for m in (load_thread_messages(active_tid) if active_tid else [])
    )
    combined_ctx = combine_nonempty([assistant_ctx, file_context, history_text])
    ctx = check_context(assistant.get("text", ""), "", combined_ctx, assistant, get_services())
    tok_in, tok_out, tok_cache = 0, 0, 0
    if active_tid:
        tok_in, tok_out, tok_cache = sum_thread_tokens(load_thread_messages(active_tid))
    line = format_token_line(ctx["total_tokens"], tok_in, tok_out, tokens_cache=tok_cache, lang=lang)
    st.markdown('<div style="font-size:0.75rem;color:#555;margin-top:6px">' + line + '</div>',
                unsafe_allow_html=True)


def _handle_send(lang: str, assistant: dict, sel_id: str, sel_name: str,
                 active_tid, user_input: str) -> None:
    """Validate the message, call the model and persist the exchange."""
    file_context = st.session_state.get("attached_file_context", "")
    file_name = st.session_state.get("attached_file_name", "")

    if not user_input.strip() and not file_context:
        st.warning(t("warn_empty", lang=lang))
        st.stop()

    if not _context_is_acceptable(lang, assistant, sel_id, active_tid, user_input, file_context):
        return

    history_msgs = messages_to_api_history(load_thread_messages(active_tid) if active_tid else [])
    last_tokens = None

    def _on_tokens(tokens):
        nonlocal last_tokens
        last_tokens = {"in": tokens["in"], "out": tokens["out"],
                       "cache": tokens.get("cache", 0) or 0}

    # Assistants with a native rag_search function tool run the function-calling
    # loop inside api_layer/assistant_tools; this page only shows the spinner.
    with st.spinner(t("processing", lang=lang)):
        try:
            result = send_request(
                user_input, assistant, file_context,
                history=history_msgs, lang=lang,
                usage_callback=_on_tokens,
            )
        except APIError as e:
            st.error(api_error_message(e, lang=lang))
            st.stop()

    if not active_tid:
        active_tid = create_thread(sel_id, sel_name)
        st.session_state.active_thread_id = active_tid

    if file_context and file_name:
        save_thread_file(active_tid, file_name, file_context)

    append_thread_message(active_tid, "user", user_input,
                          file_name=file_name, file_chars=len(file_context))
    append_thread_message(active_tid, "assistant", result, tokens=last_tokens)
    st.session_state["attached_file_context"] = ""
    st.session_state["attached_file_name"] = ""
    st.session_state.input_key += 1
    st.rerun()


def _context_is_acceptable(lang: str, assistant: dict, sel_id: str,
                           active_tid, user_input: str, file_context: str) -> bool:
    """Check the context budget and offer a forced send when it is exceeded."""
    assistant_ctx = load_assistant_files_context(sel_id)
    hist_ctx = " ".join(m["content"] for m in (load_thread_messages(active_tid) if active_tid else []))
    combined_ctx = combine_nonempty([assistant_ctx, file_context, hist_ctx])
    ctx = check_context(assistant.get("text", ""), user_input, combined_ctx,
                        assistant, get_services())
    if ctx["ok"]:
        st.session_state.force_send = False
        return True
    st.error(t("ctx_warning_title", lang=lang))
    st.warning(t("ctx_warning_body", lang=lang,
                 tokens=ctx["total_tokens"], model=assistant["model"],
                 limit=ctx["limit"], excess=ctx["excess_chars"]))
    if st.session_state.get("force_send"):
        st.session_state.force_send = False
        return True
    if st.button(t("btn_force_send", lang=lang), key="force_send_btn"):
        st.session_state.force_send = True
        st.rerun()
    st.stop()
    return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
