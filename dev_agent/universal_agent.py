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

from typing import Any, Dict, List, Optional, Callable, Tuple

from . import config
from . import workspace_tools as wt
from .tool_executor import _coerce_numeric_args


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
WORKSPACE_TOOL_CATALOG: List[Dict[str, str]] = [
    {"name": "set_workspace", "desc": "Select the target work folder. Args: path."},
    {"name": "set_target_file", "desc": "Activate single-file mode. Workspace = parent dir, all operations scoped to this file. Args: file_path."},
    {"name": "current_workspace", "desc": "Return the active workspace root, single-file mode flag, and target_file if any. No args."},
    {"name": "current_install", "desc": "Return the SagaAI install root (where the dev_agent package lives), its apps/ folder path, and whether the workspace is the install. Use for platform-level paths, e.g. creating a new project under <install>/apps/<name>. No args."},
    {"name": "scan_folder", "desc": "Inspect the workspace (files, languages, docs present). In single-file mode returns only the target file. No args."},
    {"name": "search_in_files", "desc": "Search text files in the workspace for a string or regex. Args: query, [files], [path], [subdir], [extensions], [regex], [case_sensitive], [max_results], [context_before], [context_after]. files is a list of relative file paths to scan (only those files, extension filtering ignored). path narrows the scan to one file or acts as the base directory."},
    {"name": "assess_workspace", "desc": "Classify workspace: empty | software_without_docs | software_with_docs | single_file. No args."},
    {"name": "build_project_map", "desc": "Build the deterministic structural map (files, symbols, deps). In single-file mode maps only the target file. No args."},
    {"name": "write_project_map", "desc": "Write PROJECT_MAP.md. Args: responsibilities (map path->one-line role)."},
    {"name": "write_doc", "desc": "Create/overwrite a markdown doc. Args: doc ('spec'|'architecture'), content."},
    {"name": "read_doc", "desc": "Read a managed doc. Args: doc ('map'|'spec'|'architecture'|'changelog')."},
    {"name": "snapshot_all", "desc": "Full-system backup of every project file. In single-file mode backs up only the target file. Args: [note]."},
    {"name": "list_snapshots", "desc": "List full-system snapshots. No args."},
    {"name": "restore_all", "desc": "Restore the whole system from a snapshot. Args: snapshot_id (str)."},
    # ── Orchestrator management tools ──────────────────────────────────────
    {"name": "list_orchestrators", "desc": "List all orchestrators (slug, name, description). No args."},
    {"name": "get_orchestrator", "desc": "Return a single orchestrator including prompt_text. Args: slug."},
    {"name": "create_orchestrator", "desc": "Create a new orchestrator. Args: slug, name, [description], [prompt_text], [config], [tools], [max_steps], [auto_apply]."},
    {"name": "update_orchestrator", "desc": "Update an existing orchestrator. Args: slug, [name], [description], [prompt_text], [config], [tools], [max_steps], [auto_apply]."},
    {"name": "delete_orchestrator", "desc": "Delete a custom orchestrator and its folder. Args: slug."},
    {"name": "reload_orchestrator", "desc": "Reload an orchestrator from its personal folder into the DB (applies hand-made edits to orchestrator.json / system_prompt.md). Args: slug."},
    {"name": "list_orchestrator_functions", "desc": "List custom Python functions of an orchestrator. Args: slug."},
    {"name": "get_orchestrator_function", "desc": "Return the source code of a custom function. Args: slug, name."},
    {"name": "save_orchestrator_function", "desc": "Create or overwrite a custom Python function for an orchestrator. The code must define invoke(**kwargs) -> dict. Args: slug, name, code."},
    {"name": "delete_orchestrator_function", "desc": "Delete a custom function by name. Args: slug, name."},
    {"name": "list_orchestrator_instructions", "desc": "List orchestrator-specific instructions. Args: slug."},
    {"name": "get_orchestrator_instruction", "desc": "Return a full orchestrator instruction including prompt_text. Args: slug, instruction_id."},
    {"name": "save_orchestrator_instruction", "desc": "Create or update an orchestrator-specific instruction. Args: slug, [instruction_id], name, [description], [prompt_text]."},
    {"name": "delete_orchestrator_instruction", "desc": "Delete an orchestrator-specific instruction. Args: slug, instruction_id."},
]


