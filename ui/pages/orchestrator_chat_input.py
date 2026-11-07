# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_chat_input - Chat input: toolbar, submission, attachments and token line.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""

from core.files import (
    MAX_THREAD_FILE_BYTES,
    build_saved_files_registry,
    build_thread_files_notice,
)
from core.fs import combine_nonempty
from core.render import (
    clipboard_button,
    format_token_line,
)
from core.threads_devagent import (
    create_devagent_thread,
    list_thread_files,
    save_thread_file_data,
)
from dev_agent import (
    workspace_binding as wb,
)
from dev_agent import (
    workspace_tools as wt,
)
from dev_agent.agent_loop import (
    AgentLoopState,
    apply_economy_cache,
    build_economy_context,
)
from ui.pages.orchestrator_flags import toolbar_specs
from ui.pages.orchestrator_folder import (
    _apply_folder_choice,
    _render_folder_picker,
)
from ui.pages.orchestrator_loop import _chat_toolbar_pref_changed
from ui.pages.orchestrator_shared import late, st_proxy
from ui.pages.orchestrator_text import (
    _chat_toolbar_widget_key,
    _load_attachments_manifest,
    _split_chat_prompt,
)

st = st_proxy()
_do_step = late("_do_step")
_reset_dialog = late("_reset_dialog")
_set_ss = late("_set_ss")
_ss = late("_ss")
build_assistant_dicts = late("build_assistant_dicts")
check_context = late("check_context")
get_economy_config = late("get_economy_config")
get_orchestrator = late("get_orchestrator")
get_services = late("get_services")
sum_thread_tokens = late("sum_thread_tokens")
t = late("t")


def _render_chat_toolbar(slug: str, lang: str) -> None:
    """Render the single chat toolbar (web search / sandbox / safety /
    injection / settings / new dialog / copy URL).

    The toolbar is rendered ONCE at the top of the chat and stays on screen
    while the history scrolls (the host container is sticky - see
    ``_render_chat_tab``).  Checkbox widget keys are the same canonical
    session slots used by the agent loop, so there is a single source of
    truth for each preference.
    """
    specs = toolbar_specs(t, lang)
    # One column per checkbox, then the three action buttons.
    cols = st.columns([1] * len(specs) + [0.8, 1, 1])
    for col, (pref, label, help_text) in zip(cols, specs):
        with col:
            st.checkbox(label, key=_chat_toolbar_widget_key(slug, pref),
                        help=help_text, on_change=_chat_toolbar_pref_changed,
                        args=(slug, pref))

    settings_col, reset_col, copy_col = cols[len(specs):]
    with settings_col:
        if st.button("⚙️ " + t("nav_settings", lang=lang),
                     key=f"orch_settings_{slug}",
                     use_container_width=True, type="secondary"):
            st.session_state["current_page"] = f"orchestrator_settings:{slug}"
            st.rerun()
    with reset_col:
        if st.button(t("orch_new_dialog", lang=lang),
                     key=f"orch_reset_{slug}",
                     use_container_width=True, type="secondary"):
            _reset_dialog(slug)
            st.rerun()
    with copy_col:
        url_params = {"orchestrator": slug}
        tid = _ss(slug, "thread_id")
        if tid:
            url_params["thread"] = str(tid)
        clipboard_button(
            text="", key=f"orch_cp_url_{slug}",
            label=t("orch_copy_url", lang=lang),
            copy_url_params=url_params,
        )


