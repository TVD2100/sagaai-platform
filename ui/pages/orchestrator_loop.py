# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_loop - Agent step machine: loop state, dialog tree and subagent runs.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""
import os
from typing import Any, Callable, Dict, List

from core.api_layer import send_request
from core.config import load_config
from core.orchestrator_step import persist_step_messages
from core.threads_devagent import (
    load_thread_messages,
    load_thread_meta,
)
from dev_agent import (
    config as dagent_config,
)
from dev_agent import (
    workspace_binding as wb,
)
from dev_agent import (
    workspace_tools as wt,
)
from dev_agent.agent_loop import (
    AgentLoopState,
    carry_over_economy_cache,
    step_agent_loop,
)
from dev_agent.universal_agent import UniversalDevAgent
from storage.models import DEFAULT_MAX_STEPS
from ui.pages.orchestrator_flags import (
    effective_sandbox_profile,
    env_isolation,
    read_protection_flags,
)
from ui.pages.orchestrator_shared import late, st_proxy
from ui.pages.orchestrator_state import (
    CHAT_PAGE_SIZE,
    ECONOMY_MODE_ALWAYS_ON,
    _load_economy_cache,
    _pop_ss,
    _save_chat_pref,
    _save_economy_cache,
)
from ui.pages.orchestrator_text import (
    _attach_events,
    _persist_thread_workspace_meta,
    _reindex_history,
    _release_step_resources,
    _select_live_runs,
    _select_waiting_runs,
    _sk,
    _sync_safety_mode,
)

st = st_proxy()
_make_dispatcher = late("_make_dispatcher")
_make_send_adapter = late("_make_send_adapter")
_reset_dialog = late("_reset_dialog")
_set_ss = late("_set_ss")
_ss = late("_ss")
_thread_folder = late("_thread_folder")
build_assistant_dicts = late("build_assistant_dicts")
get_economy_config = late("get_economy_config")
get_fetch_config = late("get_fetch_config")
get_orchestrator = late("get_orchestrator")
get_services = late("get_services")
get_web_search_config = late("get_web_search_config")
t = late("t")


def _assistant_dicts(slug: str):
    """Build the assistant dicts with THIS dialog's workspace.

    The workspace is needed to expand a project-relative MEMORY_DIR override
    for the working-memory block. Older builds (and tests that stub the
    builder) accept the slug alone, so the call degrades gracefully instead
    of breaking the turn.
    """
    workspace = _thread_folder(slug) or ""
    try:
        return build_assistant_dicts(slug, workspace)
    except TypeError:
        return build_assistant_dicts(slug)


def _make_send_adapter_impl(lang: str, slug: str) -> Callable:
    """Return a send_request wrapper using the orchestrator's main model."""
    # The workspace of THIS dialog is passed along: the working-memory block
    # reports a project-relative MEMORY_DIR override, and a neighbouring
    # dialog's folder must never leak into this prompt.
    strong_assistant, _ = _assistant_dicts(slug)
    if not strong_assistant.get("service") or not strong_assistant.get("model"):
        orch = get_orchestrator(slug)
        cfg = orch.get("config", {}) if orch else {}
        strong_assistant = {
            "text": orch.get("prompt_text", "") if orch else "",
            "service": cfg.get("strong_service", "DeepSeek"),
            "model": cfg.get("strong_model", "deepseek-flash"),
            "temperature": float(cfg.get("strong_temperature", 0.2) or 0.2),
        }

    def _adapter(user_message: str, system: str = "", history: List[Dict[str, Any]] = None) -> str:
        assistant = dict(strong_assistant)
        if system:
            assistant["text"] = system
        injection_protection = _ss(slug, "injection_protection")
        if injection_protection is None:
            injection_protection = True
        return send_request(
            user_message=user_message,
            assistant=assistant,
            file_context="",
            history=history or [],
            lang=lang,
            enable_injection_protection=bool(injection_protection),
        )

    return _adapter


