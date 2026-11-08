# DevAgent Universal Developer - unified dispatcher.
#
# Wraps the PROTECTED core DevAgent (file/patch/backup/test tools) and ADDS the
# workspace-layer tools (set_workspace, set_target_file, scan/assess folder,
# project map & docs, system snapshots). Exposes a single dispatch surface
# compatible with agent_loop.run_agent_loop (which calls dispatcher.dispatch /
# dispatch_json).
#
# Since the Orchestrators feature, this module also exposes orchestrator
# management tools so DevAgent can create, update and maintain custom
# orchestrators (including their personal folders, custom functions and
# orchestrator-specific instructions).
#
# Single-file mode: when target_file is set, the workspace is the parent
# directory and all scanning/mapping operations are narrowed to that one file.
#
# Nothing here modifies the Inviolable Core. The core DevAgent is composed, not
# changed. This module is itself editable (not protected).

from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config
from . import document_tools as docs
from . import thread_scope as threads_scope
from . import tool_registry as registry
from . import workspace_binding as wb
from . import workspace_tools as wt
from .orchestrator_tools import OrchestratorToolsMixin
from .tool_catalog_data import (  # noqa: F401  (public re-export)
    WORKSPACE_TOOL_ARGS,
    WORKSPACE_TOOL_CATALOG,
)
from .tool_executor import _coerce_numeric_args, workspace_guard_error
from .tool_validation import (  # noqa: F401  (public re-export)
    _workspace_usage,
    validate_tool_call,
)


def load_system_prompt() -> str:
    """Load the combined system prompt from disk.

    The single bundled prompt lives at ``dev_agent/system_prompt.md`` next to
    the agent code. No defaults/ copy is maintained (single source of truth).
    """
    path = config.SYSTEM_PROMPT_FILE
    try:
        if path.is_file():
            return path.read_text(encoding=config.DEFAULT_ENCODING)
    except OSError:
        pass
    return ""


# Extra tools exposed to the LLM, beyond the core file/backup/test tools.

ORCHESTRATOR_TOOL_NAMES = {t["name"] for t in WORKSPACE_TOOL_CATALOG if t["name"].startswith("orchestrator") or t["name"] in ("list_orchestrators", "get_orchestrator", "create_orchestrator", "update_orchestrator", "delete_orchestrator", "reload_orchestrator")}

#: Delegation tools. They are catalog entries, but the agent loop executes
#: them OUTSIDE dispatch - see the concurrency note in core.orchestration.service.
DELEGATION_TOOL_NAMES = ("spawn_agent", "invoke_assistant", "subagent_status",
                         "subagent_result",
                         "read_subagent_state", "resume_subagent", "sleep",
                         "cancel_subagent", "read_subagent_thread",
                         "verify_result", "ask_parent", "answer_child")


def build_assistant_dict_from_config() -> Tuple[dict, dict]:
    """Build TWO assistant-compatible dicts from the DevAgent config KV store.

    Returns ``(strong_assistant, weak_assistant)``. Single-model mode: the
    main model (``strong_service``/``strong_model``, falling back to the
    legacy ``service``/``model`` keys) serves every step, so *weak_assistant*
    is an identical copy of *strong_assistant*. The two-tuple shape is kept
    for backward compatibility.
    """
    from core.config import load_devagent_config
    cfg = load_devagent_config()

    def _make_assistant(svc: str, mdl: str, fallback_svc: str, fallback_mdl: str) -> dict:
        # Resolve service/model: explicit > legacy fallback
        effective_svc = svc or fallback_svc
        effective_mdl = mdl or fallback_mdl
        return {
            "text": cfg.get("prompt_text", ""),
            "service": effective_svc,
            "model": effective_mdl,
            "temperature": float(cfg.get("temperature", 0.2)),
        }

    legacy_svc = cfg.get("service", "")
    legacy_mdl = cfg.get("model", "")

    strong_assistant = _make_assistant(
        cfg.get("strong_service", ""),
        cfg.get("strong_model", ""),
        legacy_svc,
        legacy_mdl,
    )
    # Single-model mode: the weak alias mirrors the main assistant.
    weak_assistant = dict(strong_assistant)
    return strong_assistant, weak_assistant


# Legacy alias (old "skill" terminology).
build_skill_dict_from_config = build_assistant_dict_from_config