def _token_line_cache_key(slug: str, history: list, economy_enabled: bool,
                          loop_state, prompt_text: str, file_text: str,
                          strong: dict, services_names: tuple) -> str:
    """Build a stable cache key capturing every input of the token line.

    The rendered indicator depends on the full history (economy window, per-
    thread token sums, message classification), the system prompt, the
    attached-file texts, the strong service/model, the economy config, the
    live loop state and the UI language. Hashing a canonical repr of all of
    them is far cheaper
    than recomputing context/token estimates on every rerun, and still
    recomputes whenever any real input changes.
    """
    import hashlib

    econ = ()
    if economy_enabled:
        cfg = get_economy_config(slug) or {}
        econ = (
            bool(cfg.get("cache_enabled", False)),
            int(cfg.get("cache_multiplier", 1) or 1),
            int(cfg.get("tail_messages", 0) or 0),
        )
    state = ()
    if loop_state is not None:
        state = (
            getattr(loop_state, "phase", None),
            getattr(loop_state, "economy_anchor", None),
            getattr(loop_state, "economy_tail_messages", None),
        )
    eco_cache = _ss(slug, "economy_cache")
    if isinstance(eco_cache, dict):
        state += (
            eco_cache.get("economy_anchor"),
            eco_cache.get("economy_tail_messages"),
        )
    msg_parts = [
        (m.get("role"), m.get("content", ""), m.get("ts", ""),
         bool(m.get("hidden")))
        for m in history
    ]
    # The rendered labels and the economy suffix depend on the UI language.
    lang = st.session_state.get("ui_lang")
    payload = repr((
        _ss(slug, "thread_id"), economy_enabled, econ, state,
        prompt_text, file_text, strong, services_names, msg_parts, lang,
    ))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _render_token_line_impl(slug: str, history: list, economy_enabled: bool,
                       loop_state, lang: str = None) -> None:
    """Render the single-line token/economy indicator, cached per state.

    Recomputing the context estimate and the economy window on every rerun is
    the main cost of the chat page on long dialogs. The finished HTML is
    stored in session_state as ``orch_<slug>_token_line_cache`` (dict with
    ``key`` + ``html``) and reused until one of the inputs changes; when the
    history/economy config/loop phase/prompt/attachments/service changes, the
    line is recomputed and the cache refreshed.
    """
    _orch = get_orchestrator(slug)
    _full_sys = _orch.get("prompt_text", "") if _orch else ""
    # Attachments now travel with the chat input submission and are persisted
    # immediately, so there is no pending attachment text between renders.
    _file_text = ""
    _strong_assistant, _ = build_assistant_dicts(slug)
    _services_names = tuple(sorted(get_services().keys()))

    cache_key = _token_line_cache_key(
        slug, history, economy_enabled, loop_state,
        _full_sys, _file_text, _strong_assistant, _services_names,
    )
    _cached = _ss(slug, "token_line_cache")
    if (isinstance(_cached, dict) and _cached.get("key") == cache_key
            and _cached.get("html")):
        st.markdown(_cached["html"], unsafe_allow_html=True)
        return

    _effective_history, _economy_meta = _economy_window_info(
        slug, history, economy_enabled, loop_state, lang,
    )

    _effective_text = " ".join(
        m.get("content", "") for m in _effective_history if not m.get("hidden")
    )
    _ctx = check_context(
        _full_sys, "", combine_nonempty([_effective_text, _file_text]),
        _strong_assistant, get_services(),
    )
    _current_tokens = _ctx["total_tokens"]
    _tok_in, _tok_out, _tok_cache = sum_thread_tokens(history)
    _html = (
        f'<div style="font-size:0.75rem;color:#555;margin-top:6px">'
        f'{format_token_line(_current_tokens, _tok_in, _tok_out, _economy_meta, tokens_cache=_tok_cache, lang=lang)}</div>'
    )
    _set_ss(slug, "token_line_cache", {"key": cache_key, "html": _html})
    st.markdown(_html, unsafe_allow_html=True)


def _economy_window_info(slug: str, history: list, economy_enabled: bool,
                         loop_state, lang: str = None) -> tuple:
    """Return (history actually sent, economy badge text) for the token line.

    When economy mode is off or the dialog is empty the history is returned
    untouched and the badge is empty.  Otherwise the same window that the next
    request will carry is rebuilt - reusing the live loop-state anchor when one
    exists, so the indicator shows the gradual growth of the cache-friendly
    window instead of always the bare tail.
    """
    if not (economy_enabled and history):
        return history, ""

    _economy_config = get_economy_config(slug)
    _temp_state = AgentLoopState(
        task="",
        history=list(history),
        economy_mode=True,
        thread_id=_ss(slug, "thread_id") or "",
        economy_cache_enabled=bool(_economy_config.get("cache_enabled", False)),
        economy_cache_multiplier=int(_economy_config.get("cache_multiplier", 1)),
    )
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
    _econ_word = t("token_line_economy", lang=lang)
    _msgs_word = t("token_line_msgs", lang=lang)
    _economy_meta = f" 💡 {_econ_word} ({_sent_total}/{_raw_total} {_msgs_word})"
    return _effective_history, _economy_meta


