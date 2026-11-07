# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator_folder - Folder selection: the recent-folders picker and the native dialog.

Part of the orchestrator page. A name the tests may replace through the
facade (``ui.pages.orchestrator``) is resolved late, at call time, so a
patch installed on the facade reaches the implementation here.
"""
import os
from typing import Optional

from core import folder_dialog
from dev_agent import (
    workspace_binding as wb,
)
from dev_agent import (
    workspace_tools as wt,
)
from ui.pages.orchestrator_shared import late, st_proxy
from ui.pages.orchestrator_text import _persist_thread_workspace_meta

st = st_proxy()
_set_ss = late("_set_ss")
_ss = late("_ss")
_thread_folder = late("_thread_folder")
add_recent_workspace = late("add_recent_workspace")
get_recent_workspaces = late("get_recent_workspaces")
t = late("t")


FOLDER_PICKER_LIMIT = 10


FOLDER_PICKER_BROWSE = "\x00browse"


def _folder_picker_key(slug: str) -> str:
    """Return the widget key of the folder selector for this orchestrator.

    The key embeds a counter, so snapping the selector back to "Авто" after
    a send creates a BRAND-NEW widget instead of mutating an existing one
    (Streamlit forbids changing a widget's session state after it was
    instantiated in the same run).
    """
    return f"orch_folder_pick_{slug}_{int(_ss(slug, 'folder_picker_counter') or 0)}"


def _reset_folder_choice(slug: str) -> None:
    """Snap the folder selector back to "Авто" (via a fresh widget key)."""
    _set_ss(slug, "folder_choice", "")
    _set_ss(slug, "folder_picker_counter",
            int(_ss(slug, "folder_picker_counter") or 0) + 1)


def _thread_folder_impl(slug: str) -> str:
    """Return the working folder of THIS dialog (authoritative source).

    Prefers the thread binding: the live config may belong to a
    neighbouring dialog or hold a temporary detour the agent itself made.
    Falls back to the live workspace when the dialog has no thread and to
    a staged choice when the user just picked one.
    """
    tid = _ss(slug, "thread_id")
    if tid:
        try:
            bound = wb.get_thread_workspace(tid)
            if bound:
                return str(bound)
        except Exception:
            pass
    try:
        cur = wt.current_workspace()
        if cur.get("workspace_selected", True):
            return str(cur.get("root", "") or "")
    except Exception:
        pass
    return ""


def _apply_folder_choice(slug: str) -> Optional[str]:
    """Apply the folder staged in the selector BEFORE the LLM call.

    Called from the send path so the agent never spends a step calling
    ``set_workspace`` itself. The switch is recorded in the recent-folders
    history by ``set_workspace`` and bound to the dialog's thread, which
    makes it authoritative on the next dispatch. Afterwards the selector
    snaps back to "Авто" - the folder becomes dialog-managed, so a
    temporary detour the agent makes later is never overwritten by the UI.

    Returns the applied path, or None when nothing was staged (the normal
    case after the first message) or the folder was already in effect.
    """
    path = str(_ss(slug, "folder_choice") or "").strip()
    if not path:
        return None
    if not os.path.isdir(path):
        _reset_folder_choice(slug)
        return None
    tid = _ss(slug, "thread_id")
    # Compare against THIS dialog's thread binding - not the live config,
    # which may belong to a neighbouring dialog or to a temporary detour
    # the agent itself made while inspecting another folder.
    already = bool(tid) and wb.get_thread_workspace(tid) == path
    if not already:
        try:
            wt.set_workspace(path, slug=slug)
        except Exception:
            _reset_folder_choice(slug)
            return None
        if tid:
            try:
                wb.register_thread(tid, workspace=path, target_file=None)
                _persist_thread_workspace_meta(tid)
            except Exception:
                pass
    _reset_folder_choice(slug)
    return path


def _render_folder_picker(slug: str, lang: str) -> None:
    """Render the "Выбор папки" block right above the chat input.

    Offers "Авто", the orchestrator's recent folders (up to
    FOLDER_PICKER_LIMIT) and - last - an entry that opens the native OS folder
    dialog. The dialog lives INSIDE the selector: a separate "Обзор…" button
    reads as "attach a file" and was clicked by mistake. Selecting a folder
    does not touch the workspace immediately - it is applied when the next
    message is sent, and the dialog entry is an ACTION whose fresh widget key
    rebuilds the selector.
    """
    options = _folder_picker_options(slug)
    staged = str(_ss(slug, "folder_choice") or "")

    def _label(path: str) -> str:
        if not path:
            return t("orch_folder_auto", lang=lang)
        if path == FOLDER_PICKER_BROWSE:
            return t("orch_folder_browse", lang=lang)
        return f"📁 {os.path.basename(path.rstrip('/')) or path} - {path}"

    index = options.index(staged) if staged in options else 0

    picked = st.selectbox(
        t("orch_folder_picker_label", lang=lang),
        options=range(len(options)),
        format_func=lambda i: _label(options[i]),
        index=index,
        key=_folder_picker_key(slug),
    )
    try:
        picked_value = options[int(picked)]
    except Exception:
        # A stale widget value (for example after the dialog entry vanished
        # because the native picker stopped being available) must never
        # wipe the folder the user already staged: keep it as it was.
        picked_value = staged

    if picked_value == FOLDER_PICKER_BROWSE:
        _open_folder_dialog(slug, lang, staged)
    else:
        _set_ss(slug, "folder_choice", picked_value)

    # Always show the working folder of THIS dialog. The thread binding is
    # authoritative: the live config may belong to a neighbouring dialog or
    # hold a temporary detour the agent made; a just-picked folder wins
    # until it is applied on the next send.
    active = staged or _thread_folder(slug)
    if active:
        st.caption(t("orch_folder_active", lang=lang, path=active))


def _folder_picker_options(slug: str) -> list:
    """Return the selector options: Авто, recent folders and the dialog entry."""
    options: list = [""]
    try:
        options.extend(get_recent_workspaces(slug, limit=FOLDER_PICKER_LIMIT))
    except Exception:
        pass

    staged = str(_ss(slug, "folder_choice") or "")
    if staged and staged not in options:
        options.append(staged)

    # The dialog entry is offered only when a native picker actually exists;
    # without a GUI the selector keeps the manual-path workflow intact.
    try:
        dialog_available = bool(folder_dialog.native_dialog_available())
    except Exception:
        dialog_available = False
    if dialog_available:
        options.append(FOLDER_PICKER_BROWSE)
    return options


def _open_folder_dialog(slug: str, lang: str, staged: str) -> None:
    """Run the native folder dialog and record the outcome."""
    chosen = folder_dialog.pick_folder(
        initial_dir=staged or None,
        title=t("orch_folder_dialog_title", lang=lang),
    )
    if chosen:
        add_recent_workspace(chosen, slug=slug)
        _set_ss(slug, "folder_choice", chosen)
    # A fresh widget key drops the action entry whether a folder was
    # chosen or the dialog was cancelled.
    _set_ss(slug, "folder_picker_counter",
            int(_ss(slug, "folder_picker_counter") or 0) + 1)
    st.rerun()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
