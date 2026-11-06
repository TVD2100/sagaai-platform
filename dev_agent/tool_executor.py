# ruff: noqa: F401
# The import block below is a deliberate re-export surface. Tool handlers read
# these names through ``tools.deps.build_deps`` (which looks them up on this
# module on every call), and the test suite replaces several of them here with
# ``patch('dev_agent.tool_executor.<name>')``. Removing an import would break
# that lookup, so they are declared even though no code in this file reads them
# directly.
# -*- coding: utf-8 -*-
# DevAgent tool set -- the bridge between the LLM and the project filesystem.
#
# Each public method maps to a tool the LLM can call (see section 7.3 of the
# architecture). All tools return plain dicts (JSON-serializable) so they can be
# rendered in the UI and logged into pipeline_runs / changelog.
#
# Read tools return line-numbered content so the LLM can address edits by line
# number (the primary patch addressing mechanism). This is what makes the
# token-efficient patch workflow possible: the model sees exact line numbers
# and emits small fragments instead of whole files.

import subprocess  # noqa: F401  patch point: tests replace the module here
from typing import Any, Dict, List, Optional

from core.api_errors import APIError, api_error_message
from core.api_layer import send_request
from core.assistants import create_assistant, get_assistant_by_id, update_assistant
from core.config import load_config, load_devagent_config
from core.dangerous import tool_needs_confirmation
from core.i18n import invalidate_langs_cache
from core.instructions import (
    get_instruction,
    get_instruction_for,
    get_instruction_prompt,
    list_instructions,
    list_instructions_for,
)
from core.prompt_guard import (
    is_tool_result_text,
    sanitize_fetched_page,
    sanitize_search_result,
    sanitize_tool_result_content,
)
from core.services import get_services

# Import lang cache invalidation so newly added language files appear immediately
from . import config

# Import assistant detection functionality
from .assistant_detector import (
    ASSISTANT_CREATOR_INSTRUCTION_ID,
    detect_and_select_assistant,
    list_all_assistants_for_detection,
)

# Import new automatic model resolution
from .assistant_model_resolver import (
    classify_assistant_requirements,
    resolve_service_model_for_assistant,
)
from .backup_manager import BackupManager
from .llm_utils import call_llm_with_system
from .safe_writer import SafeWriter
from .tools.assistants import AssistantTools
from .tools.clock import ClockTools
from .tools.editing import EditingTools
from .tools.exec import ExecTools
from .tools.files import FilesTools
from .tools.history import HistoryTools
from .tools.images import ImageTools
from .tools.library import LibraryTools
from .tools.patch import PatchTools
from .tools.rag import RagTools
from .tools.task_state import TaskStateTools
from .tools.url_fetch import UrlFetchTools
from .tools.web_search import WebSearchTools
from .tools.workspace import WorkspaceTools


