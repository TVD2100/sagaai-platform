"""
ui.pages.chat - page_run_query: chat interface for sending queries to AI assistants.
Requires Streamlit.

When st.session_state.selected_assistant_id is set (e.g., from the sidebar),
that assistant is used automatically without showing the selector. The legacy
session key selected_skill_id is still read as a fallback.

The send form keeps its values across reruns (clear_on_submit=False) so that
an API error never destroys the typed message or the attached file: the user
can immediately press Send again without retyping anything.
"""
import streamlit as st

from core.i18n import t
from core.assistants import (
    load_assistants_index, get_assistant_by_id, list_assistant_files,
    load_assistant_files_context,
)
from core.threads import (
    load_thread_meta, load_thread_messages,
    create_thread, append_thread_message,
    messages_to_api_history, save_thread_file,
    sum_thread_tokens,
)
from core.files import (
    get_file_uploader_types, extract_file_content, check_context,
    check_upload_tokens, MAX_UPLOAD_TOKENS,
)
from core.services import get_services
from core.fs import combine_nonempty
from core.api_layer import send_request
from core.api_errors import APIError, api_error_message
from core.render import _md_to_txt, clipboard_button, format_token_line, format_ts_label
from core.recent_assistants import record_assistant_use


def _get_preselected(assistant_id_by_name: dict):
    """Resolve the preselected assistant id with legacy-key fallback."""
    val = st.session_state.get("selected_assistant_id") or st.session_state.get("selected_skill_id")
    return val if val in assistant_id_by_name else None


def _uploader_counter() -> int:
    """Return the upload-reset counter, bumped on every file detach."""
    return int(st.session_state.get("upload_counter", 0) or 0)


def _uploader_key(base: str) -> str:
    """Return the file-uploader widget key for the chat send form.

    The key embeds a counter that is bumped on every detach.  This makes
    Streamlit recreate the uploader widget so that a previously uploaded
    file is dropped instead of being silently re-attached on the next
    rerun (a file_uploader otherwise keeps the file in its widget state).
    """
    return f"{base}_{_uploader_counter()}"


def _detach_file() -> None:
    """Clear the attached file and invalidate the uploader widget."""
    st.session_state["attached_file_context"] = ""
    st.session_state["attached_file_name"]    = ""
    st.session_state["upload_counter"] = _uploader_counter() + 1