# Keyword-argument allow-lists for the catalog tools above. Used by
# UniversalDevAgent.dispatch to reject unknown argument names with a
# structured error BEFORE the call, so the LLM gets ``unknown_args`` +
# ``suggestion`` instead of "anything goes" **kwargs swallowing.
WORKSPACE_TOOL_ARGS: Dict[str, Dict[str, set]] = {
    "set_workspace": {"required": {"path"}, "optional": set()},
    "set_target_file": {"required": {"file_path"}, "optional": set()},
    "current_workspace": {"required": set(), "optional": set()},
    "current_install": {"required": set(), "optional": set()},
    "scan_folder": {"required": set(), "optional": set()},
    "search_in_files": {
        "required": {"query"},
        "optional": {"files", "path", "subdir", "extensions", "regex",
                     "case_sensitive", "max_results", "context_before", "context_after"},
    },
    "assess_workspace": {"required": set(), "optional": set()},
    "build_project_map": {"required": set(), "optional": set()},
    "write_project_map": {"required": {"responsibilities"}, "optional": set()},
    "write_doc": {"required": {"doc"}, "optional": {"content"}},
    "read_doc": {"required": {"doc"}, "optional": set()},
    "snapshot_all": {"required": set(), "optional": {"note"}},
    "list_snapshots": {"required": set(), "optional": set()},
    "restore_all": {"required": {"snapshot_id"}, "optional": set()},
    "list_orchestrators": {"required": set(), "optional": set()},
    "get_orchestrator": {"required": {"slug"}, "optional": set()},
    "create_orchestrator": {
        "required": {"slug", "name"},
        "optional": {"description", "prompt_text", "config", "tools",
                     "max_steps", "auto_apply"},
    },
    "update_orchestrator": {
        "required": {"slug"},
        "optional": {"name", "description", "prompt_text", "config", "tools",
                     "max_steps", "auto_apply", "sort_order"},
    },
    "delete_orchestrator": {"required": {"slug"}, "optional": set()},
    "reload_orchestrator": {"required": {"slug"}, "optional": set()},
    "list_orchestrator_functions": {"required": {"slug"}, "optional": set()},
    "get_orchestrator_function": {"required": {"slug", "name"}, "optional": set()},
    "save_orchestrator_function": {"required": {"slug", "name", "code"}, "optional": set()},
    "delete_orchestrator_function": {"required": {"slug", "name"}, "optional": set()},
    "list_orchestrator_instructions": {"required": {"slug"}, "optional": set()},
    "get_orchestrator_instruction": {"required": {"slug", "instruction_id"}, "optional": set()},
    "save_orchestrator_instruction": {
        "required": {"slug", "name"},
        "optional": {"instruction_id", "description", "prompt_text"},
    },
    "delete_orchestrator_instruction": {"required": {"slug", "instruction_id"}, "optional": set()},
}

_UNKNOWN_ARGS_ERROR = (
    "Tool '{tool}' got unexpected argument(s): {unknown}. "
    "Use only the standard arguments documented in the system prompt "
    "(section 6) for this tool - do not invent parameter names."
)


def _workspace_usage(tool_name: str, spec: Dict[str, set]) -> str:
    """Build a short usage/signature hint from a WORKSPACE_TOOL_ARGS spec."""
    parts = sorted(spec["required"])
    parts += [f"[{name}]" for name in sorted(spec["optional"])]
    if not parts:
        return f"Usage: {tool_name}() - this tool takes no arguments."
    return f"Usage: {tool_name}({', '.join(parts)})."


ORCHESTRATOR_TOOL_NAMES = {t["name"] for t in WORKSPACE_TOOL_CATALOG if t["name"].startswith("orchestrator") or t["name"] in ("list_orchestrators", "get_orchestrator", "create_orchestrator", "update_orchestrator", "delete_orchestrator", "reload_orchestrator")}