class UniversalDevAgent(OrchestratorToolsMixin):
    """Universal-developer dispatch surface = core tools + workspace tools."""

    def __init__(self, workspace: Optional[str] = None, target_file: Optional[str] = None,
                 thread_id: Optional[str] = None):
        # Lazy import to break circular dependency with dev_agent.py
        from .tool_executor import ToolExecutor as DevAgent
        if target_file:
            wt.set_target_file(target_file)
        elif workspace:
            wt.set_workspace(workspace)
        # The core agent reads config.* lazily, so it always targets the
        # currently-selected workspace.
        self.core = DevAgent()
        self.target_file = target_file
        self.thread_id: Optional[str] = (thread_id or "").strip() or None
        if self.thread_id and not wb.has_thread(self.thread_id):
            # Initial binding from the current live config state; the UI
            # re-binds explicitly when restoring a thread from history.
            wb.register_thread(self.thread_id,
                               workspace=str(config.PROJECT_ROOT),
                               target_file=config.TARGET_FILE or None)
        if self.thread_id:
            config.ACTIVE_THREAD_ID = self.thread_id
        self._extra: Dict[str, Callable[..., Dict[str, Any]]] = {
            "set_workspace": lambda **kw: self._set_workspace(**kw),
            "set_target_file": lambda **kw: self._set_target_file(**kw),
            "current_workspace": lambda **kw: wt.current_workspace(**kw),
            "current_install": lambda **kw: wt.current_install(**kw),
            "scan_folder": lambda **kw: wt.scan_folder(**kw),
            "search_in_files": lambda **kw: wt.search_in_files(**kw),
            "assess_workspace": lambda **kw: wt.assess_workspace(**kw),
            "build_project_map": lambda **kw: wt.build_project_map(**kw),
            "write_project_map": lambda **kw: self._write_project_map(**kw),
            "write_doc": lambda **kw: self._write_doc(**kw),
            "read_doc": lambda **kw: self._read_doc(**kw),
            "snapshot_all": lambda **kw: self._snapshot_all(**kw),
            "list_snapshots": lambda **kw: wt.list_snapshots(**kw),
            "restore_all": lambda **kw: self._restore_all(**kw),
            "list_thread_files": lambda **kw: self._list_thread_files(**kw),
            "read_thread_file": lambda **kw: self._read_thread_file(**kw),
            "read_document": lambda **kw: self._read_document(**kw),
            "extract_document_images": lambda **kw: self._extract_document_images(**kw),
            "search_in_threads": lambda **kw: self._search_in_threads(**kw),
            "list_threads": lambda **kw: self._list_threads(**kw),
            "read_thread": lambda **kw: self._read_thread(**kw),
            # Orchestrator management tools
            "list_orchestrators": lambda **kw: self._list_orchestrators(**kw),
            "get_orchestrator": lambda **kw: self._get_orchestrator(**kw),
            "create_orchestrator": lambda **kw: self._create_orchestrator(**kw),
            "update_orchestrator": lambda **kw: self._update_orchestrator(**kw),
            "delete_orchestrator": lambda **kw: self._delete_orchestrator(**kw),
            "reload_orchestrator": lambda **kw: self._reload_orchestrator(**kw),
            "list_orchestrator_functions": lambda **kw: self._list_orchestrator_functions(**kw),
            "get_orchestrator_function": lambda **kw: self._get_orchestrator_function(**kw),
            "save_orchestrator_function": lambda **kw: self._save_orchestrator_function(**kw),
            "delete_orchestrator_function": lambda **kw: self._delete_orchestrator_function(**kw),
            "list_orchestrator_instructions": lambda **kw: self._list_orchestrator_instructions(**kw),
            "get_orchestrator_instruction": lambda **kw: self._get_orchestrator_instruction(**kw),
            "save_orchestrator_instruction": lambda **kw: self._save_orchestrator_instruction(**kw),
            "delete_orchestrator_instruction": lambda **kw: self._delete_orchestrator_instruction(**kw),
            # Connector management tools
            "list_connections": lambda **kw: self._list_connections(**kw),
            "get_connection": lambda **kw: self._get_connection(**kw),
            "list_connector_services": lambda **kw: self._list_connector_services(**kw),
            "list_connection_presets": lambda **kw: self._list_connection_presets(**kw),
            "create_connection": lambda **kw: self._create_connection(**kw),
            "update_connection": lambda **kw: self._update_connection(**kw),
            "delete_connection": lambda **kw: self._delete_connection(**kw),
            "test_connection": lambda **kw: self._test_connection(**kw),
            "get_enabled_connections": lambda **kw: self._get_enabled_connections(**kw),
            "set_enabled_connections": lambda **kw: self._set_enabled_connections(**kw),
            "get_connection_memory": lambda **kw: self._get_connection_memory(**kw),
            "set_connection_memory": lambda **kw: self._set_connection_memory(**kw),
            "get_personal_memory": lambda **kw: self._get_personal_memory(**kw),
            "set_personal_memory": lambda **kw: self._set_personal_memory(**kw),
            "list_personal_memory": lambda **kw: self._list_personal_memory(**kw),
        }

    # ─── system prompt = single file ────────────────────────────────────────
    @property
    def system_prompt(self) -> str:
        """Load the single combined system prompt from disk."""
        return load_system_prompt()

    @property
    def tool_catalog(self) -> List[Dict[str, str]]:
        """Combined catalog: core ToolExecutor tools + workspace-layer tools."""
        from .tool_executor import TOOL_CATALOG as CORE_CATALOG
        return list(CORE_CATALOG) + list(WORKSPACE_TOOL_CATALOG)

    # ─── dispatch ──────────────────────────────────────────────────────────
    def dispatch(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Route a tool call under the binding of THIS dialog thread.

        When the dispatcher knows its thread id and the thread has a bound
        workspace state, the config globals are swapped to that state for the
        duration of the call and restored right after (serialized by the
        workspace-binding RLock). Unbound dispatchers fall back to the legacy
        process-global behavior.
        """
        with wb.thread_context(self.thread_id) as _engaged:
            return self._dispatch_impl(tool_name, args)

    def _current_orchestrator_slug(self) -> str:
        """Return the orchestrator slug this dispatcher currently serves.

        Falls back to 'dev_agent' when no orchestrator has been attached
        (the core executor default).
        """
        slug = getattr(self.core, "_orchestrator_slug", "dev_agent")
        return (str(slug or "dev_agent").strip()) or "dev_agent"

    def _is_disabled_for_orchestrator(self, tool_name: str) -> bool:
        """Return True when the active orchestrator has this tool disabled.

        Reads the ``disabled_tools`` config of the orchestrator served by this
        dispatcher and checks both the raw name and legacy aliases. Best
        effort: on any read error the tool is treated as enabled so a config
        failure can never break dispatching.
        """
        try:
            from core.orchestrator_tools import is_tool_disabled
            from core.orchestrators import get_disabled_tools
            disabled = set(get_disabled_tools(self._current_orchestrator_slug()))
            return is_tool_disabled(tool_name, disabled)
        except Exception:
            return False

    def _disabled_tool_error(self, tool_name: str) -> Dict[str, Any]:
        """Return the structured error for a call to a disabled tool."""
        return {
            "ok": False,
            "error": (
                f"Tool '{tool_name}' is disabled for orchestrator "
                f"'{self._current_orchestrator_slug()}' via its "
                "'disabled_tools' setting. Do not call it."
            ),
            "disabled": True,
        }

    def _dispatch_impl(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Route to core ToolExecutor or an extra workspace tool.

        Catalog tools are validated against WORKSPACE_TOOL_ARGS first: unknown
        argument names produce a structured error (``unknown_args`` +
        ``suggestion``) telling the caller to use only the standard arguments
        from the system prompt. Custom orchestrator functions are NOT listed
        there and keep their arbitrary-kwargs contract.

        The two delegation tools are NOT executed here: ``spawn_agent`` and
        ``invoke_assistant`` are run by the agent loop BEFORE entering this
        method, because a child started inside ``dispatch`` would wait for the
        binding lock while the parent holds it. A dispatch call for them only
        happens when the loop did not intercept the call (e.g. an external
        caller) - then the call is refused with a pointer to the loop.
        """
        if not isinstance(args, dict):
            return {"ok": False, "error": f"args must be a dict, got {type(args).__name__}"}
        refusal = validate_tool_call(tool_name, args, self._current_orchestrator_slug(),
                                     extra_disabled=getattr(self, "child_disabled_tools", None))
        if refusal is not None:
            return refusal
        if tool_name in DELEGATION_TOOL_NAMES:
            return {
                "ok": False,
                "error": (
                    f"Tool '{tool_name}' is executed by the agent loop, not by "
                    "the dispatcher. Re-issue the call through the loop."
                ),
                "error_code": "invalid_args",
                "loop_only": True,
            }
        guard = workspace_guard_error(tool_name, args)
        if guard is not None:
            return guard
        if tool_name in self._extra:
            args = _coerce_numeric_args(self._extra[tool_name], args)
            result = self._extra[tool_name](**args)
            # Tool-result size policy (context-overflow protection):
            # custom functions and connection tools go through the same
            # spill/preview/cap path so no single payload can blow up the
            # model context.
            try:
                from dev_agent.agent_loop import _apply_tool_result_cap
                return _apply_tool_result_cap(result, tool_name)
            except Exception:
                return result
        return self.core.dispatch(tool_name, args)

    # ─── custom orchestrator function attachment ────────────────────────────
    def attach_orchestrator(self, slug: str) -> None:
        """Load custom Python functions of an orchestrator into this dispatcher.

        Each custom function becomes a tool named exactly like the function
        name (e.g. ``calculate_metrics``). The original orchestrator tools
        remain available. If the same name already exists (e.g. from a previous
        attach), it is overwritten.
        """
        from core.orchestrator_folders import load_all_orchestrator_functions
        functions = load_all_orchestrator_functions(slug)
        # set_orchestrator_slug (not a bare assignment) so the working-memory
        # root is refreshed together with the slug: ``memory:`` must resolve to
        # THIS employee's data folder.
        if hasattr(self.core, "set_orchestrator_slug"):
            self.core.set_orchestrator_slug(slug)
        else:
            self.core._orchestrator_slug = slug
        for fname, fn in functions.items():
            self._extra[fname] = fn
        self._attach_connection_tools(slug)

    _CONNECTION_TOOL_NAMES_REST = (
        "ghr_list_repos",
        "ghr_create_repo",
        "ghr_read_file",
        "ghr_upload_file",
        "ghr_update_file",
        "ghr_delete_file",
        "ghr_list_files",
        "ghr_test_connection",
        "ghr_get_repo_info",
        "ghr_read_file_meta",
        "ghr_get_ref",
        "ghr_get_commit",
        "ghr_get_tree",
        "ghr_create_ref",
        "ghr_create_tag",
        "ghr_batch_commit",
        "ghr_batch_commit_paths",
        "ghr_batch_upsert",
    )

    _CONNECTION_TOOL_NAMES_SSH = (
        "ssh_test_connection",
        "ssh_exec",
        "ssh_list_dir",
        "ssh_read_file",
        "ssh_upload_file",
        "ssh_write_file",
    )

    _CONNECTION_TOOL_NAMES_MCP = (
        "mcp_list_tools",
        "mcp_call_tool",
    )

    def _pop_mcp_tools(self) -> None:
        """Remove the static MCP tools and every dynamic ``mcp__*`` tool.

        The ``mcp__`` prefix is reserved for MCP dynamic tools (ADR-001
        rev. 2 p. 5), so prefix-based cleanup is safe here.
        """
        for name in self._CONNECTION_TOOL_NAMES_MCP:
            self._extra.pop(name, None)
        for name in [n for n in self._extra if n.startswith("mcp__")]:
            self._extra.pop(name, None)

    def _attach_connection_tools(self, slug: str) -> None:
        """Register/unregister built-in connection tools for an orchestrator.

        Connection tools (GitHub REST, SSH, MCP) are only made callable when
        the orchestrator has enabled connections in its config. They are
        removed when the orchestrator disables all connections so stale
        dispatchers do not keep accepting connection calls.
        """
        try:
            from core.connectors import get_connection
            from core.orchestrators import get_enabled_connections
            enabled = get_enabled_connections(slug)
            if not enabled:
                registry.drop_family(self._extra, self._CONNECTION_TOOL_NAMES_REST)
                registry.drop_family(self._extra, self._CONNECTION_TOOL_NAMES_SSH)
                self._pop_mcp_tools()
                return
            services = registry.enabled_services(enabled, get_connection)
            self._attach_service_family(
                slug, services, "github_rest",
                self._CONNECTION_TOOL_NAMES_REST, "core.github_tools_rest")
            self._attach_service_family(
                slug, services, "ssh",
                self._CONNECTION_TOOL_NAMES_SSH, "core.ssh_tools")
            self._attach_service_family(
                slug, services, "mcp",
                self._CONNECTION_TOOL_NAMES_MCP, "core.mcp_tools")
        except Exception:
            # Best effort: never break orchestrator attachment on library errors.
            pass

    def _attach_service_family(self, slug: str, services: set, service: str,
                               names: tuple, module_path: str) -> None:
        """Register one family of connection tools, or drop it when disabled.

        The module is imported only when its service is enabled, so an
        orchestrator without SSH never pays for loading the SSH client. The
        MCP family also carries the per-connection dynamic tools, which are
        rebuilt from the live server catalog.
        """
        if service not in services:
            registry.drop_family(self._extra, names)
            if service == "mcp":
                self._pop_mcp_tools()
            return
        module = importlib.import_module(module_path)
        registry.register_family(self._extra, names, module)
        if service == "mcp":
            self._extra.update(module.build_dynamic_tools(slug))

    def attach_orchestrator_catalog(self, slug: str) -> None:
        """Add the orchestrator's custom functions to the tool catalog.

        This makes the LLM aware of the custom tools during function-calling.
        Requires the orchestrator's bundle (orchestrator.json) to be present so
        we can read the function descriptions from the folder.
        """
        from core.orchestrator_folders import list_orchestrator_functions
        for meta in list_orchestrator_functions(slug):
            entry = {
                "name": meta["name"],
                "desc": f"Custom function of orchestrator '{slug}'. Accepts arbitrary kwargs; returns {"ok": bool, ...}.",
            }
            # Avoid duplicate entries with the same name.
            existing = [t for t in WORKSPACE_TOOL_CATALOG if t["name"] == meta["name"]]
            if existing:
                WORKSPACE_TOOL_CATALOG.remove(existing[0])
            WORKSPACE_TOOL_CATALOG.append(entry)

    def dispatch_json(self, call: Dict[str, Any]) -> Dict[str, Any]:
        """JSON-compatible dispatch: takes {"tool": ..., "args": {...}}.

        Safe: returns an error dict when 'tool' key is missing or call is not a dict.
        """
        if not isinstance(call, dict) or "tool" not in call:
            return {
                "ok": False,
                "error": "dispatch_json requires a dict with 'tool' key, got: " + repr(call)[:200]
            }
        return self.dispatch(call["tool"], call.get("args", {}))

    def _recreate_core_preserving_history(self) -> None:
        """Create a fresh ToolExecutor but copy the old history cache over.

        Called when set_workspace / set_target_file recreates the core
        (because config.PROJECT_ROOT changed). We must preserve the
        economy-mode history cache so get_history_index / get_history_messages
        still see the full dialogue history.
        """
        from .tool_executor import ToolExecutor
        old_history = list(self.core._history) if self.core else []
        old_send_request_fn = self.core._send_request_fn if self.core else None
        old_web_search_enabled = self.core._web_search_enabled if self.core else False
        old_orch_slug = getattr(self.core, "_orchestrator_slug", "dev_agent") if self.core else "dev_agent"
        old_web_search_config = getattr(self.core, "_web_search_config", None) if self.core else None
        self.core = ToolExecutor()
        self.core.set_history(old_history)
        self.core._send_request_fn = old_send_request_fn
        self.core._web_search_enabled = old_web_search_enabled
        if hasattr(self.core, "set_orchestrator_slug"):
            self.core.set_orchestrator_slug(old_orch_slug)
        else:
            self.core._orchestrator_slug = old_orch_slug
        self.core._web_search_config = old_web_search_config

    # ─── workspace-tool wrappers ───────────────────────────────────────────
    def set_thread_id(self, thread_id: Optional[str]) -> None:
        """Bind this dispatcher to a dialog thread (called by the UI).

        Registers the thread in the workspace-binding registry using the
        current live config state and publishes the id for the journal layer.
        A None/empty id detaches the dispatcher (legacy process-global mode).
        """
        tid = (thread_id or "").strip() or None
        self.thread_id = tid
        if tid:
            # Never overwrite an existing binding from the live config: the
            # process-global root may momentarily belong to a neighbouring
            # dialog, and rebinding from it would leak that folder into this
            # thread. A binding is created only for a thread that has none
            # yet (the fresh dispatcher of a brand-new thread) - for a new
            # dialog that live state is the neutral empty state.
            if not wb.has_thread(tid):
                wb.register_thread(tid,
                                   workspace=str(config.PROJECT_ROOT),
                                   target_file=config.TARGET_FILE or None)
            config.ACTIVE_THREAD_ID = tid

    def _persist_thread_workspace(self) -> None:
        """Save the current workspace/target_file into the thread's DB meta.

        Best-effort: a persistence failure must never break the switch. The
        empty state is persisted as CLEARED fields (empty workspace) so the
        neutral root is never recorded as a chosen folder, and switching a
        thread back to folder mode drops any stale single-file target.
        """
        if not self.thread_id:
            return
        try:
            from core.threads_devagent import save_thread_workspace
            selected = bool(getattr(config, "WORKSPACE_SELECTED", True))
            save_thread_workspace(
                self.thread_id,
                workspace=str(config.PROJECT_ROOT) if selected else "",
                target_file=((config.TARGET_FILE or "") if selected else ""),
            )
        except Exception:
            pass

    def _sync_binding_after_switch(self) -> None:
        """After a workspace switch: refresh the registry + DB binding."""
        if self.thread_id:
            wb.sync_registry_from_config(self.thread_id)
        config.ACTIVE_THREAD_ID = self.thread_id or ""

    def _set_workspace(self, path: str, **kwargs) -> Dict[str, Any]:
        result = wt.set_workspace(path, slug=self._current_orchestrator_slug())
        if result.get("ok"):
            self.target_file = None
            self._sync_binding_after_switch()
            self._persist_thread_workspace()
        self._recreate_core_preserving_history()
        return result

    def _set_target_file(self, file_path: str, **kwargs) -> Dict[str, Any]:
        result = wt.set_target_file(file_path, slug=self._current_orchestrator_slug())
        if result.get("ok"):
            self.target_file = result.get("target_file")
            self._sync_binding_after_switch()
            self._persist_thread_workspace()
        self._recreate_core_preserving_history()
        return result

    def _write_project_map(self, responsibilities: Dict[str, str], **kwargs) -> Dict[str, Any]:
        return wt.write_project_map(responsibilities)

    def _write_doc(self, doc: str, content: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        return wt.write_doc(doc, content)

    def _read_doc(self, doc: str, **kwargs) -> Dict[str, Any]:
        return wt.read_doc(doc)

    def _snapshot_all(self, note: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        return wt.snapshot_all(note)

    def _restore_all(self, snapshot_id: str | int, **kwargs) -> Dict[str, Any]:
        return wt.restore_all(str(snapshot_id))

    # ─── dialog upload tool wrappers (history/<tid>/files) ─────────────────

    @staticmethod
    def _active_thread_id() -> str:
        """Return the active dialog thread id published by the agent loop."""
        from dev_agent import config as _dagent_config
        return (getattr(_dagent_config, "ACTIVE_THREAD_ID", "") or "").strip()

    def _list_thread_files(self, **kwargs) -> Dict[str, Any]:
        from core.threads_devagent import list_thread_files
        tid = self._active_thread_id()
        if not tid:
            return {"ok": False, "error": "No active dialog thread: cannot list dialog uploads."}
        records = list_thread_files(tid)
        return {"ok": True, "thread_id": tid, "count": len(records), "files": records}

    def _read_thread_file(self, file_name: str = "", offset: Optional[int] = None,
                          limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
        from core.threads_devagent import read_thread_file
        tid = self._active_thread_id()
        if not tid:
            return {"ok": False, "error": "No active dialog thread: cannot read dialog uploads."}
        if not file_name:
            return {"ok": False, "error": "Missing required argument 'file_name'."}
        # The dispatch layer cannot coerce through the **kwargs lambda, so cast
        # stringified numerics here (e.g. {"offset": "2"} -> 2).
        if offset is not None and not isinstance(offset, int):
            offset = int(str(offset))
        if limit is not None and not isinstance(limit, int):
            limit = int(str(limit))
        return read_thread_file(tid, str(file_name), offset=offset, limit=limit)

    def _read_document(self, path: str = "", offset: Optional[int] = None,
                       limit: Optional[int] = None,
                       max_chars: Optional[int] = None, **kwargs) -> Dict[str, Any]:
        """Extract a document from disk by path and return a line window.

        The path may be absolute or relative to the active workspace. The
        extractor (``core.document_text``) covers PDF, DOCX, DOC, PPTX, XLSX,
        XLS, RTF, ODT, HTML and plain text, so the orchestrator never needs
        ``run_code`` for a document. Single-file mode is honoured: only the
        active target file may be read there.
        """
        from core.document_text import read_document_path
        target = str(path or "").strip()
        if not target:
            return {"ok": False, "error": "Missing required argument 'path'."}
        for name, value in (("offset", offset), ("limit", limit),
                            ("max_chars", max_chars)):
            if value is not None and not isinstance(value, int):
                try:
                    if name == "offset":
                        offset = int(str(value))
                    elif name == "limit":
                        limit = int(str(value))
                    else:
                        max_chars = int(str(value))
                except (TypeError, ValueError):
                    return {"ok": False, "error": "%s must be an integer" % name}
        resolved = self._resolve_document_path(target)
        if resolved.get("ok") is False:
            return resolved
        result = read_document_path(
            resolved["path"], offset=offset or 0, limit=limit, max_chars=max_chars,
        )
        result["requested_path"] = target
        return result

    def _extract_document_images(self, path: str = "", file_name: str = "",
                                 output_dir: str = "", max_images=None,
                                 min_px=None, ocr: str = "off",
                                 pages=None, dpi=None, **kwargs) -> Dict[str, Any]:
        """Extract the pictures embedded in a document and save them to disk.

        Exactly one source is accepted: *path* (a document in the workspace
        or an absolute path) or *file_name* (an upload of the active dialog).
        The saved files live inside the project, so ``analyze_image`` can read
        them back by path; recognition itself stays a separate call.

        Args:
            path / file_name: the document to read.
            output_dir: workspace-relative folder; defaults to
                ``.dev_agent/doc_images/<name>-<digest>/``.
            max_images: cap on extracted pictures (default from the module).
            min_px: drop pictures whose longer side is smaller (icons).
            ocr: ``off`` extracts stored pictures only, ``auto`` renders the
                pages of a PDF without a text layer, ``force`` renders the
                pages of any PDF.
            pages: 1-based page numbers for rendering.
            dpi: render resolution (72-400).
        Returns:
            ``{"ok": True, ...}`` with the saved paths, a manifest and ready
            batches, or ``{"ok": False, "error": ...}`` with an actionable
            message.
        """
        from core import document_images as images_module

        source = docs.select_source(path, file_name)
        if source.get("ok") is False:
            return source
        resolved = docs.resolve_document(source, self._active_thread_id,
                                         self._resolve_document_path)
        if resolved.get("ok") is False:
            return resolved
        resolved_path = resolved["path"]
        display_name = resolved["name"]

        refusal = docs.check_supported(display_name, images_module.is_supported,
                                       images_module.IMAGE_EXTENSIONS)
        if refusal is not None:
            return refusal
        raw, refusal = docs.read_document_bytes(resolved_path)
        if refusal is not None:
            return refusal

        options = docs.parse_options(max_images, min_px, pages, dpi)
        limit = options["limit"]
        found, warnings = images_module.extract_images(
            display_name, raw, **docs.extract_kwargs(limit))

        should_render, warnings = docs.decide_render(
            ocr, display_name, raw, found, warnings, images_module)
        rendered = 0
        if should_render:
            page_images, render_warnings = images_module.render_pdf_pages(
                display_name, raw,
                **docs.build_render_kwargs(options["pages"], options["dpi"], limit))
            warnings.extend(render_warnings)
            found, rendered = docs.merge_renders(found, page_images, warnings)

        found = docs.drop_small(found, options["min_px"], warnings)

        if not found:
            return docs.empty_result(display_name, len(raw), warnings,
                                     self._vision_available())

        ordered = images_module.priority_order(found)
        if limit:
            ordered = ordered[:limit]
        folder = self._document_images_dir(resolved_path, output_dir)
        if folder.get("ok") is False:
            return folder
        written = images_module.save_images(ordered, folder["path"],
                                           prefix=folder["prefix"])
        failed = [record for record in written if record.get("error")]
        saved = [record for record in written if not record.get("error")]
        if failed and not saved:
            return {"ok": False, "error": failed[0]["error"]}

        available = self._vision_available()
        batches = docs.group_batches(saved, docs.batch_size())
        result = docs.success_result(
            display_name, len(raw), resolved_path, folder["path"], saved,
            ordered, rendered, batches, available, warnings, images_module)
        if failed:
            result["warnings"] = warnings + [entry["error"] for entry in failed]
        return result

    def _vision_available(self) -> bool:
        """Return True when a vision model is assigned and usable."""
        try:
            from core.multimodal import VISION, resolve_model
        except Exception:
            return False
        return docs.vision_available(resolve_model, VISION)

    def _document_images_dir(self, document_path: str,
                             output_dir: str = "") -> Dict[str, Any]:
        """Return the folder for extracted pictures of one document.

        Without *output_dir* the folder is
        ``<workspace>/.dev_agent/doc_images/<name>-<digest>/`` - inside the
        project, so ``analyze_image`` can read the files back, and outside the
        normal file listing, so the project tree stays clean. An explicit
        *output_dir* must stay inside the project.
        """
        from dev_agent import config as dagent_config
        return docs.document_images_dir(
            document_path, output_dir,
            str(getattr(dagent_config, "PROJECT_ROOT", "") or ""),
            dagent_config.resolve_in_project)

    def _resolve_document_path(self, target: str) -> Dict[str, Any]:
        """Resolve a document path argument to an absolute path inside the scope.

        An absolute path is used as is (``read_document`` is meant for files
        the model already knows, including dialog uploads outside the project
        folder). A relative path is resolved against the active workspace.
        Single-file mode restricts the read to the active target file.
        """
        from dev_agent import config as dagent_config
        return docs.resolve_document_path(
            target,
            str(getattr(dagent_config, "PROJECT_ROOT", "") or ""),
            str(getattr(dagent_config, "TARGET_FILE", "") or ""))

    # ─── thread-search tool wrappers (dialog history) ──────────────────────

    def _thread_access_slugs(self):
        """Return ``allowed_slugs`` for the thread tools, or None.

        DevAgent (the built-in orchestrator) sees every dialog; any other
        orchestrator is restricted to its own dialogs. ``None`` means "no
        restriction" for the service layer.
        """
        slug = self._current_orchestrator_slug()
        try:
            from core.orchestrators import DEVAGENT_SLUG
            devagent_slug = str(DEVAGENT_SLUG or "dev_agent")
        except Exception:
            devagent_slug = "dev_agent"
        if not slug or slug == devagent_slug:
            return None
        return [slug]

    @staticmethod
    def _normalize_orchestrator_arg(orchestrator):
        """Normalize the ``orchestrator`` argument to a list of slugs.

        Accepts a single slug, a comma-separated string of slugs, or a list
        of slugs. Returns ``(slugs, error)``; slugs is None when the
        argument is empty, error is a message for an invalid type.
        """
        if not orchestrator:
            return None, None
        if isinstance(orchestrator, str):
            slugs = [s.strip() for s in orchestrator.split(",") if s.strip()]
        elif isinstance(orchestrator, (list, tuple)):
            slugs = [str(s).strip() for s in orchestrator if str(s).strip()]
        else:
            return None, "'orchestrator' must be a slug or a list of slugs."
        return (slugs or None), None

    @staticmethod
    def _as_bool(value, default=False) -> bool:
        """Coerce a stringified boolean tool argument to a real bool."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        low = str(value).strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
        return default

    def _search_in_threads(self, query: str = "", thread_id: str = None,
                           scope: str = None, orchestrator=None,
                           date_from=None, date_to=None,
                           date_field: str = "updated", role: str = None,
                           regex=False, include_tool_results=False,
                           max_results=50, **kwargs) -> Dict[str, Any]:
        """Search dialog messages; the CURRENT dialog is the default target.

        Targeting priority: an explicit ``thread_id`` (no listing needed),
        then the current dialog, then ``scope='orchestrator'`` (the given
        slug(s) or the active orchestrator), then ``scope='all'``.
        """
        from core.threads_devagent import search_thread_messages
        q = str(query or "").strip()
        if not q:
            return {"ok": False, "error": "Missing required argument 'query'."}
        orch_slugs, orch_error = self._normalize_orchestrator_arg(orchestrator)
        if orch_error:
            return {"ok": False, "error": orch_error}

        scope_norm, scope_error = threads_scope.normalize_scope(
            scope, thread_id, orch_slugs)
        if scope_error:
            return {"ok": False, "error": scope_error}

        active_tid = self._active_thread_id()
        thread_ids, slugs, target_error = threads_scope.select_targets(
            scope_norm, thread_id, active_tid, orch_slugs,
            self._current_orchestrator_slug())
        if target_error:
            return {"ok": False, "error": target_error}

        res = search_thread_messages(
            q,
            thread_ids=thread_ids,
            slugs=slugs,
            date_from=date_from,
            date_to=date_to,
            date_field=str(date_field or "updated").strip().lower(),
            role=(str(role).strip().lower() or None) if role else None,
            regex=self._as_bool(regex, False),
            include_tool_results=self._as_bool(include_tool_results, False),
            max_results=max_results,
            allowed_slugs=self._thread_access_slugs(),
        )
        return threads_scope.apply_active_thread(res, active_tid)

    def _list_threads(self, orchestrator=None, date_from=None, date_to=None,
                      date_field: str = "updated", limit=50, offset=0,
                      order: str = "desc", **kwargs) -> Dict[str, Any]:
        """List dialog threads with metadata and per-dialog message counters."""
        from core.threads_devagent import list_threads_filtered
        orch_slugs, orch_error = self._normalize_orchestrator_arg(orchestrator)
        if orch_error:
            return {"ok": False, "error": orch_error}
        res = list_threads_filtered(
            slugs=orch_slugs,
            date_from=date_from,
            date_to=date_to,
            date_field=str(date_field or "updated").strip().lower(),
            limit=limit,
            offset=offset,
            order=str(order or "desc").strip().lower(),
            allowed_slugs=self._thread_access_slugs(),
        )
        if isinstance(res, dict) and res.get("ok"):
            res["active_thread_id"] = self._active_thread_id() or ""
        return res

    def _read_thread(self, thread_id: str = None, offset=0, limit=50,
                     include_tool_results=False, **kwargs) -> Dict[str, Any]:
        """Read a window of one dialog; the CURRENT dialog is the default."""
        from core.threads_devagent import read_thread_window
        active_tid = self._active_thread_id()
        tid = str(thread_id or "").strip()
        if not tid:
            if not active_tid:
                return {"ok": False,
                        "error": "No active dialog thread: pass thread_id."}
            tid = active_tid
        res = read_thread_window(
            tid,
            offset=offset,
            limit=limit,
            include_tool_results=self._as_bool(include_tool_results, False),
            allowed_slugs=self._thread_access_slugs(),
        )
        if isinstance(res, dict) and res.get("ok"):
            res["active_thread_id"] = active_tid or ""
        return res

    # ─── connector tool wrappers ───────────────────────────────────────────

    def _list_connections(self, **kwargs) -> Dict[str, Any]:
        from core.connectors import list_connections
        conns = list_connections()
        return {"ok": True, "count": len(conns), "connections": conns}

    def _get_connection(self, conn_id: str = "", **kwargs) -> Dict[str, Any]:
        from core.connectors import get_connection
        if not conn_id:
            return {"ok": False, "error": "Missing required argument 'conn_id'."}
        conn = get_connection(conn_id)
        if not conn:
            return {"ok": False, "error": f"Connection not found: {conn_id}"}
        return {"ok": True, "connection": conn}

    def _list_connector_services(self, **kwargs) -> Dict[str, Any]:
        from core.connectors import list_services
        return {"ok": True, "services": list_services()}

    def _list_connection_presets(self, service: str = "mcp",
                                 **kwargs) -> Dict[str, Any]:
        """List bundled connection presets (public views, no secrets)."""
        try:
            from core import connector_presets
            presets = connector_presets.list_presets(
                str(service or "mcp").strip().lower()
            )
            return {"ok": True, "presets": presets}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _create_connection(self, service: str = "", name: str = "", token: str = "",
                           account: str = "", config: dict = None, secrets: dict = None,
                           pending: bool = False, preset: str = "",
                           **kwargs) -> Dict[str, Any]:
        if preset:
            # A preset instantiates a DRAFT (pending) connection with the
            # preset config and memory seed; credentials are left to the user.
            try:
                from core import connector_presets
                result = connector_presets.instantiate_preset(
                    str(preset).strip(),
                    service=str(service or "mcp").strip().lower(),
                    name=name or "",
                )
            except Exception as e:
                return {
                    "ok": False,
                    "error": str(e) or "Preset was not instantiated",
                }
            return {
                "ok": True,
                "connection": result["connection"],
                "preset": result["preset"],
            }
        from core.connectors import create_connection
        if not service or not name:
            return {"ok": False, "error": "Both 'service' and 'name' are required."}
        try:
            created = create_connection(
                service, name, token,
                account=account or "",
                config=config if isinstance(config, dict) else None,
                secrets=secrets if isinstance(secrets, dict) else None,
                pending=self._as_bool(pending, False),
            )
        except Exception as e:
            return {"ok": False, "error": str(e) or "Connection was not created"}
        return {"ok": True, "connection": created}

    def _update_connection(self, conn_id: str = "", **kwargs) -> Dict[str, Any]:
        from core.connectors import update_connection
        if not conn_id:
            return {"ok": False, "error": "Missing required argument 'conn_id'."}
        allowed = {"name", "account", "token", "config", "secrets"}
        update_kwargs = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        try:
            updated = update_connection(conn_id, **update_kwargs)
        except Exception as e:
            return {"ok": False, "error": str(e) or "Connection was not updated"}
        return {"ok": True, "connection": updated}

    def _delete_connection(self, conn_id: str = "", **kwargs) -> Dict[str, Any]:
        from core.connectors import delete_connection
        if not conn_id:
            return {"ok": False, "error": "Missing required argument 'conn_id'."}
        deleted = delete_connection(conn_id)
        if not deleted:
            return {"ok": False, "error": f"Failed to delete connection: {conn_id}"}
        return {"ok": True, "conn_id": conn_id, "deleted": True}

    def _test_connection(self, conn_id: str = "", **kwargs) -> Dict[str, Any]:
        from core import connectors
        if not conn_id:
            return {"ok": False, "error": "Missing required argument 'conn_id'."}
        try:
            conn = connectors.get_connection(conn_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        service = str((conn or {}).get("service") or "")
        try:
            if service == "ssh":
                from core.ssh_connector import test_connection as _ssh_test
                result = _ssh_test(conn_id)
            elif service == "github_rest":
                from core.github_connector_rest import test_connection as _gh_test
                result = _gh_test(conn_id)
            else:
                return {"ok": False, "error": f"Unsupported service for testing: {service or '?'}"}
        except Exception as e:
            return {"ok": False, "error": str(e) or "Connection test failed"}
        return {"ok": True, "result": result}

    def _get_enabled_connections(self, orchestrator: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import get_enabled_connections
        slug = str(orchestrator or "").strip() or self._current_orchestrator_slug()
        return {"ok": True, "orchestrator": slug, "connection_ids": get_enabled_connections(slug)}

    def _set_enabled_connections(self, connection_ids, orchestrator: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import set_enabled_connections
        slug = str(orchestrator or "").strip() or self._current_orchestrator_slug()
        ids = list(connection_ids) if isinstance(connection_ids, (list, tuple)) else []
        if connection_ids and not ids:
            return {"ok": False, "error": "'connection_ids' must be a list of connection ids."}
        ok = set_enabled_connections(slug, ids)
        if not ok:
            return {"ok": False, "error": f"Failed to update enabled connections for orchestrator: {slug}"}
        return {"ok": True, "orchestrator": slug, "connection_ids": ids}

    # ─── Personal memory ───────────────────────────────────────────────────
    def _personal_scope(self, scope: str = "") -> str:
        """Resolve a user-supplied scope name to its canonical form.

        ``'current'`` and an empty value both mean the employee's own scope; a
        bare ``'project'`` means the active working folder. Permission checks
        are not applied here - the memory layer owns that policy.
        """
        from core import personal_memory as pm

        text = (scope or "").strip()
        slug = self._current_orchestrator_slug()
        if not text or text.lower() == "current":
            return pm.format_scope(pm.SCOPE_ORCHESTRATOR, slug)
        kind, key = (pm.SCOPE_PROJECT, "") if text.lower() in ("project", "project:") \
            else pm.parse_scope(text)
        if kind == pm.SCOPE_PROJECT and not key:
            workspace = str(getattr(config, "PROJECT_ROOT", "") or "")
            if not getattr(config, "WORKSPACE_SELECTED", True) or not workspace:
                raise ValueError(
                    "No working folder is selected, so 'project' has no target")
            return pm.format_scope(pm.SCOPE_PROJECT, workspace)
        return pm.format_scope(kind, key)

    def _get_personal_memory(self, scope: str = "", max_chars: int = 0,
                             **kwargs) -> Dict[str, Any]:
        """Read one memory scope on behalf of the active employee."""
        from core import personal_memory as pm
        try:
            resolved = self._personal_scope(scope)
            limit = int(max_chars or 0)
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": str(e) or "Invalid scope"}
        try:
            result = pm.read_memory(resolved, max_chars=limit,
                                    caller_slug=self._current_orchestrator_slug())
        except (PermissionError, ValueError) as e:
            return {"ok": False, "scope": resolved, "error": str(e)}
        out: Dict[str, Any] = {"ok": True}
        out.update(result)
        return out

    def _set_personal_memory(self, content: str = "", scope: str = "",
                             mode: str = "replace", note: str = "",
                             overwrite_owner_profile: bool = False,
                             **kwargs) -> Dict[str, Any]:
        """Write one memory scope for the active employee.

        The owner profile (``user``) is a SHARED card: every employee reads it
        as rules and preferences, so a full rewrite by one employee would
        silently drop what the others recorded. Extending it with ``append`` is
        allowed; ``replace`` needs ``overwrite_owner_profile=True``, which is
        passed only when the owner explicitly asked for a rewrite.
        """
        from core import personal_memory as pm
        chosen = (mode or "replace").strip().lower()
        if chosen not in ("replace", "append"):
            return {"ok": False,
                    "error": "'mode' must be 'replace' or 'append', got " + repr(mode)}
        try:
            resolved = self._personal_scope(scope)
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": str(e) or "Invalid scope"}

        caller = self._current_orchestrator_slug()
        refusal = self._owner_profile_refusal(
            pm, resolved, chosen,
            registry.normalize_overwrite(overwrite_owner_profile))
        if refusal is not None:
            return refusal

        text = str(content if content is not None else "")
        if chosen == "append":
            try:
                existing = pm.read_memory(resolved, caller_slug=caller)
            except (PermissionError, ValueError) as e:
                return {"ok": False, "scope": resolved, "error": str(e)}
            text = registry.merge_append(existing.get("content") or "", text)

        try:
            result = pm.write_memory(resolved, text, updated_by=caller,
                                     caller_slug=caller)
        except (PermissionError, ValueError) as e:
            return {"ok": False, "scope": resolved, "error": str(e)}
        out: Dict[str, Any] = {"ok": True, "mode": chosen}
        if note:
            out["note"] = note
        out.update(result)
        return out

    def _owner_profile_refusal(self, pm, resolved: str, mode: str,
                               overwrite: bool) -> Optional[Dict[str, Any]]:
        """Return the refusal of an unauthorised owner-profile rewrite, or None.

        Every employee reads the owner profile as rules and preferences, so a
        replacement that was not explicitly requested would drop what the
        others recorded. Appending is always allowed.
        """
        is_owner = resolved == pm.format_scope(pm.SCOPE_USER)
        if not (is_owner and mode == "replace" and not overwrite):
            return None
        return {
            "ok": False,
            "scope": resolved,
            "error": ("The owner profile is shared by every employee; a full "
                      "rewrite would drop what the others recorded. Use "
                      "mode='append' to extend it. A rewrite is allowed only "
                      "when the owner asked for it, with "
                      "overwrite_owner_profile=true."),
        }

    def _list_personal_memory(self, **kwargs) -> Dict[str, Any]:
        """List the memory scopes that hold notes."""
        from core import personal_memory as pm
        try:
            scopes = pm.list_memory_scopes()
        except Exception as e:
            return {"ok": False, "error": str(e) or "Memory scopes are unavailable"}
        return {"ok": True, "count": len(scopes), "scopes": scopes}

    def _get_connection_memory(self, conn_id: str = "", **kwargs) -> Dict[str, Any]:
        from core.connectors import get_connection_memory
        if not conn_id:
            return {"ok": False, "error": "Missing required argument 'conn_id'."}
        return {"ok": True, "conn_id": conn_id, "content": get_connection_memory(conn_id)}

    def _set_connection_memory(self, conn_id: str = "", content: str = "", **kwargs) -> Dict[str, Any]:
        from core.connectors import set_connection_memory
        if not conn_id:
            return {"ok": False, "error": "Missing required argument 'conn_id'."}
        try:
            ok = set_connection_memory(conn_id, str(content if content is not None else ""))
        except Exception as e:
            return {"ok": False, "error": str(e) or "Memory was not saved"}
        return {"ok": True, "conn_id": conn_id, "saved": ok}

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