def _make_dispatcher_impl(slug: str) -> UniversalDevAgent:
    """Return or create the UniversalDevAgent for this orchestrator."""
    dk = _sk(slug, "dispatcher")
    dispatcher = st.session_state.get(dk)
    if dispatcher is None:
        dispatcher = UniversalDevAgent(workspace=None, target_file=None)
        st.session_state[dk] = dispatcher
    # Re-registering on every render would snapshot the live config of a
    # parallel dialog, so the binding is refreshed only when it changed.
    tid = _ss(slug, "thread_id") or None
    if getattr(dispatcher, "thread_id", None) != tid:
        dispatcher.set_thread_id(tid)
    dispatcher.core._web_search_enabled = _ss(slug, "web_search") or False
    dispatcher.core._web_search_config = get_web_search_config(slug)
    dispatcher.core._url_fetch_config = get_fetch_config(slug)
    safety_mode = _ss(slug, "safety_mode")
    dispatcher.core._safety_enabled = True if safety_mode is None else bool(safety_mode)
    # Read every render: a cached value would outlive a settings change.
    dispatcher.core._sandbox_profile = effective_sandbox_profile(
        get_orchestrator, slug)
    # The access grant decides the child environment, so it has to be read
    # from the settings on every render for the same reason.
    dispatcher.core._env_isolation = env_isolation(get_orchestrator, slug)
    # Load custom orchestrator functions into the dispatcher so the LLM can
    # call them by name as if they were built-in tools.
    dispatcher.attach_orchestrator(slug)
    return dispatcher


def _assistant_has_api_key_impl(svc_name: str) -> bool:
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


def _dialog_tree_root(slug: str) -> str:
    """Return the tree root of this dialog, recovering it after a reload.

    A browser session forgets the root when the page reloads, while the run
    records stay durable and a dialog root is registered under its thread id.
    Without the recovery a child that waits for an answer stays invisible
    until the next step - exactly the hidden stop the waiting line exists to
    prevent.
    """
    root = str(_ss(slug, "active_root_run_id") or "").strip()
    if root:
        return root
    thread_id = str(_ss(slug, "thread_id") or "").strip()
    if not thread_id:
        return ""
    try:
        from core.orchestration.service import get_service
        finder = getattr(get_service(), "roots_for_thread", None)
        roots = finder(thread_id) if callable(finder) else []
    except Exception:
        return ""
    if not roots:
        return ""
    root = str(roots[0] or "").strip()
    if root:
        _set_ss(slug, "active_root_run_id", root)
    return root


def _live_subagent_runs(slug: str) -> list:
    """Return the runs of this dialog's tree that are still running.

    The list is read from the durable registry, so a child started in a
    previous script run is still visible. An empty list means the dialog has
    nothing to poll and the auto-refresh fragment is not rendered at all -
    an idle dialog must not hold a background timer.

    A stored ``running`` claim is not taken at face value: the registry demotes
    a record this process cannot verify to ``failed``, so a child whose thread
    died cannot keep the status line up. The root record of the dialog is never
    a child of itself.
    """
    try:
        from core.orchestration.service import get_service
        root = _dialog_tree_root(slug)
        if not root:
            return []
        runs = get_service().list_runs(root) or []
    except Exception:
        return []
    return _select_live_runs(runs, root)


def _waiting_subagent_runs(slug: str) -> list:
    """Return the child runs of this dialog that wait for an ANSWER.

    A parked run is not working: it holds no pool slot and its thread is gone,
    so the running line would never report it and the user would see an idle
    dialog while a child stands still. Only a question is reported here - a
    run that merely ran out of continuations waits for a RESUME, not for an
    answer, and telling the user to answer it would be wrong.
    """
    try:
        from core.orchestration.service import get_service
        root = _dialog_tree_root(slug)
        if not root:
            return []
        runs = get_service().list_runs(root) or []
    except Exception:
        return []
    return _select_waiting_runs(runs, root)


def _render_subagent_status(slug: str, lang: str) -> None:
    """Render the live-subagent line and keep the dialog refreshed.

    Three rules shape this function. A child that waits for an ANSWER gets its
    own line naming the question - otherwise a parked run looks like an idle
    dialog. The poll fragment exists only while a child is running or waiting:
    rendering it unconditionally would leave every idle dialog polling the
    registry forever.
    """
    waiting = _waiting_subagent_runs(slug)
    if waiting:
        QUESTION_PREVIEW = 160
        for rec in waiting[:3]:
            question = rec.get("question") if isinstance(rec.get("question"), dict) else {}
            text = " ".join(str(question.get("text") or "").split())
            if len(text) > QUESTION_PREVIEW:
                text = text[:QUESTION_PREVIEW - 3] + "..."
            st.caption(t("orch_subagent_waiting", lang=lang,
                         agent=str(rec.get("agent_slug") or "?"),
                         run=str(rec.get("run_id") or "")[:8],
                         question=text or "?"))
        if len(waiting) > 3:
            st.caption(t("orch_subagents_waiting", lang=lang, count=len(waiting)))
    alive = _live_subagent_runs(slug)
    if alive:
        names = ", ".join(sorted({str(r.get("agent_slug") or "?") for r in alive}))
        st.caption(t("orch_subagents_running", lang=lang, count=len(alive), agents=names))
    if not alive and not waiting:
        return

    @st.fragment(run_every=3)
    def _poll_children() -> None:
        """Re-render while children run or wait; stop when both are over."""
        if _live_subagent_runs(slug) or _waiting_subagent_runs(slug):
            return
        # Nothing is running or waiting any more: the notices are already in
        # the mailbox or in the history. Continue the dialog so the parent
        # reads them.
        st.rerun()

    _poll_children()