def _handle_chat_submission(slug: str, lang: str, orch) -> None:
    """Render the input row and process a submitted message."""
    with st.bottom:
        _render_folder_picker(slug, lang)
        _chat_prompt = st.chat_input(
            t("orch_input_placeholder", lang=lang),
            accept_file="multiple",
        )
        _render_chat_toolbar(slug, lang)

    _prompt_text, _prompt_files = _split_chat_prompt(_chat_prompt)

    if not (_prompt_text.strip() or _prompt_files):
        return

    # Files arrive together with the text from the single chat input
    # (accept_file="multiple"); raw bytes are persisted UNCHANGED and
    # the agent decides later how to extract them.
    _msg_text = _build_submission_text(_prompt_text, _prompt_files, lang)
    # A folder picked in "Выбор папки" is applied HERE, before the
    # model is called: the workspace switch happens automatically
    # (set_workspace + thread binding), so the agent never spends a
    # step doing it itself. No-op once the selector snapped to "Авто".
    _apply_folder_choice(slug)
    _ensure_thread(slug, orch, _msg_text)
    ctx_text, name = _collect_attachment_context(slug, _prompt_files, lang)
    _set_ss(slug, "file_ctx", ctx_text)
    _set_ss(slug, "file_name", name)
    _set_ss(slug, "user_message", _msg_text)
    _set_ss(slug, "scroll_bottom", True)
    _do_step(slug=slug, lang=lang)
    st.rerun()


def _build_submission_text(prompt_text: str, prompt_files: list, lang: str) -> str:
    """Return the task line of a submission.

    A files-only submission still needs one: the agent loop ignores an empty
    user_message, so the names of the attached files become the task line.
    """
    msg_text = prompt_text.strip()
    if not msg_text and prompt_files:
        names = ", ".join(
            getattr(f, "name", "") for f in prompt_files
            if getattr(f, "name", "")
        )
        msg_text = t("orch_attach_only_message", lang=lang, names=names).strip()
    return msg_text


def _ensure_thread(slug: str, orch, msg_text: str) -> None:
    """Create and bind the dialog thread of the first message."""
    if _ss(slug, "thread_id"):
        return
    orch_obj = orch or {}
    ws_info = wt.current_workspace()
    # The empty state ("no folder selected") is persisted as NO
    # folder: the neutral root must never be recorded as if the
    # user had chosen it.
    ws_selected = bool(ws_info.get("workspace_selected", True))
    tid = create_devagent_thread(
        title=msg_text,
        orchestrator_slug=slug,
        orchestrator_name=orch_obj.get("name", slug),
        workspace=(ws_info.get("root", "") or None) if ws_selected else None,
        target_file=((ws_info.get("target_file", "") or None)
                     if (ws_selected and ws_info.get("single_file_mode")) else None),
    )
    _set_ss(slug, "thread_id", tid)
    _set_ss(slug, "saved_msg_count", 0)
    # Bind the brand-new thread to the folder chosen for THIS
    # dialog, so the thread binding is authoritative from the very
    # first message instead of waiting for the next render to
    # lazily snapshot the live config.
    if ws_selected and ws_info.get("root"):
        try:
            wb.register_thread(
                tid,
                workspace=ws_info.get("root", ""),
                target_file=((ws_info.get("target_file") or None)
                             if ws_info.get("single_file_mode") else None),
            )
        except Exception:
            pass


def _collect_attachment_context(slug: str, prompt_files: list, lang: str) -> tuple:
    """Persist the submitted attachments and build the notice for the agent.

    Every file is stored as raw bytes under history/<tid>/files - the agent
    decides later how to extract it. The returned text carries the notice of
    this upload plus the registry of the dialog's earlier workspace files, so
    the agent still knows them when economy mode dropped the first turns.
    """
    ctx_text, name = "", ""
    tid = _ss(slug, "thread_id")
    if not tid:
        return ctx_text, name
    ws_root = wt.current_workspace().get("root", "")
    saved_names = []
    for uf in prompt_files:
        try:
            data = uf.read()
        except Exception:
            continue
        if len(data) > MAX_THREAD_FILE_BYTES:
            st.error(t("orch_file_too_large_bytes", lang=lang,
                       max_bytes=MAX_THREAD_FILE_BYTES))
            continue
        try:
            save_thread_file_data(tid, getattr(uf, "name", ""), bytes(data))
            saved_names.append(getattr(uf, "name", ""))
        except Exception:
            continue
    if saved_names:
        # Tell the agent exactly which files belong to this dialog.
        # The notice lists names, absolute paths, sizes and the
        # text/binary classification - without extracting content.
        ctx_text = build_thread_files_notice(list_thread_files(tid))
        name = ", ".join(n for n in saved_names if n)
    # Re-announce ALL legacy workspace attachments on every message
    # so the agent still knows them when economy mode has dropped
    # the beginning of the conversation.
    registry = build_saved_files_registry(_load_attachments_manifest(ws_root, tid))
    if registry:
        ctx_text = f"{ctx_text}\n\n{registry}" if ctx_text else registry
    return ctx_text, name


def _services_with_web_search() -> dict:
    all_svc = get_services()
    result = {}
    for name, svc in all_svc.items():
        tools = svc.get("tools_options", [])
        for tool in tools:
            if tool.get("key") == "web_search":
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
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
