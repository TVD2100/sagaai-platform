# -*- coding: utf-8 -*-
"""
ui.pages.orchestrator - reusable orchestrator chat page.

Parameterised by orchestrator slug. Both DevAgent and custom orchestrators
render through this module. History and Settings live on dedicated pages
(ui.pages.history and ui.pages.orchestrator_settings).

The page itself is split by responsibility across the sibling modules; this
module is the public surface. A helper the tests replace through the module
object is wrapped here, so a patch installed on this module reaches the
implementation wherever it lives.

All user-facing strings go through t(key, lang=lang).
"""
from typing import Any, Callable, Dict, List, Optional

import streamlit as st

from core import folder_dialog
from core.api_layer import send_request
from core.config import load_config
from core.connectors import list_connections
from core.feed_autoscroll import render_autoscroll
from core.files import (
    MAX_THREAD_FILE_BYTES,
    build_saved_files_registry,
    build_thread_files_notice,
    check_context,
)
from core.fs import combine_nonempty
from core.i18n import t
from core.orchestration.contracts import (
    DEFAULT_ANSWER_MAX_CHARS,
    DEFAULT_ANSWER_WAIT_SECONDS,
    DEFAULT_DIALOG_ROUNDS,
    DEFAULT_MAX_PARKED_RUNS,
    DEFAULT_QUESTION_MAX_CHARS,
)
from core.orchestrator_tools import list_system_tools
from core.orchestrators import (
    DEVAGENT_SLUG,
    UNIVERSAL_AGENT_SLUG,
    build_assistant_dicts,
    get_allowed_agents,
    get_allowed_assistants,
    get_delegation_limits,
    get_disabled_tools,
    get_economy_config,
    get_economy_tail_messages,
    get_enabled_connections,
    get_enabled_skills,
    get_fetch_config,
    get_orchestrator,
    get_orchestrator_rag_bases,
    get_universal_agent_enabled,
    get_verifier_agent,
    get_web_search_config,
    list_orchestrators,
    orch_delete_function,
    orch_delete_instruction,
    orch_get_function,
    orch_get_instruction,
    orch_list_functions,
    orch_list_instructions,
    orch_save_function,
    orch_save_instruction,
    reset_builtin_prompt,
    save_orchestrator,
    set_allowed_agents,
    set_allowed_assistants,
    set_delegation_limits,
    set_disabled_tools,
    set_enabled_connections,
    set_enabled_skills,
    set_orchestrator_rag_bases,
    set_universal_agent_enabled,
    set_verifier_agent,
)
from core.rag import list_bases_with_activity
from core.recent_workspaces import add_recent_workspace, get_recent_workspaces
from core.render import _md_to_txt, clipboard_button, format_token_line, format_ts_label
from core.services import (
    default_reasoning_effort,
    get_fetch_models,
    get_image_models,
    get_model_reasoning_effort_options,
    get_services,
    get_vision_models,
    service_supports_reasoning_effort,
)
from core.skills_library import list_skills as list_library_skills
from core.threads_devagent import (
    append_thread_message,
    create_devagent_thread,
    list_thread_files,
    load_thread_messages,
    load_thread_meta,
    save_thread_file_data,
    sum_thread_tokens,
)
from dev_agent import config as dagent_config
from dev_agent import workspace_binding as wb
from dev_agent import workspace_tools as wt
from dev_agent.agent_loop import (
    AgentLoopState,
    apply_economy_cache,
    approve_pending_confirmation,
    approve_sanitized_content,
    build_economy_context,
    carry_over_economy_cache,
    deny_pending_confirmation,
    deny_sanitized_content,
    economy_cache_to_dict,
    step_agent_loop,
)
from dev_agent.universal_agent import UniversalDevAgent
from storage.models import DEFAULT_MAX_STEPS
from ui.components.flash import flash_error, flash_success
from ui.pages.orchestrator_chat import (
    _apply_autoscroll,
    _chat_show_count,
    _render_active_controls,
    _render_assistant_message,
    _render_chat_feed,
    _render_chat_header,
    _render_chat_styles,
    _render_chat_tab,
    _render_chat_welcome,
    _render_confirmation_buttons,
    _render_danger_confirmation,
    _render_history_message,
    _render_loop_status,
    _render_message_controls,
    _render_sanitized_confirmation,
    _render_show_earlier,
    _render_user_message,
    _resume_after_confirmation,
)
from ui.pages.orchestrator_chat_input import (
    _build_submission_text,
    _collect_attachment_context,
    _economy_window_info,
    _ensure_thread,
    _handle_chat_submission,
    _render_chat_toolbar,
    _render_token_line_impl,
    _services_with_web_search,
    _temp_slider,
    _token_line_cache_key,
)
from ui.pages.orchestrator_feed import (
    _EVENT_RENDERERS,
    _HIDDEN_EVENT_TYPES,
    _VISIBLE_PHASES,
    _render_applied_event,
    _render_error_event,
    _render_event_group,
    _render_event_impl,
    _render_events,
    _render_generated_image,
    _render_incomplete_retry_event,
    _render_parked_question,
    _render_phase_event,
    _render_result_payload,
    _render_retry_event,
    _render_sanitized_event,
    _render_skill_event,
    _render_tool_call_args,
    _render_tool_call_event,
    _render_tool_result_impl,
    _tool_result_header,
    _tool_result_summary,
)
from ui.pages.orchestrator_folder import (
    FOLDER_PICKER_BROWSE,
    FOLDER_PICKER_LIMIT,
    _apply_folder_choice,
    _folder_picker_key,
    _folder_picker_options,
    _open_folder_dialog,
    _render_folder_picker,
    _reset_folder_choice,
    _thread_folder_impl,
)
from ui.pages.orchestrator_loop import (
    _assistant_dicts,
    _assistant_has_api_key_impl,
    _cancel_dialog_tree,
    _chat_toolbar_pref_changed,
    _commit_loop_state,
    _dialog_tree_root,
    _do_step_impl,
    _ensure_loop_state,
    _handle_stop_requested,
    _live_subagent_runs,
    _load_thread_impl,
    _make_dispatcher_impl,
    _make_send_adapter_impl,
    _new_loop_state,
    _persist_step_to_thread,
    _read_step_flags,
    _register_root_run,
    _render_subagent_status,
    _reset_dialog_impl,
    _run_agent_step,
    _sync_chat_pref_checkbox,
    _waiting_subagent_runs,
)
from ui.pages.orchestrator_settings_delegation import (
    _live_orchestrator_entries,
    _number_input_setting,
    _render_allowed_agents,
    _render_allowed_assistants,
    _render_delegation_limits,
    _render_delegation_settings,
    _render_dialog_limits,
    _render_other_settings,
    _render_verifier_section,
    _save_delegation_settings,
)
from ui.pages.orchestrator_settings_models import (
    _current_optional_pair,
    _render_economy_settings,
    _render_models_settings,
    _render_optional_capability_sections,
    _render_optional_model_section,
    _render_prompt_settings,
    _render_reasoning_effort,
    _render_search_model_section,
    _render_search_pair,
    _render_search_prompt,
    _render_strong_model_section,
    _save_models_settings,
    _warn_unavailable_pair,
)
from ui.pages.orchestrator_settings_orch import (
    _render_function_cards,
    _render_function_form,
    _render_instruction_cards,
    _render_instruction_form,
    _render_memory_dir_section,
    _render_memory_editor,
    _render_orch_connections,
    _render_orch_memory,
    _render_orch_rag_bases,
    _render_orch_skills_settings,
    _render_orch_system_tools,
    _render_orchestrator_functions_settings,
    _render_orchestrator_instructions_settings,
)
from ui.pages.orchestrator_shared import (
    SETTINGS_SECTIONS,
    pin_settings_section_impl,
    settings_save_failed_impl,
)
from ui.pages.orchestrator_state import (
    CHAT_PAGE_SIZE,
    ECONOMY_MODE_ALWAYS_ON,
    _chat_prefs,
    _init_orch_state_impl,
    _load_economy_cache,
    _make_state_keys,
    _pop_ss,
    _save_chat_pref,
    _save_economy_cache,
    _set_ss_impl,
    _ss_impl,
)
from ui.pages.orchestrator_text import (  # noqa: F401  re-exported for the page and the tests
    _attach_events,
    _attachments_manifest_path,
    _chat_pref_config_keys,
    _chat_toolbar_widget_key,
    _dialog_limit,
    _extract_result_body,
    _first_two_lines,
    _format_call_args_preview,
    _get_max_tokens_limit,
    _is_agent_active,
    _last_assistant_index,
    _load_attachments_manifest,
    _model_ids,
    _persist_thread_workspace_meta,
    _reindex_history,
    _release_step_resources,
    _select_live_runs,
    _select_waiting_runs,
    _sk,
    _split_chat_prompt,
    _split_events_by_run,
    _strip_empty_fenced_blocks,
    _strip_html_details_tags,
    _strip_tool_calls,
    _strip_tool_calls_for_message,
    _sync_safety_mode,
    _tool_result_title,
    build_state_keys,
    settings_section_key,
    settings_tabs_key,
)