def _read_step_flags(slug: str) -> tuple:
    """Return ``(economy_mode, web_search, safety_mode, injection_protection)``.

    Each protection owns its slot, so toggling one never changes the other.
    """
    ws = _ss(slug, "web_search")
    safety_mode, injection_protection = read_protection_flags(lambda n: _ss(slug, n))
    return (ECONOMY_MODE_ALWAYS_ON, ws if ws is not None else False,
            safety_mode, injection_protection)


def _new_loop_state(slug: str, prev_state, user_message: str, *,
                    economy_mode: bool, web_search_enabled: bool,
                    safety_mode: bool, injection_protection: bool = True):
    """Build a fresh loop state for a newly submitted user message.

    The economy window anchor must survive loop-state clearing: ``carry_over``
    handles the case when the previous loop state was kept (e.g. awaiting_user)
    and ``_load_economy_cache`` restores the persisted parameters when it was
    cleared by a terminal status.
    """
    strong_assistant, weak_assistant = _assistant_dicts(slug)
    orch = get_orchestrator(slug)
    economy_config = get_economy_config(slug)
    state = AgentLoopState(
        task=user_message,
        max_steps=orch.get("max_steps", DEFAULT_MAX_STEPS) if orch else DEFAULT_MAX_STEPS,
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
        enable_injection_protection=bool(injection_protection),
        # The mailbox of the tree is keyed by its ROOT run, and the
        # registration below uses the same id: the drain in the loop and
        # the notice publisher in the worker meet in one place.
        root_run_id=(getattr(prev_state, "run_id", "") or _ss(slug, "thread_id") or "").strip(),
    )
    carry_over_economy_cache(prev_state, state)
    if prev_state is None:
        _load_economy_cache(slug, state)
    _reindex_history(state.history)
    return state


def _ensure_loop_state(slug: str, state, user_message, *, economy_mode: bool,
                       web_search_enabled: bool, safety_mode: bool,
                       injection_protection: bool = True) -> tuple:
    """Return ``(state, should_return)`` for the step about to run.

    A brand-new loop starts either when there is no loop yet, or when the
    previous loop has finished and the user has submitted a new message; an
    empty message ends the step instead of starting one.
    """
    if state is not None and not (user_message and state.phase in ("done", "error")):
        return state, False
    if not user_message or not user_message.strip():
        return state, True
    state = _new_loop_state(slug, state, user_message,
                            economy_mode=economy_mode,
                            web_search_enabled=web_search_enabled,
                            safety_mode=safety_mode,
                            injection_protection=injection_protection)
    _set_ss(slug, "loop_state", state)
    _set_ss(slug, "stop_requested", False)
    return state, False


def _register_root_run(slug: str, state, events: list) -> tuple:
    """Register the dialog as the ROOT run of this step and subscribe to it.

    The record gives a spawned child its parent (depth and tree limits), and
    the subscription is what carries child events into this step's feed:
    ``service.publish`` delivers descendants' events to the subscribers of
    their ancestors. A failure leaves the step running without a tree.
    """
    orchestration = None
    unsubscribe = None
    root_run_id = (getattr(state, "run_id", "") or _ss(slug, "thread_id") or "").strip()
    if not root_run_id:
        return orchestration, unsubscribe, root_run_id
    try:
        from core.orchestration.service import get_service
        _orch_now = get_orchestrator(slug)
        orchestration = get_service()
        orchestration.ensure_run(
            root_run_id,
            thread_id=str(_ss(slug, "thread_id") or ""),
            agent_slug=slug,
            config=(_orch_now.get("config") or {}) if _orch_now else None,
        )
        # Remember the tree for the live status line: the dialog itself no
        # longer holds the run record between script runs, and the mailbox is
        # addressed by this id.
        _set_ss(slug, "active_root_run_id", root_run_id)
        state.root_run_id = root_run_id

        def _forward_child_event(ev: dict) -> None:
            """Collect a published event into the step feed."""
            if isinstance(ev, dict):
                events.append(ev)

        unsubscribe = orchestration.subscribe(root_run_id, _forward_child_event)
    except Exception:
        orchestration = None
        unsubscribe = None
    return orchestration, unsubscribe, root_run_id