def page_run_query():
    """Chat page - assistant selector, message history, send form."""
    index = load_assistants_index()
    lang  = st.session_state.get("ui_lang")

    if not index:
        st.title(f"💬 {t('nav_run', lang=lang)}")
        st.warning(t("no_skills", lang=lang))
        return

    active_tid = st.session_state.get("active_thread_id")

    # ── Assistant resolution ───────────────────────────────────────────────────
    # Priority: preselected_assistant_id from session > active thread's assistant > first
    preselected_id = _get_preselected({p["id"]: p for p in index})
    id_from_name = {p["id"]: p for p in index}

    if preselected_id and preselected_id in id_from_name:
        sel_id   = preselected_id
        sel_name = id_from_name[preselected_id]["name"]
        # Keep the selection in session state: it is the user's active choice.
        # Clearing it here caused subsequent reruns to fall back to the first
        # assistant in the index instead of the one the user picked.
    elif active_tid:
        meta     = load_thread_meta(active_tid)
        sel_id   = meta.get("assistant_id") or meta.get("skill_id") or list(id_from_name.keys())[0]
        sel_name = meta.get("assistant_name") or meta.get("skill_name") or list(id_from_name.values())[0]["name"]
    else:
        # Show selector only when no preselected assistant
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
                files_badge = f" · 📎 {len(sf)}" if sf else ""
                st.caption(
                    f"🔧 {assistant_tmp.get('service','?')} › {assistant_tmp['model']} "
                    f"| T={assistant_tmp['temperature']}{files_badge}"
                )

    assistant = get_assistant_by_id(sel_id)
    if not assistant:
        st.error(t("skill_not_found", lang=lang))
        return

    # ── Saved model availability warning ────────────────────────────────────
    services = get_services()
    saved_svc = assistant.get("service", "")
    if saved_svc and saved_svc not in services:
        st.warning(t("assistant_service_unavailable", lang=lang, service=saved_svc))
    elif saved_svc in services:
        saved_models = [
            m.get("id") if isinstance(m, dict) else m
            for m in services.get(saved_svc, {}).get("models", [])
        ]
        if assistant.get("model") and assistant["model"] not in saved_models:
            st.warning(t("assistant_model_unavailable", lang=lang,
                         model=assistant["model"], service=saved_svc))

    # Record the assistant as recently used.
    record_assistant_use(sel_id)

    # ── header ────────────────────────────────────────────────────────────────
    if active_tid:
        meta       = load_thread_meta(active_tid)
        title_text = meta.get("title") or t("untitled", lang=lang)

        st.markdown(
            f'''<h2 style="margin:0;padding-bottom:2px">💬 {title_text[:55]}</h2>
            <p style="color:#888;font-size:0.8rem;margin:0">
            🔧 {meta.get("assistant_name","") or meta.get("skill_name","")} &nbsp;|&nbsp; 📅 {meta.get("created_at","")[:10]}
            </p>''',
            unsafe_allow_html=True,
        )
    else:
        st.title(f"💬 {t('nav_run', lang=lang)}")

    st.markdown("---")

    # ── Quick actions: settings / new dialog (mirrors orchestrator UX) ──────
    col_set, col_new = st.columns([1, 1])
    with col_set:
        if st.button(
            "⚙️ " + t("nav_settings", lang=lang),
            key=f"chat_settings_{sel_id}",
            use_container_width=True,
        ):
            st.session_state["last_active_entity_type"] = "assistant"
            st.session_state["last_active_entity_id"]   = sel_id
            st.session_state["selected_assistant_id"]   = sel_id
            st.session_state["selected_skill_id"]       = sel_id
            st.session_state["show_assistant_form"]     = True
            st.session_state["show_skill_form"]         = True
            st.session_state["edit_assistant_id"]       = sel_id
            st.session_state["edit_skill_id"]           = sel_id
            st.session_state["current_page"]            = "skills"
            st.rerun()
    with col_new:
        if st.button(
            "🔄 " + t("sidebar_new_dialog", lang=lang),
            key=f"chat_new_dialog_{sel_id}",
            use_container_width=True,
        ):
            st.session_state["active_thread_id"]      = None
            st.session_state["attached_file_context"] = ""
            st.session_state["attached_file_name"]    = ""
            st.session_state["force_send"]            = False
            st.session_state["selected_assistant_id"] = sel_id
            st.session_state["selected_skill_id"]     = sel_id
            st.session_state["current_page"]          = "run"
            st.rerun()

    # Show current assistant info when not using selector (preselected or active thread)
    if preselected_id and preselected_id in id_from_name:
        assistant_tmp = get_assistant_by_id(sel_id)
        if assistant_tmp:
            with st.expander(f"🔧 {sel_name}", expanded=False):
                st.caption(f"{assistant_tmp.get('service','?')} › {assistant_tmp['model']} | T={assistant_tmp['temperature']}")
                sf = list_assistant_files(sel_id)
                if sf:
                    st.caption(t("skill_files_caption", lang=lang, files=", ".join(
                        fn[:-4] if fn.endswith(".txt") else fn for fn in sf
                    )))
    elif active_tid:
        with st.expander(f"🔧 {sel_name}", expanded=False):
            assistant_tmp = get_assistant_by_id(sel_id)
            if assistant_tmp:
                st.caption(f"{assistant_tmp.get('service','?')} › {assistant_tmp['model']} | T={assistant_tmp['temperature']}")
                sf = list_assistant_files(sel_id)
                if sf:
                    st.caption(t("skill_files_caption", lang=lang, files=", ".join(
                        fn[:-4] if fn.endswith(".txt") else fn for fn in sf
                    )))

    # ── message history ────────────────────────────────────────────────────────
    if active_tid:
        messages = load_thread_messages(active_tid)
        for msg_idx, msg in enumerate(messages):
            role    = msg["role"]
            content = msg["content"]
            ts_raw  = msg.get("ts", "")
            ts_display = format_ts_label(ts_raw)
            fname   = msg.get("file_name", "")
            if role == "user":
                with st.chat_message("user"):
                    if fname:
                        st.caption(f"📎 {fname}")
                    st.markdown(content)
                    if ts_display:
                        st.caption(f"🕐 {ts_display}")
            else:
                with st.chat_message("assistant"):
                    st.markdown(content)
                    _dl_ts    = ts_display.replace(":", "-") if ts_display else str(msg_idx)
                    _dl_fname = f"response_{_dl_ts}_{msg_idx}"
                    c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 5])
                    with c1:
                        st.download_button(
                            t("chat_dl_md", lang=lang), data=content.encode("utf-8"),
                            file_name=f"{_dl_fname}.md", mime="text/markdown",
                            key=f"dl_md_{msg_idx}_{active_tid}", use_container_width=True,
                        )
                    with c2:
                        st.download_button(
                            t("chat_dl_txt", lang=lang), data=_md_to_txt(content).encode("utf-8"),
                            file_name=f"{_dl_fname}.txt", mime="text/plain",
                            key=f"dl_txt_{msg_idx}_{active_tid}", use_container_width=True,
                        )
                    with c3:
                        clipboard_button(content, key=f"cp_md_{msg_idx}_{active_tid}", label=t("chat_cp_md", lang=lang))
                    with c4:
                        clipboard_button(_md_to_txt(content), key=f"cp_txt_{msg_idx}_{active_tid}", label=t("chat_cp_txt", lang=lang))
                    with c5:
                        if ts_display:
                            st.caption(f"🕐 {ts_display}")

    # ── send form ──────────────────────────────────────────────────────────────
    uploader_key = _uploader_key(f"clip_{active_tid or 'new'}")
    file_context = st.session_state.get("attached_file_context", "")
    file_name    = st.session_state.get("attached_file_name",    "")

    if file_name:
        c_b, c_r = st.columns([10, 1])
        with c_b:
            badge_html = (
                f'<div style="background:#1e3a5f;border-radius:6px;padding:4px 10px;'
                f'font-size:0.82rem;color:#7eb8f7;margin-bottom:6px;display:inline-block">'
                f'📎 {file_name} <span style="color:#aaa">({len(file_context):,} {t("chars_abbr", lang=lang)})</span></div>'
            )
            st.markdown(badge_html, unsafe_allow_html=True)
        with c_r:
            if st.button("✕", key="remove_file", help=t("detach_file_help", lang=lang)):
                _detach_file()
                st.rerun()

    st.markdown("<div style='margin-top:6px'></div>", unsafe_allow_html=True)
    with st.form(key=f"qform_{st.session_state.input_key}", clear_on_submit=False):
        st.markdown("""
        <style>
        div[data-testid="stFileUploader"] section[data-testid="stFileUploaderDropzone"]{
            padding:0.12rem 0.38rem !important;
            min-height:2.05rem !important;
            border-radius:0.5rem !important;
        }
        div[data-testid="stFileUploader"] button{
            padding:0.1rem 0.45rem !important;
            font-size:0.80rem !important;
            min-height:1.9rem !important;
        }
        div[data-testid="stFileUploader"] small{display:none !important;}
        div[data-testid="stFileUploader"] label{
            margin-bottom:0 !important;
            font-size:0 !important;
            height:0 !important;
            min-height:0 !important;
        }
        </style>
        """, unsafe_allow_html=True)
        col_text, col_side = st.columns([5.2, 1.35])
        with col_text:
            user_input   = st.text_area(
                "msg", height=130,
                placeholder=t("input_placeholder", lang=lang),
                label_visibility="collapsed",
                key=f"chat_msg_input_{st.session_state.input_key}",
            )
            label_send   = t("btn_reply", lang=lang) if active_tid else t("btn_send", lang=lang)
            send_clicked = st.form_submit_button(label_send, type="primary", use_container_width=False)
        with col_side:
            uploaded = st.file_uploader(
                "",
                type=get_file_uploader_types(),
                key=uploader_key,
                label_visibility="collapsed",
            )
            st.caption(t("file_uploader_types", lang=lang,
                         types=", ".join(get_file_uploader_types())))
            if uploaded:
                already_saved = (
                    st.session_state.get("attached_file_name") == uploaded.name
                    and st.session_state.get("attached_file_context")
                )
                if not already_saved:
                    try:
                        extracted = extract_file_content(uploaded)
                        ok_tokens, tokens = check_upload_tokens(extracted)
                        if not ok_tokens:
                            st.error(t("file_too_large_tokens", lang=lang,
                                       tokens=tokens, max_tokens=MAX_UPLOAD_TOKENS))
                        else:
                            st.session_state["attached_file_context"] = extracted
                            st.session_state["attached_file_name"]    = uploaded.name
                    except Exception as e:
                        st.error(t("file_error", lang=lang, error=str(e)))

            _fname = st.session_state.get("attached_file_name", "")
            _fctx  = st.session_state.get("attached_file_context", "")
            if _fname:
                st.markdown(
                    f'<div style="background:#1e3a5f;border-radius:6px;padding:4px 8px;'
                    f'font-size:0.78rem;color:#7eb8f7;margin-top:4px">📎 {_fname} '
                    f'<span style="color:#aaa">({len(_fctx):,} {t("chars_abbr", lang=lang)})</span></div>',
                    unsafe_allow_html=True,
                )
                if st.form_submit_button(t("btn_detach", lang=lang), use_container_width=True):
                    _detach_file()
                    st.rerun()

    # ── Token usage indicator ──────────────────────────────────────────────────
    sys_text = assistant.get("text", "")
    assistant_ctx = load_assistant_files_context(sel_id) if sel_id else ""
    history_text = " ".join(
        m.get("content", "") for m in (load_thread_messages(active_tid) if active_tid else [])
    )
    combined_ctx = combine_nonempty([assistant_ctx, file_context, history_text])
    ctx = check_context(sys_text, "", combined_ctx, assistant, get_services())
    current_tokens = ctx["total_tokens"]
    tok_in, tok_out, tok_cache = 0, 0, 0
    if active_tid:
        tok_in, tok_out, tok_cache = sum_thread_tokens(load_thread_messages(active_tid))
    st.markdown(
        f'<div style="font-size:0.75rem;color:#555;margin-top:6px">'
        f'{format_token_line(current_tokens, tok_in, tok_out, tokens_cache=tok_cache)}</div>',
        unsafe_allow_html=True,
    )

    # ── send handling ──────────────────────────────────────────────────────────
    if send_clicked:
        file_context = st.session_state.get("attached_file_context", "")
        file_name    = st.session_state.get("attached_file_name",    "")

        if not user_input.strip() and not file_context:
            st.warning(t("warn_empty", lang=lang))
            st.stop()

        assistant_ctx = load_assistant_files_context(sel_id)
        hist_ctx     = " ".join(
            m["content"] for m in (load_thread_messages(active_tid) if active_tid else [])
        )
        combined_ctx = combine_nonempty([assistant_ctx, file_context, hist_ctx])
        ctx = check_context(assistant.get("text", ""), user_input, combined_ctx, assistant, get_services())
        if not ctx["ok"]:
            st.error(t("ctx_warning_title", lang=lang))
            st.warning(t("ctx_warning_body", lang=lang,
                         tokens=ctx["total_tokens"], model=assistant["model"],
                         limit=ctx["limit"], excess=ctx["excess_chars"]))
            if not st.session_state.get("force_send"):
                if st.button(t("btn_force_send", lang=lang), key="force_send_btn"):
                    st.session_state.force_send = True
                    st.rerun()
                st.stop()
            else:
                st.session_state.force_send = False
        else:
            st.session_state.force_send = False

        # The thread, the thread file and the user message are persisted only
        # AFTER the model has answered.  On an API error nothing is stored or
        # cleared, so the user can simply press Send again without retyping
        # or re-attaching anything.
        history_msgs = messages_to_api_history(
            load_thread_messages(active_tid) if active_tid else []
        )

        # ── Capture token usage via callback ────────────────────────────────────
        last_tokens = None
        def _on_tokens(t):
            nonlocal last_tokens
            last_tokens = {"in": t["in"], "out": t["out"], "cache": t.get("cache", 0) or 0}

        # ── Send request (unified APIError contract) ───────────────────────────
        # Assistants with a native ``rag_search`` function tool run the
        # function-calling loop inside api_layer/assistant_tools:
        # model -> function_call -> local rag_search -> function_call_output
        # -> final answer. chat.py only shows the spinner; send_request
        # executes the whole loop synchronously.
        with st.spinner(t("processing", lang=lang)):
            try:
                result = send_request(
                    user_input, assistant, file_context,
                    history=history_msgs, lang=lang,
                    usage_callback=_on_tokens,
                )
            except APIError as e:
                # Keep the typed message and the attached file so the user
                # can simply retry without retyping or re-attaching.
                st.error(api_error_message(e, lang=lang))
                st.stop()

        if not active_tid:
            active_tid = create_thread(sel_id, sel_name)
            st.session_state.active_thread_id = active_tid

        if file_context and file_name:
            save_thread_file(active_tid, file_name, file_context)

        append_thread_message(active_tid, "user", user_input,
                              file_name=file_name, file_chars=len(file_context))
        append_thread_message(active_tid, "assistant", result,
                              tokens=last_tokens)
        st.session_state["attached_file_context"] = ""
        st.session_state["attached_file_name"]    = ""
        st.session_state.input_key += 1
        st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