# Names the page exposes; every import above is a deliberate re-export.
__all__ = [
    "AgentLoopState",
    "Any",
    "CHAT_PAGE_SIZE",
    "Callable",
    "DEFAULT_ANSWER_MAX_CHARS",
    "DEFAULT_ANSWER_WAIT_SECONDS",
    "DEFAULT_DIALOG_ROUNDS",
    "DEFAULT_MAX_PARKED_RUNS",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_QUESTION_MAX_CHARS",
    "DEVAGENT_SLUG",
    "Dict",
    "ECONOMY_MODE_ALWAYS_ON",
    "FOLDER_PICKER_BROWSE",
    "FOLDER_PICKER_LIMIT",
    "List",
    "MAX_THREAD_FILE_BYTES",
    "Optional",
    "SETTINGS_SECTIONS",
    "UNIVERSAL_AGENT_SLUG",
    "UniversalDevAgent",
    "_EVENT_RENDERERS",
    "_HIDDEN_EVENT_TYPES",
    "_VISIBLE_PHASES",
    "_apply_autoscroll",
    "_apply_folder_choice",
    "_assistant_dicts",
    "_assistant_has_api_key",
    "_attach_events",
    "_attachments_manifest_path",
    "_build_submission_text",
    "_cancel_dialog_tree",
    "_chat_pref_config_keys",
    "_chat_prefs",
    "_chat_show_count",
    "_chat_toolbar_pref_changed",
    "_chat_toolbar_widget_key",
    "_collect_attachment_context",
    "_commit_loop_state",
    "_current_optional_pair",
    "_dialog_limit",
    "_dialog_tree_root",
    "_do_step",
    "_economy_window_info",
    "_ensure_loop_state",
    "_ensure_thread",
    "_extract_result_body",
    "_first_two_lines",
    "_folder_picker_key",
    "_folder_picker_options",
    "_format_call_args_preview",
    "_get_max_tokens_limit",
    "_handle_chat_submission",
    "_handle_stop_requested",
    "_init_orch_state",
    "_is_agent_active",
    "_last_assistant_index",
    "_live_orchestrator_entries",
    "_live_subagent_runs",
    "_load_attachments_manifest",
    "_load_economy_cache",
    "_load_thread",
    "_make_dispatcher",
    "_make_send_adapter",
    "_make_state_keys",
    "_md_to_txt",
    "_model_ids",
    "_new_loop_state",
    "_number_input_setting",
    "_open_folder_dialog",
    "_persist_step_to_thread",
    "_persist_thread_workspace_meta",
    "_pop_ss",
    "_read_step_flags",
    "_register_root_run",
    "_reindex_history",
    "_release_step_resources",
    "_render_active_controls",
    "_render_allowed_agents",
    "_render_allowed_assistants",
    "_render_applied_event",
    "_render_assistant_message",
    "_render_chat_feed",
    "_render_chat_header",
    "_render_chat_styles",
    "_render_chat_tab",
    "_render_chat_toolbar",
    "_render_chat_welcome",
    "_render_confirmation_buttons",
    "_render_danger_confirmation",
    "_render_delegation_limits",
    "_render_delegation_settings",
    "_render_dialog_limits",
    "_render_economy_settings",
    "_render_error_event",
    "_render_event",
    "_render_event_group",
    "_render_events",
    "_render_folder_picker",
    "_render_function_cards",
    "_render_function_form",
    "_render_generated_image",
    "_render_history_message",
    "_render_incomplete_retry_event",
    "_render_instruction_cards",
    "_render_instruction_form",
    "_render_loop_status",
    "_render_memory_dir_section",
    "_render_memory_editor",
    "_render_message_controls",
    "_render_models_settings",
    "_render_optional_capability_sections",
    "_render_optional_model_section",
    "_render_orch_connections",
    "_render_orch_memory",
    "_render_orch_rag_bases",
    "_render_orch_skills_settings",
    "_render_orch_system_tools",
    "_render_orchestrator_functions_settings",
    "_render_orchestrator_instructions_settings",
    "_render_other_settings",
    "_render_parked_question",
    "_render_phase_event",
    "_render_prompt_settings",
    "_render_reasoning_effort",
    "_render_result_payload",
    "_render_retry_event",
    "_render_sanitized_confirmation",
    "_render_sanitized_event",
    "_render_search_model_section",
    "_render_search_pair",
    "_render_search_prompt",
    "_render_show_earlier",
    "_render_skill_event",
    "_render_strong_model_section",
    "_render_subagent_status",
    "_render_token_line",
    "_render_tool_call_args",
    "_render_tool_call_event",
    "_render_tool_result",
    "_render_user_message",
    "_render_verifier_section",
    "_reset_dialog",
    "_reset_folder_choice",
    "_resume_after_confirmation",
    "_run_agent_step",
    "_save_chat_pref",
    "_save_delegation_settings",
    "_save_economy_cache",
    "_save_models_settings",
    "_select_live_runs",
    "_select_waiting_runs",
    "_services_with_web_search",
    "_set_ss",
    "_sk",
    "_split_chat_prompt",
    "_split_events_by_run",
    "_ss",
    "_strip_empty_fenced_blocks",
    "_strip_html_details_tags",
    "_strip_tool_calls",
    "_strip_tool_calls_for_message",
    "_sync_chat_pref_checkbox",
    "_sync_safety_mode",
    "_temp_slider",
    "_thread_folder",
    "_token_line_cache_key",
    "_tool_result_header",
    "_tool_result_summary",
    "_tool_result_title",
    "_waiting_subagent_runs",
    "_warn_unavailable_pair",
    "add_recent_workspace",
    "append_thread_message",
    "apply_economy_cache",
    "approve_pending_confirmation",
    "approve_sanitized_content",
    "build_assistant_dicts",
    "build_economy_context",
    "build_saved_files_registry",
    "build_state_keys",
    "build_thread_files_notice",
    "carry_over_economy_cache",
    "check_context",
    "clipboard_button",
    "combine_nonempty",
    "create_devagent_thread",
    "dagent_config",
    "default_reasoning_effort",
    "deny_pending_confirmation",
    "deny_sanitized_content",
    "economy_cache_to_dict",
    "flash_error",
    "flash_success",
    "folder_dialog",
    "format_token_line",
    "format_ts_label",
    "get_allowed_agents",
    "get_allowed_assistants",
    "get_delegation_limits",
    "get_disabled_tools",
    "get_economy_config",
    "get_economy_tail_messages",
    "get_enabled_connections",
    "get_enabled_skills",
    "get_fetch_config",
    "get_fetch_models",
    "get_image_models",
    "get_model_reasoning_effort_options",
    "get_orchestrator",
    "get_orchestrator_rag_bases",
    "get_recent_workspaces",
    "get_services",
    "get_universal_agent_enabled",
    "get_verifier_agent",
    "get_vision_models",
    "get_web_search_config",
    "list_bases_with_activity",
    "list_connections",
    "list_library_skills",
    "list_orchestrators",
    "list_system_tools",
    "list_thread_files",
    "load_config",
    "load_thread_messages",
    "load_thread_meta",
    "orch_delete_function",
    "orch_delete_instruction",
    "orch_get_function",
    "orch_get_instruction",
    "orch_list_functions",
    "orch_list_instructions",
    "orch_save_function",
    "orch_save_instruction",
    "page_devagent",
    "page_orchestrator",
    "render_autoscroll",
    "reset_builtin_prompt",
    "save_orchestrator",
    "save_thread_file_data",
    "send_request",
    "service_supports_reasoning_effort",
    "set_allowed_agents",
    "set_allowed_assistants",
    "set_delegation_limits",
    "set_disabled_tools",
    "set_enabled_connections",
    "set_enabled_skills",
    "set_orchestrator_rag_bases",
    "set_universal_agent_enabled",
    "set_verifier_agent",
    "settings_section_key",
    "settings_tabs_key",
    "st",
    "step_agent_loop",
    "sum_thread_tokens",
    "t",
    "wb",
    "wt",
]