def _handle_stop_requested(slug: str, state, lang: str, orchestration,
                           unsubscribe, root_run_id: str) -> bool:
    """Apply a pending stop request; return True when the step must end.

    The step returns before the normal cleanup, so the listener and the tree
    are released here: a stale subscriber would keep the finished step's event
    list alive and the tree would leak a pool.
    """
    if not _ss(slug, "stop_requested"):
        return False
    state.phase = "done"
    state.final_status = "stopped_by_user"
    state.final_text = t("devagent_stopped_by_user", lang=lang)
    _save_economy_cache(slug, state)
    _release_step_resources(orchestration, unsubscribe, root_run_id, release_tree=True)
    _set_ss(slug, "loop_state", None)
    _set_ss(slug, "stop_requested", False)
    return True


def _run_agent_step(state, dispatcher, lang: str, on_event):
    """Run one agent loop step, turning a crash into an error state.

    A failure keeps the SAME state object: it is already referenced by the
    subscriber and by the caller, so the error must land on it in place.
    """
    try:
        return step_agent_loop(state, dispatcher=dispatcher, lang=lang, on_event=on_event)
    except Exception as exc:
        state.phase = "error"
        state.error_message = str(exc)
        on_event({"type": "error", "error": str(exc)})
        return state


def _persist_step_to_thread(slug: str, state) -> None:
    """Append the messages produced by this step to the dialog thread."""
    tid = _ss(slug, "thread_id")
    if not tid or not state.history:
        return
    cursor = _ss(slug, "saved_msg_count") or 0
    new_cursor = persist_step_messages(tid, state.history, cursor)
    _set_ss(slug, "saved_msg_count", new_cursor)

    # Persist the LAST active workspace / target_file so reopening the thread
    # from history restores the correct project folder.
    _persist_thread_workspace_meta(tid)


def _commit_loop_state(slug: str, state) -> None:
    """Keep or clear the loop state according to the step's phase.

    The loop state is kept while the agent waits for the user to act (plan
    approval, dangerous-operation confirmation, sanitation approval) and is
    cleared for every other terminal status.
    """
    if state.phase not in ("done", "error"):
        _set_ss(slug, "loop_state", state)
        return
    # Persist the economy window BEFORE the loop state may be cleared:
    # terminal statuses clear loop_state, but the anchor must survive.
    _save_economy_cache(slug, state)
    if getattr(state, "final_status", "") in ("awaiting_user", "awaiting_approval",
                                               "awaiting_confirmation", "sanitized_required"):
        _set_ss(slug, "loop_state", state)
    else:
        _set_ss(slug, "loop_state", None)


def _do_step_impl(slug: str, lang: str) -> None:
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
    flags = _read_step_flags(slug)
    state, should_return = _ensure_loop_state(
        slug, state, _pop_ss(slug, "user_message"),
        economy_mode=flags[0], web_search_enabled=flags[1],
        safety_mode=flags[2], injection_protection=flags[3],
    )
    if should_return:
        return

    dispatcher.core.set_history(state.history)

    orchestration, unsubscribe, root_run_id = _register_root_run(slug, state, events)
    if _handle_stop_requested(slug, state, lang, orchestration, unsubscribe, root_run_id):
        return

    _sync_safety_mode(dispatcher, state, flags[2], flags[3])
    state = _run_agent_step(state, dispatcher, lang, on_event)

    _set_ss(slug, "history", state.history)

    # Attach the step events to the message that renders them. A step that
    # failed before producing an assistant reply (e.g. the very first LLM
    # call) attaches its error to the user request instead, so the failure
    # stays visible in the chat feed even without an active thread.
    _attach_events(state.history, events)

    _release_step_resources(orchestration, unsubscribe, root_run_id,
                            release_tree=state.phase in ("done", "error"))
    _persist_step_to_thread(slug, state)
    _commit_loop_state(slug, state)