class ToolExecutor(FilesTools, EditingTools, PatchTools, ExecTools, WebSearchTools, UrlFetchTools, ImageTools, RagTools, HistoryTools, LibraryTools, AssistantTools, TaskStateTools, ClockTools, WorkspaceTools):
    def __init__(self):
        config.ensure_runtime_dirs()
        self.backups = BackupManager()
        self.writer = SafeWriter(backup_manager=self.backups)
        # Callable for LLM requests (set by agent loop).
        self._send_request_fn = None
        # Flag: True only during the assistant_detection phase of the agent loop.
        # Set by the agent loop before calling dispatch("detect_and_select_assistant", ...)
        # and cleared immediately afterwards. Prevents the LLM from triggering
        # assistant detection in the middle of a cycle, which would create duplicate assistants.
        self._assistant_detection_phase = False
        # Web-search enabled flag (set by UI). When False, web_search returns an error.
        self._web_search_enabled = True
        # Active orchestrator slug this executor serves (set via
        # UniversalDevAgent.attach_orchestrator). Used to resolve
        # orchestrator-specific instructions for meta-tasks.
        self._orchestrator_slug: str = "dev_agent"
        # Working-memory root of that employee (DATA_DIR/agents_data/<slug> by
        # default). File tools reach it through the ``memory:`` prefix; it is
        # refreshed together with the slug so the tools and the prompt never
        # disagree about where the employee's data folder is.
        try:
            config.set_memory_root(self._orchestrator_slug, str(config.PROJECT_ROOT))
        except Exception:
            pass
        # Web-search config (set by the orchestrator UI). When present, it takes
        # precedence over the global DevAgent config: service, model, temperature,
        # max_tool_calls, and base system prompt. Falls back to the legacy global
        # DevAgent config when unset (e.g. when called outside an orchestrator).
        self._web_search_config: Optional[Dict[str, Any]] = None
        # URL-fetch config (service/model/Selenium) set by the orchestrator
        # UI. Empty or unset means neither a fetch model nor Selenium is
        # configured: the url_fetch tool is absent from the catalog and
        # refuses to run.
        self._url_fetch_config: Optional[Dict[str, Any]] = None
        # Safety-mode gate set by the UI. When False, run_code/run_test execute
        # without the usual dangerous-pattern confirmation check.
        self._safety_enabled = True
        # History cache for economy mode (get_history_index / get_history_messages).
        self._history: List[Dict[str, Any]] = []

    def set_send_request(self, fn):
        """Inject the LLM-request callable so assistant-detection tools can use it."""
        self._send_request_fn = fn

    # --- dispatch -----------------------------------
    def set_orchestrator_slug(self, slug: str) -> None:
        """Point the executor at another employee and refresh its memory root.

        Called by ``UniversalDevAgent.attach_orchestrator`` and on restore, so
        the ``memory:`` prefix always resolves to the folder of the employee
        that is actually running. The workspace is passed explicitly because
        the resolver needs it to expand a project-relative override.
        """
        self._orchestrator_slug = (slug or "dev_agent").strip() or "dev_agent"
        try:
            workspace = (str(config.PROJECT_ROOT)
                         if getattr(config, "WORKSPACE_SELECTED", True) else "")
            config.set_memory_root(self._orchestrator_slug, workspace)
        except Exception:
            pass

    def dispatch(self, tool_name: str, args: Any) -> Dict[str, Any]:
        """Route a tool call to the corresponding method.

        Accepts a flat dict of arguments (as received from the LLM) and
        passes them to the matching method by keyword. Unknown argument
        names are rejected with a structured error (``unknown_args`` +
        ``suggestion``) BEFORE the call so the agent can fix the call
        instead of guessing from a generic TypeError. Missing required
        arguments produce the same structured form; any other unhandled
        exception is caught and returned as an error dict so the agent
        loop never crashes.
        """
        if not isinstance(args, dict):
            return {"ok": False, "error": f"args must be a dict, got {type(args).__name__}"}
        if tool_name in _LEGACY_TOOL_ALIASES:
            tool_name = _LEGACY_TOOL_ALIASES[tool_name]
        guard = workspace_guard_error(tool_name, args)
        if guard is not None:
            return guard
        method = getattr(self, tool_name, None)
        if method is None:
            return {"ok": False, "error": f"Unknown tool: {tool_name}"}
        unknown = _unknown_args(method, args)
        if unknown:
            return {
                "ok": False,
                "error": _UNKNOWN_ARGS_ERROR.format(
                    tool=tool_name, unknown=", ".join(sorted(unknown))
                ),
                "unknown_args": sorted(unknown),
                "suggestion": _usage_string(tool_name, method),
            }
        try:
            args = _coerce_numeric_args(method, args)
            result = method(**args)
            # ── Tool-result size cap (context-overflow protection) ──────────
            # Results above _TOOL_RESULT_INLINE_LIMIT serialized characters
            # are spilled to disk before the first send and replaced by a
            # bounded preview; above MAX_TOOL_RESULT_CHARS an explicit
            # ok=False size error is returned instead (the agent loop / UI
            # would otherwise inject the payload into the model context).
            # Policy implemented in dev_agent.agent_loop
            # (_apply_tool_result_cap); lazy import keeps module loading
            # order safe.
            if tool_name in ("propose_file", "apply_patch") and result.get("applied") is False:
                return result
            try:
                from .agent_loop import _apply_tool_result_cap
                return _apply_tool_result_cap(result, tool_name)
            except Exception:
                return result
        except TypeError as e:
            return {
                "ok": False,
                "error": f"Tool {tool_name} error: {e}",
                "suggestion": _usage_string(tool_name, method),
            }
        except Exception as e:
            return {"ok": False, "error": f"Tool {tool_name} error: {e}"}

    def dispatch_json(self, call: Dict[str, Any]) -> Dict[str, Any]:
        """Route a parsed tool call (from parse_tool_calls) - same as dispatch
        but always unpacks 'args' from the call dict."""
        tool_name = call.get("tool", "")
        args = call.get("args", {})
        if not isinstance(args, dict):
            args = {}
        return self.dispatch(tool_name, args)


# --- Re-exported helpers and constants ----------------------------------------
# Kept importable from this module so callers and tests keep their old paths.
from .tools.args import (  # noqa: E402
    _LEGACY_TOOL_ALIASES,
    _UNKNOWN_ARGS_ERROR,
    _coerce_numeric_args,
    _unknown_args,
    _usage_string,
)
from .tools.catalog import TOOL_CATALOG  # noqa: E402
from .tools.workspace import (  # noqa: E402,F401
    WORKSPACE_REQUIRED_TOOLS,
    _addresses_memory,
    workspace_guard_error,
)

__all__ = [
    'TOOL_CATALOG',
    'ToolExecutor',
    'WORKSPACE_REQUIRED_TOOLS',
    'workspace_guard_error',
]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