def build_assistant_dict_from_config() -> Tuple[dict, dict]:
    """Build TWO assistant-compatible dicts from the DevAgent config KV store.

    Returns ``(strong_assistant, weak_assistant)``.

    *strong_skill* uses ``strong_service``/``strong_model``; if not configured,
    falls back to the legacy ``service``/``model`` keys for backward compatibility.

    *weak_skill* uses ``weak_service``/``weak_model``; if not configured,
    falls back to *strong_skill* so every step has a valid model.
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
    weak_assistant = _make_assistant(
        cfg.get("weak_service", ""),
        cfg.get("weak_model", ""),
        strong_assistant["service"],
        strong_assistant["model"],
    )
    return strong_assistant, weak_assistant


# Legacy alias (old "skill" terminology).
build_skill_dict_from_config = build_assistant_dict_from_config


class UniversalDevAgent:
    """Universal-developer dispatch surface = core tools + workspace tools."""

    def __init__(self, workspace: Optional[str] = None, target_file: Optional[str] = None):
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
        """Route to core ToolExecutor or an extra workspace tool.

        Catalog tools are validated against WORKSPACE_TOOL_ARGS first: unknown
        argument names produce a structured error (``unknown_args`` +
        ``suggestion``) telling the caller to use only the standard arguments
        from the system prompt. Custom orchestrator functions are NOT listed
        there and keep their arbitrary-kwargs contract.
        """
        if not isinstance(args, dict):
            return {"ok": False, "error": f"args must be a dict, got {type(args).__name__}"}
        spec = WORKSPACE_TOOL_ARGS.get(tool_name)
        if spec is not None:
            allowed = spec["required"] | spec["optional"]
            unknown = sorted(k for k in args if k not in allowed)
            if unknown:
                return {
                    "ok": False,
                    "error": _UNKNOWN_ARGS_ERROR.format(
                        tool=tool_name, unknown=", ".join(unknown)
                    ),
                    "unknown_args": unknown,
                    "suggestion": _workspace_usage(tool_name, spec),
                }
        if tool_name in self._extra:
            args = _coerce_numeric_args(self._extra[tool_name], args)
            return self._extra[tool_name](**args)
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
        self.core._orchestrator_slug = slug
        for fname, fn in functions.items():
            self._extra[fname] = fn
        self._attach_connection_tools(slug)

    _CONNECTION_TOOL_NAMES = (
        "github_list_repos",
        "github_create_repo",
        "github_upload_file",
        "github_update_file",
        "github_read_file",
    )

    def _attach_connection_tools(self, slug: str) -> None:
        """Register/unregister built-in connection tools for an orchestrator.

        Connection tools (currently GitHub) are only made callable when the
        orchestrator has enabled connections in its config. They are removed
        when the orchestrator disables all connections so stale dispatchers
        do not keep accepting GitHub calls.
        """
        try:
            from core.orchestrators import get_enabled_connections
            enabled = get_enabled_connections(slug)
            if not enabled:
                for name in self._CONNECTION_TOOL_NAMES:
                    self._extra.pop(name, None)
                return
            from core import github_tools
            for name in self._CONNECTION_TOOL_NAMES:
                fn = getattr(github_tools, name, None)
                if callable(fn):
                    self._extra[name] = fn
        except Exception:
            # Best effort: never break orchestrator attachment on library errors.
            pass

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
        self.core._orchestrator_slug = old_orch_slug
        self.core._web_search_config = old_web_search_config

    # ─── workspace-tool wrappers ───────────────────────────────────────────
    def _set_workspace(self, path: str, **kwargs) -> Dict[str, Any]:
        result = wt.set_workspace(path)
        self.target_file = None
        self._recreate_core_preserving_history()
        return result

    def _set_target_file(self, file_path: str, **kwargs) -> Dict[str, Any]:
        result = wt.set_target_file(file_path)
        if result.get("ok"):
            self.target_file = result.get("target_file")
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

    # ─── orchestrator tool wrappers ────────────────────────────────────────

    def _list_orchestrators(self, **kwargs) -> Dict[str, Any]:
        from core.orchestrators import list_orchestrators
        orch_list = list_orchestrators()
        return {"ok": True, "count": len(orch_list), "orchestrators": orch_list}

    def _get_orchestrator(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import get_orchestrator
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        orch = get_orchestrator(slug)
        if orch is None:
            return {"ok": False, "error": f"Orchestrator not found: {slug}"}
        return {"ok": True, "orchestrator": orch}

    def _create_orchestrator(self, slug: str = "", name: str = "", description: str = "",
                             prompt_text: str = "", config: Optional[dict] = None,
                             tools: Optional[list] = None, max_steps: int = 100,
                             auto_apply: bool = True, **kwargs) -> Dict[str, Any]:
        from core.orchestrators import create_orchestrator
        from core.orchestrator_folders import safe_orchestrator_slug
        effective_slug = safe_orchestrator_slug(slug)
        if not effective_slug or not name.strip():
            return {"ok": False, "error": "Both 'slug' and 'name' are required."}
        orch_id = create_orchestrator(
            slug=slug.strip(),
            name=name.strip(),
            description=description or "",
            prompt_text=prompt_text or "",
            config=config or {},
            tools=tools or [],
            max_steps=int(max_steps or 100),
            auto_apply=bool(auto_apply),
        )
        if orch_id is None:
            return {"ok": False, "error": f"Failed to create orchestrator (slug may already exist): {slug}"}
        return {"ok": True, "orchestrator_id": orch_id, "slug": effective_slug, "name": name.strip()}

    def _update_orchestrator(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import save_orchestrator
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        allowed = {"name", "description", "prompt_text", "config", "tools",
                   "max_steps", "auto_apply", "sort_order"}
        update_kwargs = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        ok = save_orchestrator(slug, **update_kwargs)
        if not ok:
            return {"ok": False, "error": f"Failed to update orchestrator: {slug}"}
        return {"ok": True, "slug": slug, "updated": True, "fields": list(update_kwargs.keys())}

    def _delete_orchestrator(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import delete_orchestrator
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        ok = delete_orchestrator(slug)
        if not ok:
            return {"ok": False, "error": f"Failed to delete orchestrator (may be built-in or not found): {slug}"}
        return {"ok": True, "slug": slug, "deleted": True}

    def _reload_orchestrator(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import reload_orchestrator_from_folder
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        res = reload_orchestrator_from_folder(slug)
        if not res.get("ok"):
            return {"ok": False, "error": f"Failed to reload orchestrator: {slug} ({res.get('error', 'unknown')})"}
        return {"ok": True, "slug": slug, "action": res.get("action"), "reloaded": True}

    def _list_orchestrator_functions(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_list_functions
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        functions = orch_list_functions(slug)
        return {"ok": True, "slug": slug, "count": len(functions), "functions": functions}

    def _get_orchestrator_function(self, slug: str = "", name: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_get_function
        if not slug or not name:
            return {"ok": False, "error": "Both 'slug' and 'name' are required."}
        fn = orch_get_function(slug, name)
        if fn is None:
            return {"ok": False, "error": f"Function not found: {slug}/{name}"}
        return {"ok": True, "function": fn}

    def _save_orchestrator_function(self, slug: str = "", name: str = "", code: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_save_function
        if not slug or not name or not code:
            return {"ok": False, "error": "Arguments 'slug', 'name' and 'code' are required."}
        ok = orch_save_function(slug, name, code)
        if not ok:
            return {"ok": False, "error": f"Failed to save function (name must be a valid Python identifier): {slug}/{name}"}
        return {"ok": True, "slug": slug, "name": name, "saved": True}

    def _delete_orchestrator_function(self, slug: str = "", name: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_delete_function
        if not slug or not name:
            return {"ok": False, "error": "Both 'slug' and 'name' are required."}
        ok = orch_delete_function(slug, name)
        if not ok:
            return {"ok": False, "error": f"Failed to delete function: {slug}/{name}"}
        return {"ok": True, "slug": slug, "name": name, "deleted": True}

    def _list_orchestrator_instructions(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_list_instructions
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        instructions = orch_list_instructions(slug)
        return {"ok": True, "slug": slug, "count": len(instructions), "instructions": instructions}

    def _get_orchestrator_instruction(self, slug: str = "", instruction_id: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_get_instruction
        if not slug or not instruction_id:
            return {"ok": False, "error": "Both 'slug' and 'instruction_id' are required."}
        inst = orch_get_instruction(slug, instruction_id)
        if inst is None:
            return {"ok": False, "error": f"Instruction not found: {slug}/{instruction_id}"}
        return {"ok": True, "instruction": inst}

    def _save_orchestrator_instruction(self, slug: str = "", instruction_id: str = "", name: str = "",
                                      description: str = "", prompt_text: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_save_instruction
        if not slug or not name:
            return {"ok": False, "error": "Arguments 'slug' and 'name' are required."}
        new_id = orch_save_instruction(
            slug, instruction_id=instruction_id, name=name,
            description=description or "", prompt_text=prompt_text or "",
        )
        if not new_id:
            return {"ok": False, "error": f"Failed to save instruction: {slug}"}
        return {"ok": True, "slug": slug, "instruction_id": new_id, "saved": True}

    def _delete_orchestrator_instruction(self, slug: str = "", instruction_id: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_delete_instruction
        if not slug or not instruction_id:
            return {"ok": False, "error": "Both 'slug' and 'instruction_id' are required."}
        ok = orch_delete_instruction(slug, instruction_id)
        if not ok:
            return {"ok": False, "error": f"Failed to delete instruction: {slug}/{instruction_id}"}
        return {"ok": True, "slug": slug, "instruction_id": instruction_id, "deleted": True}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