def _assistant_has_api_key(*args, **kwargs):
    """Call ``_assistant_has_api_key_impl`` with the arguments the caller passed."""
    return _assistant_has_api_key_impl(*args, **kwargs)


def _do_step(*args, **kwargs):
    """Call ``_do_step_impl`` with the arguments the caller passed."""
    return _do_step_impl(*args, **kwargs)


def _init_orch_state(*args, **kwargs):
    """Call ``_init_orch_state_impl`` with the arguments the caller passed."""
    return _init_orch_state_impl(*args, **kwargs)


def _load_thread(*args, **kwargs):
    """Call ``_load_thread_impl`` with the arguments the caller passed."""
    return _load_thread_impl(*args, **kwargs)


def _make_dispatcher(*args, **kwargs):
    """Call ``_make_dispatcher_impl`` with the arguments the caller passed."""
    return _make_dispatcher_impl(*args, **kwargs)


def _make_send_adapter(*args, **kwargs):
    """Call ``_make_send_adapter_impl`` with the arguments the caller passed."""
    return _make_send_adapter_impl(*args, **kwargs)


def _render_event(*args, **kwargs):
    """Call ``_render_event_impl`` with the arguments the caller passed."""
    return _render_event_impl(*args, **kwargs)


def _render_token_line(*args, **kwargs):
    """Call ``_render_token_line_impl`` with the arguments the caller passed."""
    return _render_token_line_impl(*args, **kwargs)