def _reset_dialog_impl(slug: str) -> None:
    """Reset dialog state for an orchestrator."""
    _set_ss(slug, "history", [])
    _set_ss(slug, "loop_state", None)
    _set_ss(slug, "economy_cache", None)
    _set_ss(slug, "token_line_cache", None)
    _set_ss(slug, "thread_id", None)
    _set_ss(slug, "stop_requested", False)
    _set_ss(slug, "saved_msg_count", 0)
    _set_ss(slug, "chat_show_count", CHAT_PAGE_SIZE)
    _set_ss(slug, "scroll_bottom", False)
    _set_ss(slug, "folder_choice", "")
    _set_ss(slug, "folder_picker_counter",
            int(_ss(slug, "folder_picker_counter") or 0) + 1)
    if _sk(slug, "dispatcher") in st.session_state:
        del st.session_state[_sk(slug, "dispatcher")]
    # A fresh dialog starts in the EMPTY workspace state: repoint the live
    # config at the neutral root so the new dialog never inherits the folder
    # of a parallel dialog. Threads with a chosen folder re-apply their own
    # state per dispatch (workspace_binding.thread_context).
    try:
        dagent_config.apply_paths(dagent_config.NEUTRAL_ROOT,
                                  create_dirs=False, selected=False)
    except Exception:
        pass


def _load_thread_impl(slug: str, tid: str) -> None:
    """Load a thread into the current session and restore its workspace."""
    _reset_dialog(slug)

    # Restore the LAST workspace recorded for this thread so DevAgent works
    # on the same project folder as before.
    meta = load_thread_meta(tid)
    workspace = meta.get("workspace") or ""
    target_file = meta.get("target_file") or ""

    restored = False
    try:
        if target_file and os.path.isfile(target_file):
            wt.set_target_file(target_file, slug=slug)
            restored = True
        elif workspace and os.path.isdir(workspace):
            wt.set_workspace(workspace, slug=slug)
            restored = True
    except Exception:
        restored = False

    # A thread without a saved folder - or whose folder no longer exists -
    # opens in the EMPTY state applied by _reset_dialog; it must never
    # inherit the folder of a neighbouring dialog.

    msgs = load_thread_messages(tid)
    _set_ss(slug, "history", msgs)
    _set_ss(slug, "thread_id", tid)
    _set_ss(slug, "saved_msg_count", len(msgs))
    # Mirror the ACTUAL restored state into the thread binding: a
    # folder-less thread is bound as workspace-less so its dispatches keep
    # running in the empty state.
    try:
        if restored:
            wb.sync_registry_from_config(tid)
        else:
            wb.register_thread(tid, workspace=None, target_file=None)
    except Exception:
        pass


def _sync_chat_pref_checkbox(slug: str, pref: str, value: bool) -> None:
    """Write one chat preference into the canonical slot.

    The toolbar renders a single checkbox widget whose key IS the canonical
    session slot; this helper persists the changed preference into the
    orchestrator config via ``_save_chat_pref`` and, for safety_mode,
    rebuilds the live dispatcher so the switch applies at once.
    """
    _set_ss(slug, pref, value)
    _save_chat_pref(slug, pref, value)
    if pref == "safety_mode":
        try:
            _make_dispatcher(slug)
        except Exception:
            pass


def _chat_toolbar_pref_changed(slug: str, pref: str) -> None:
    """Persist a chat preference after the user toggles its checkbox.

    Streamlit calls this callback AFTER updating the canonical
    ``orch_<slug>_<pref>`` session-state slot, so the current value is
    read from that slot instead of comparing a pre-render snapshot.
    """
    _sync_chat_pref_checkbox(slug, pref, bool(_ss(slug, pref)))


def _cancel_dialog_tree(slug: str, loop_state) -> None:
    """Cancel the whole run tree of this dialog.

    Child runs never see the dialog's stop flag, so without this cascade they
    would keep working after the user pressed Stop and burn provider tokens.
    """
    try:
        from core.sandbox import active as sandbox_active

        # A child process has its own registry, so Stop must reach it too,
        # or a snippet started a second ago keeps writing after the click.
        sandbox_active.cancel_thread(str(_ss(slug, "thread_id") or ""))
    except Exception:
        pass
    try:
        from core.orchestration.service import get_service
        _root = (getattr(loop_state, "run_id", "") or _ss(slug, "thread_id") or "").strip()
        if _root:
            get_service().cancel(_root, cascade=True)
    except Exception:
        pass
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