def _render_tool_result(*args, **kwargs):
    """Call ``_render_tool_result_impl`` with the arguments the caller passed."""
    return _render_tool_result_impl(*args, **kwargs)


def _reset_dialog(*args, **kwargs):
    """Call ``_reset_dialog_impl`` with the arguments the caller passed."""
    return _reset_dialog_impl(*args, **kwargs)


def _set_ss(*args, **kwargs):
    """Call ``_set_ss_impl`` with the arguments the caller passed."""
    return _set_ss_impl(*args, **kwargs)


def _ss(*args, **kwargs):
    """Call ``_ss_impl`` with the arguments the caller passed."""
    return _ss_impl(*args, **kwargs)


def _thread_folder(*args, **kwargs):
    """Call ``_thread_folder_impl`` with the arguments the caller passed."""
    return _thread_folder_impl(*args, **kwargs)


def pin_settings_section(*args, **kwargs):
    """Call ``pin_settings_section_impl`` with the arguments the caller passed."""
    return pin_settings_section_impl(*args, **kwargs)


def settings_save_failed(*args, **kwargs):
    """Call ``settings_save_failed_impl`` with the arguments the caller passed."""
    return settings_save_failed_impl(*args, **kwargs)


def page_orchestrator(slug: str) -> None:
    _init_orch_state(slug)
    lang = st.session_state.get("ui_lang")
    orch = get_orchestrator(slug)
    orch_name = orch.get("name", slug) if orch else slug

    # DevAgent has its own icon, custom employees share the robots icon.
    icon = "🛠️" if slug == DEVAGENT_SLUG else "🤖🤖"
    st.title(f"{icon} {orch_name}")

    _render_chat_tab(slug, lang)


def page_devagent() -> None:
    """DevAgent page - delegates to the generic orchestrator page."""
    page_orchestrator(DEVAGENT_SLUG)


# The page modules resolve their late references through this object: bind it
# last, when every name the two page entries need is defined.
import sys as _sys  # noqa: E402

from ui.pages.orchestrator_shared import bind_facade as _bind_facade  # noqa: E402

_bind_facade(_sys.modules[__name__])
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
