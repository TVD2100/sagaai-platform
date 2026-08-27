"""
core.orchestrators - public API for orchestrator management.

Orchestrators are self-contained autonomous agents (like DevAgent).
Each orchestrator has its own system prompt, model configuration (strong/weak/search),
tool set, economy settings, and instructions.  Orchestrators appear as
separate pages in the sidebar navigation and can be exported/imported as JSON.

Since the "Orchestrators" feature (v1), every user-created orchestrator also
has a personal folder under DATA_DIR/orchestrators/<slug>/ that stores:
  - orchestrator.json   (full export bundle)
  - system_prompt.md    (latest system prompt, easy to edit directly)
  - instructions/*.md   (orchestrator-specific instructions, one file per instruction)
  - functions/*.py      (custom Python functions the orchestrator can call)

The folder is the source of truth. A DB cache (orchestrator_instructions)
holds instructions for the runtime hot path; it is rebuilt at startup and
after every write. Orchestrators can be exported/imported as JSON.

Since the standardized-skills feature, an orchestrator can also have a list of
enabled skill IDs stored in its config under the "enabled_skills" key. The
metadata of those skills is appended to the system prompt so the orchestrator
knows which skills are available to it.

DevAgent can read and edit those folders directly, which allows iterating on
an orchestrator through the normal DevAgent workflow.
"""
import os
import uuid
from datetime import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

from storage.repository import (
    repo_list_orchestrators,
    repo_get_orchestrator_by_slug,
    repo_get_orchestrator_with_text,
    repo_create_orchestrator,
    repo_update_orchestrator,
    repo_delete_orchestrator,
    repo_delete_all_orchestrator_instructions,
)

from core.config import (
    get_default_economy_tail_messages,
    get_default_economy_cache_enabled,
    get_default_economy_cache_multiplier,
    get_default_strong_max_tokens,
    get_default_weak_max_tokens,
    get_devagent_defaults,
)
from core.services import (
    get_services as _get_services,
    service_supports_reasoning_effort,
    default_reasoning_effort,
)
from core.orchestrator_folders import (
    safe_orchestrator_slug,
    ensure_orchestrator_dir,
    remove_orchestrator_dir,
    save_orchestrator_bundle,
    load_orchestrator_bundle,
    load_orchestrator_prompt_file,
    list_orchestrator_folder_slugs,
    orchestrator_folder_exists,
    list_orchestrator_functions,
    get_orchestrator_function,
    save_orchestrator_function,
    delete_orchestrator_function,
    list_orchestrator_instructions,
    get_orchestrator_instruction,
    save_orchestrator_instruction,
    delete_orchestrator_instruction,
    sync_orchestrator_instructions,
    export_orchestrator_folder,
    import_orchestrator_folder,
)

# Built-in DevAgent orchestrator slug.
DEVAGENT_SLUG = "dev_agent"

# Default system prompt for the web-search sub-agent. Every orchestrator can
# override it via the ``web_search_prompt`` config key; dynamic, task-specific
# additions are passed through the ``instructions`` argument of the web_search
# tool and appended to this base prompt at call time.
DEFAULT_WEB_SEARCH_PROMPT = (
    "You are a helpful research assistant. Use web search to find "
    "accurate, up-to-date information. Answer the user's query "
    "concisely based on the search results. Cite sources when possible."
)


def _ensure_default_orchestrators() -> Dict[str, str]:
    """Create default orchestrators from defaults/orchestrators/*.

    Imported lazily to avoid a circular import with core.default_imports.
    """
    from core.default_imports import ensure_default_orchestrators
    return ensure_default_orchestrators()

# ─── Default config for the DevAgent orchestrator ─────────────────────────
# The values come from the canonical orchestrator bundle
# (orchestrators/dev_agent/orchestrator.json, config section) via the
# core.config helper. This is the single source of truth for first-boot
# creation, backfill and the build_assistant_dicts fallback.


def _devagent_default_config() -> Dict[str, Any]:
    """Build the DevAgent orchestrator config from the canonical bundle.

    Reads ``core.config.get_devagent_defaults()`` (bundle config section with
    built-in fallback) and normalizes types to the shape expected by the
    orchestrator storage. This is the single source of truth for first-boot
    creation, backfill and the build_assistant_dicts fallback.
    """
    from core.config import get_devagent_defaults as _daf_defaults
    defaults = _daf_defaults()

    def _str1(key, fallback):
        val = defaults.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
        return fallback

    def _num(key, fallback, cast):
        try:
            return cast(defaults.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _lst(key):
        val = defaults.get(key)
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    return {
        "strong_service": _str1("strong_service", "DeepSeek"),
        "strong_model": _str1("strong_model", "deepseek-v4-pro"),
        "strong_temperature": _num("strong_temperature", 0.4, float),
        "strong_max_tokens": _num("strong_max_tokens", 384000, int),
        "strong_reasoning_effort": _str1("strong_reasoning_effort", "max"),
        "weak_service": _str1("weak_service", "DeepSeek"),
        "weak_model": _str1("weak_model", "deepseek-v4-pro"),
        "weak_temperature": _num("weak_temperature", 0.4, float),
        "weak_max_tokens": _num("weak_max_tokens", 384000, int),
        "weak_reasoning_effort": _str1("weak_reasoning_effort", "max"),
        "search_service": _str1("search_service", "YandexAI"),
        "search_model": _str1("search_model", "aliceai-llm-flash"),
        "search_temperature": _num("search_temperature", 0.3, float),
        "search_max_tool_calls": _num("search_max_tool_calls", 1, int),
        "search_reasoning_effort": _str1("search_reasoning_effort", "high"),
        "web_search_prompt": _str1("web_search_prompt", DEFAULT_WEB_SEARCH_PROMPT),
        "economy_tail_messages": get_default_economy_tail_messages(),
        "economy_cache_enabled": get_default_economy_cache_enabled(),
        "economy_cache_multiplier": get_default_economy_cache_multiplier(),
        "enabled_skills": _lst("enabled_skills"),
        "enabled_connections": _lst("enabled_connections"),
    }


# Backward-compatible alias (tests/legacy code may import this name).
_DEVAGENT_DEFAULT_CONFIG: Dict[str, Any] = _devagent_default_config()


def _default_economy_tail_messages() -> int:
    """Return the default economy tail length from the canonical DevAgent
    orchestrator bundle or the built-in fallback (30). This is the single
    source of truth for the default value."""
    return get_default_economy_tail_messages()


def _default_economy_cache_enabled() -> bool:
    """Return the default cache-friendly economy mode flag."""
    return get_default_economy_cache_enabled()


def _default_economy_cache_multiplier() -> int:
    """Return the default cache-window multiplier (xN)."""
    return get_default_economy_cache_multiplier()


# ─── Import validation helpers ────────────────────────────────────────────

# Set of tool names that are KNOWN and allowed for orchestrators.
# Any tool name present in import/export must be in this set to be accepted.
# Populated lazily on first use (to avoid import-time dependency on
# dev_agent.tool_executor).
_KNOWN_TOOL_NAMES: Optional[set] = None


def _get_known_tool_names() -> set:
    """Return the set of all allowed tool names across core + workspace catalogs.

    Cached on first call; use ``_invalidate_known_tool_names()`` if the
    tool catalog changes at runtime (rare).
    """
    global _KNOWN_TOOL_NAMES
    if _KNOWN_TOOL_NAMES is not None:
        return _KNOWN_TOOL_NAMES
    try:
        from dev_agent.tool_executor import TOOL_CATALOG as CORE_TOOLS
        from dev_agent.universal_agent import WORKSPACE_TOOL_CATALOG
        _KNOWN_TOOL_NAMES = (
        {t["name"] for t in CORE_TOOLS}
        | {t["name"] for t in WORKSPACE_TOOL_CATALOG}
        | {"list_skills", "get_skill_by_id", "update_skill_by_id",
           "create_skill_for_task", "detect_and_select_skill",
           "list_skills_library", "get_skill_folder", "get_skill_prompt", "get_skill_file"}
    )
    except Exception:
        # During early bootstrap or tests, the catalogs may not be importable.
        # Fall back to a minimal known set that covers the most common tools.
        _KNOWN_TOOL_NAMES = {
            "read_file", "list_files", "propose_file", "verify_file",
            "create_backup", "restore_backup", "show_history", "run_test",
            "run_code", "list_skills", "get_skill_by_id", "update_skill_by_id",
            "list_instructions", "get_instruction", "detect_and_select_skill",
            "create_skill_for_task",
            "list_skills_library", "get_skill_folder", "get_skill_prompt", "get_skill_file",
            "web_search", "get_history_index",
            "get_history_messages",
            "set_workspace", "set_target_file", "current_workspace",
            "scan_folder", "assess_workspace", "build_project_map",
            "write_project_map", "write_doc", "read_doc",
            "snapshot_all", "list_snapshots", "restore_all",
        }
    return _KNOWN_TOOL_NAMES


def _invalidate_known_tool_names() -> None:
    """Clear the cached known-tool-names set so it is rebuilt on next use."""
    global _KNOWN_TOOL_NAMES
    _KNOWN_TOOL_NAMES = None


# Prompt length limit for imported orchestrators (characters).
# Imports larger than this are rejected to prevent resource-exhaustion.
_MAX_PROMPT_TEXT_LENGTH = 100_000


# ─── List / Get ──────────────────────────────────────────────────────────


def list_orchestrators() -> List[Dict[str, Any]]:
    """Return all orchestrators (without prompt_text), for navigation."""
    return repo_list_orchestrators()


def get_orchestrator(slug: str) -> Optional[Dict[str, Any]]:
    """Return a full orchestrator dict including prompt_text and config."""
    return repo_get_orchestrator_with_text(slug)


def get_orchestrator_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    """Return orchestrator metadata (no prompt_text), or None."""
    return repo_get_orchestrator_by_slug(slug)


# ─── Create / Update / Delete ────────────────────────────────────────────


def create_orchestrator(slug: str, name: str, description: str = "",
                        prompt_text: str = "", config: dict = None,
                        tools: list = None, max_steps: int = 100,
                        auto_apply: bool = True) -> Optional[str]:
    """Create a new orchestrator. Returns the id on success, None on failure.

    Also creates the orchestrator's personal folder on disk and writes an
    initial bundle (orchestrator.json).
    """
    # Normalise slug to [a-z0-9_]+ so it is safe as a DB slug and a folder
    # name. Empty input (or a slug made only of invalid characters) is
    # rejected instead of silently creating an 'unnamed' folder.
    slug = safe_orchestrator_slug(slug)
    if not slug:
        return None
    # Ensure no duplicate slug.
    existing = repo_get_orchestrator_by_slug(slug)
    if existing:
        return None
    orch_id = uuid.uuid4().hex[:8]
    ok = repo_create_orchestrator(
        orchestrator_id=orch_id,
        slug=slug,
        name=name.strip(),
        description=description,
        prompt_text=prompt_text,
        config=config or {},
        tools=tools or [],
        max_steps=max_steps,
        auto_apply=auto_apply,
        is_builtin=False,
        sort_order=100,  # user-created orchestrators go before DevAgent
    )
    if ok:
        _sync_orchestrator_folder(slug)
    return orch_id if ok else None


def save_orchestrator(slug: str, **kwargs) -> bool:
    """Update an existing orchestrator by slug.

    Supported kwargs: name, description, prompt_text, config, tools,
    max_steps, auto_apply, sort_order.

    Returns True on success.
    """
    orch = repo_get_orchestrator_with_text(slug)
    if orch is None:
        return False
    ok = repo_update_orchestrator(orch["id"], **kwargs)
    if ok:
        _sync_orchestrator_folder(slug)
    return ok


def delete_orchestrator(slug: str) -> bool:
    """Delete an orchestrator.  Built-in orchestrators cannot be deleted.

    Also removes the orchestrator's personal folder from disk.
    """
    orch = repo_get_orchestrator_with_text(slug)
    if orch is None:
        return False
    if orch.get("is_builtin"):
        return False
    ok = repo_delete_orchestrator(orch["id"])
    if ok:
        # The orchestrator_instructions cache rows would otherwise survive
        # the deletion and could leak into a future orchestrator created
        # with the same slug.
        repo_delete_all_orchestrator_instructions(slug)
        remove_orchestrator_dir(slug)
    return ok


def _sync_orchestrator_folder(slug: str) -> None:
    """Write the current orchestrator record as orchestrator.json bundle.

    Uses get_orchestrator() so the bundle always has the latest prompt_text
    and config from the DB.  Does not overwrite the functions/ or
    instructions/ parts of an existing folder (they are edited by
    DevAgent / the user separately). Instructions cache is rebuilt too.
    """
    orch = repo_get_orchestrator_with_text(slug)
    if orch is None:
        return
    bundle = {
        "format": _EXPORT_FORMAT,
        "slug": orch.get("slug", slug),
        "name": orch.get("name", ""),
        "description": orch.get("description", ""),
        "prompt_text": orch.get("prompt_text", ""),
        "config": orch.get("config", {}),
        "tools": orch.get("tools", []),
        "max_steps": orch.get("max_steps", 100),
        "auto_apply": orch.get("auto_apply", True),
        "exported_at": datetime.now().isoformat(),
    }
    save_orchestrator_bundle(slug, bundle)
    # Keep the instruction cache in sync too (rebuild from md files).
    try:
        sync_orchestrator_instructions(slug)
    except Exception:
        pass


def reload_orchestrator_from_folder(slug: str) -> Dict[str, Any]:
    """Reload an orchestrator from its folder into the DB.

    Reads orchestrator.json (and system_prompt.md when present) and updates
    the DB record. This is the "folders first" direction used at startup and
    by the settings Sync button. Instructions are rebuilt into the cache.

    Returns a status dict: {ok, slug, action, error}.
    """
    if not orchestrator_folder_exists(slug):
        return {"ok": False, "slug": slug, "action": "skip", "error": "folder missing"}
    bundle = load_orchestrator_bundle(slug)
    if bundle is None:
        return {"ok": False, "slug": slug, "action": "skip", "error": "unreadable orchestrator.json"}

    prompt_text = str(bundle.get("prompt_text") or "")
    prompt_file_text = load_orchestrator_prompt_file(slug)
    if prompt_file_text.strip():
        prompt_text = prompt_file_text

    config = bundle.get("config")
    if not isinstance(config, dict):
        config = {}
    raw_tools = bundle.get("tools")
    if not isinstance(raw_tools, list):
        raw_tools = []
    tools, _rejected = _validate_imported_tools(raw_tools)

    existing = repo_get_orchestrator_with_text(slug)
    if existing is not None:
        update_kwargs: Dict[str, Any] = dict(
            name=str(bundle.get("name") or slug),
            description=str(bundle.get("description") or ""),
            config=config,
            tools=tools,
            max_steps=int(bundle.get("max_steps", existing.get("max_steps", 100)) or 100),
            auto_apply=bool(bundle.get("auto_apply", existing.get("auto_apply", True))),
        )
        if not existing.get("is_builtin"):
            update_kwargs["prompt_text"] = prompt_text
        repo_update_orchestrator(existing["id"], **update_kwargs)
        sync_orchestrator_instructions(slug)
        return {"ok": True, "slug": slug, "action": "updated"}

    orch_id = uuid.uuid4().hex[:8]
    ok = repo_create_orchestrator(
        orchestrator_id=orch_id,
        slug=slug,
        name=str(bundle.get("name") or slug),
        description=str(bundle.get("description") or ""),
        prompt_text=prompt_text,
        config=config,
        tools=tools,
        max_steps=int(bundle.get("max_steps", 100) or 100),
        auto_apply=bool(bundle.get("auto_apply", True)),
        is_builtin=False,
        sort_order=int(bundle.get("sort_order", 100) or 100),
    )
    if ok:
        sync_orchestrator_instructions(slug)
        return {"ok": True, "slug": slug, "action": "created"}
    return {"ok": False, "slug": slug, "action": "skip", "error": "db create failed"}


def sync_all_orchestrator_folders() -> Dict[str, Any]:
    """Reload every existing orchestrator folder into the DB.

    Called at startup and by the settings Sync button. Returns per-slug status.
    """
    results: Dict[str, Any] = {}
    for slug in list_orchestrator_folder_slugs():
        try:
            res = reload_orchestrator_from_folder(slug)
            results[slug] = res.get("action", "error") + ("" if res.get("ok") else ": " + str(res.get("error", "")))
        except Exception as exc:
            results[slug] = "error: " + str(exc)
    return results


# ─── Enabled standardized skills ─────────────────────────────────────────


def get_enabled_skills(orchestrator_slug: str = DEVAGENT_SLUG) -> List[str]:
    """Return the list of enabled skill IDs for an orchestrator.

    The info is stored in the orchestrator's config under "enabled_skills".
    Missing/invalid values are treated as an empty list.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    skills = cfg.get("enabled_skills") or []
    if not isinstance(skills, list):
        return []
    return [str(s) for s in skills if isinstance(s, str) and s]


def set_enabled_skills(orchestrator_slug: str, skill_ids: List[str]) -> bool:
    """Store the list of enabled skill IDs for an orchestrator.

    Also syncs the orchestrator bundle on disk. Returns True on success.
    """
    normalized = []
    for s in (skill_ids or []):
        if isinstance(s, str) and s.strip() and s.strip() not in normalized:
            normalized.append(s.strip())
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg["enabled_skills"] = normalized
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


# ─── Enabled connections (connectors) ────────────────────────────────────


def get_enabled_connections(orchestrator_slug: str = DEVAGENT_SLUG) -> List[str]:
    """Return the list of enabled connection ids for an orchestrator.

    The info is stored in the orchestrator's config under
    "enabled_connections". Missing/invalid values are treated as an empty list.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    connections = cfg.get("enabled_connections") or []
    if not isinstance(connections, list):
        return []
    return [str(c) for c in connections if isinstance(c, str) and c]


def set_enabled_connections(orchestrator_slug: str, connection_ids: List[str]) -> bool:
    """Store the list of enabled connection ids for an orchestrator.

    Also syncs the orchestrator bundle on disk. Returns True on success.
    """
    normalized = []
    for c in (connection_ids or []):
        if isinstance(c, str) and c.strip() and c.strip() not in normalized:
            normalized.append(c.strip())
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg["enabled_connections"] = normalized
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


def _extend_prompt_with_connections(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Append a metadata block describing enabled connections to the prompt.

    The block lists each enabled connection (service, name, account - never
    the token) and the GitHub tools available to the orchestrator with their
    signatures. Returns the original prompt when no connections are enabled
    or on error (best effort).
    """
    enabled = get_enabled_connections(orchestrator_slug)
    if not enabled:
        return prompt
    try:
        from core.connectors import get_connection
        from core.github_tools import get_tools
    except Exception:
        return prompt

    lines = [
        "## Available service connections",
        "",
        "The following external service connections are enabled for you. "
        "Use them via the GitHub tools listed below; tokens are handled "
        "by the platform and are never passed to you:",
        "",
    ]
    for conn_id in enabled:
        conn = get_connection(conn_id)
        if not isinstance(conn, dict):
            continue
        service = str(conn.get("service") or "?")
        name = str(conn.get("name") or "?")
        account = str(conn.get("account") or "")
        line = f"- `{conn_id}` - {service} ({name})"
        if account:
            line += f", account: {account}"
        lines.append(line)

    lines.append("")
    lines.append("Available connection tools:")
    try:
        for tool in get_tools():
            lines.append(f"- `{tool['name']}` - {tool['desc']}")
    except Exception:
        pass

    # Compact usage notes so orchestrators can call the tools correctly even
    # without loading the full github_connector instruction.
    lines.append("")
    lines.append("Quick usage notes:")
    lines.append("- Always pass `connector_id` (from the list above) as the first argument.")
    lines.append("- `repo` accepts `owner/repo` or a bare repo name of the authenticated user.")
    lines.append("- New repo names must be lowercase, without spaces.")
    lines.append("- `github_upload_file` creates a NEW file; use `github_update_file` to change an existing file.")
    lines.append("- Before updating a file, read it with `github_read_file` first.")
    lines.append("- For the full usage guide, load the `github_connector` instruction if it is listed in `## Available instructions` of this orchestrator.")

    block = "\n".join(lines)
    if prompt.strip():
        return f"{prompt}\n\n{block}"
    return block


# ─── Assigned RAG knowledge bases ──────────────────────────────────────


def get_orchestrator_rag_bases(orchestrator_slug: str = DEVAGENT_SLUG) -> List[str]:
    """Return the list of RAG base slugs assigned to an orchestrator.

    The list is stored in the orchestrator config under "rag_bases".
    DevAgent (slug="dev_agent") is special: it may use any base, so an
    empty list means "all bases". Other orchestrators receive only the
    bases explicitly assigned here; bases not listed are forbidden even
    if their slug is known.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    bases = cfg.get("rag_bases") or []
    if not isinstance(bases, list):
        return []
    return [str(b) for b in bases if isinstance(b, str) and b]


def set_orchestrator_rag_bases(orchestrator_slug: str, base_slugs: List[str]) -> bool:
    """Store the list of RAG base slugs assigned to an orchestrator.

    Duplicates are removed and each entry is normalized to lowercase.
    Returns True on success.
    """
    normalized = []
    for b in (base_slugs or []):
        if isinstance(b, str) and b.strip():
            val = b.strip().lower()
            if val not in normalized:
                normalized.append(val)
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg["rag_bases"] = normalized
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


def _extend_prompt_with_rag_bases(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Append the assigned RAG knowledge bases as a metadata block.

    The block tells the orchestrator which bases it may search via the
    ``rag_search`` tool. DevAgent sees all bases; other orchestrators see
    only their assigned list. Inactive bases (missing provider credentials)
    are marked so the model knows they require API keys.
    Returns the original prompt when there is nothing to append or on error.
    """
    try:
        from core.rag import list_bases_with_activity
        if orchestrator_slug == DEVAGENT_SLUG:
            assigned = None  # all bases
        else:
            assigned = set(get_orchestrator_rag_bases(orchestrator_slug))
            if not assigned:
                return prompt
        bases = list_bases_with_activity()
        rows = []
        for b in bases:
            bslug = str(b.get("slug") or "")
            if not bslug:
                continue
            if assigned is not None and bslug not in assigned:
                continue
            active = bool(b.get("active", True))
            name = str(b.get("name") or bslug)
            status = str(b.get("status") or "draft")
            flag = "" if active else " [INACTIVE: provider API keys required]"
            rows.append(f"- `{bslug}` — {name} (status: {status}){flag}")
        if not rows:
            return prompt
        block = (
            "## Available RAG knowledge bases\n\n"
            "Use only these bases with the `rag_search` tool. Bases not listed "
            "here are forbidden even if you know their identifiers:\n\n"
            + "\n".join(rows)
        )
        if prompt.strip():
            return f"{prompt}\n\n{block}"
        return block
    except Exception:
        return prompt


def _extend_prompt_with_skills(prompt: str, enabled_skills: List[str]) -> str:
    """Append the metadata block of enabled skills to a system prompt.

    Returns the original prompt if no skills are enabled or if the library
    cannot be read (best effort - the orchestrator still works without
    the skills block).
    """
    if not enabled_skills:
        return prompt
    try:
        from core.skills_library import build_skills_metadata_text
        skills_text = build_skills_metadata_text(enabled_skills)
        if skills_text:
            if prompt.strip():
                return f"{prompt}\n\n{skills_text}"
            return skills_text
    except Exception:
        pass
    return prompt



def _extend_prompt_with_instructions(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Append the metadata block of available instructions to a system prompt.

    Lists orchestrator-specific instructions of the given orchestrator plus
    any global instructions (if present). The block tells the orchestrator
    how to load the full text of each instruction, so the main system prompt
    only needs to say: "for this task type, first load the matching instruction".
    Returns the original prompt when there are no instructions or on error.
    """
    try:
        from core.instructions import list_instructions_for as list_global_instructions
        from core.orchestrator_folders import list_orchestrator_instructions as list_orch_instructions

        local = [i for i in list_orch_instructions(orchestrator_slug)
                 if isinstance(i, dict) and i.get("id")]
        glob = [i for i in list_global_instructions(orchestrator_slug)
                if isinstance(i, dict) and i.get("id")]
        if not local and not glob:
            return prompt

        lines = [
            "## Available instructions",
            "",
            "The following instructions may be useful for certain task types. "
            "When a task matches an instruction type, first load and follow "
            "the corresponding instruction:",
            "",
        ]
        if local:
            lines.append(f"Instructions of orchestrator '{orchestrator_slug}':")
            for inst in sorted(local, key=lambda x: (x.get("name") or x["id"]).lower()):
                iid = inst["id"]
                name = inst.get("name") or iid
                desc = (inst.get("description") or "").strip()
                lines.append(
                    f"- **{name}** (id: `{iid}`) - load full text via "
                    f"`get_orchestrator_instruction('{orchestrator_slug}', '{iid}')`"
                )
                if desc:
                    lines.append(f"  {desc}")
            lines.append("")
        if glob:
            lines.append("Global instructions:")
            for inst in sorted(glob, key=lambda x: (x.get("name") or x["id"]).lower()):
                iid = inst["id"]
                name = inst.get("name") or iid
                desc = (inst.get("description") or "").strip()
                lines.append(
                    f"- **{name}** (id: `{iid}`) - load full text via "
                    f"`get_instruction('{iid}')`"
                )
                if desc:
                    lines.append(f"  {desc}")
            lines.append("")
        lines.append(
            "Use these instructions as guidance when performing the matching tasks."
        )
        block = "\n".join(lines)
        if prompt.strip():
            return f"{prompt}\n\n{block}"
        return block
    except Exception:
        return prompt

# ─── Assistant-dict building (for agent loop) ─────────────────────────────


def build_assistant_dicts(orchestrator_slug: str = DEVAGENT_SLUG) -> Tuple[dict, dict]:
    """Build (strong_assistant, weak_assistant) from an orchestrator's config.

    Assistant dicts are compatible with ``core.api_layer.send_request``.
    Falls back to DevAgent defaults if the orchestrator is missing.

    If the orchestrator has enabled standardized skills (config key
    "enabled_skills"), their metadata is appended to the system prompt sent
    to the model.
    """
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        orch = {
            "prompt_text": "",
            "config": _devagent_default_config(),
        }
    cfg = orch.get("config", {})
    prompt = orch.get("prompt_text", "")

    # Append metadata of enabled standardized skills (Skills library).
    enabled_skills = cfg.get("enabled_skills") or []
    prompt = _extend_prompt_with_skills(prompt, enabled_skills)
    prompt = _extend_prompt_with_instructions(prompt, orchestrator_slug)
    # Append enabled external service connections (connectors).
    prompt = _extend_prompt_with_connections(prompt, orchestrator_slug)
    # Append assigned RAG knowledge bases (DevAgent: all; others: assigned).
    prompt = _extend_prompt_with_rag_bases(prompt, orchestrator_slug)

    strong_svc = cfg.get("strong_service", "") or cfg.get("service", "DeepSeek")
    strong_mdl = cfg.get("strong_model", "") or cfg.get("model", "deepseek-v4-pro")
    strong_temp = float(cfg.get("strong_temperature", 0.2) or 0.2)
    strong_max_tokens = int(cfg.get("strong_max_tokens", 0) or 0)

    weak_svc = cfg.get("weak_service", "") or strong_svc
    weak_mdl = cfg.get("weak_model", "") or strong_mdl
    weak_temp = float(cfg.get("weak_temperature", 0.5) or 0.5)
    weak_max_tokens = int(cfg.get("weak_max_tokens", 0) or 0)

    services = _get_services()
    strong_svc_def = services.get(strong_svc, {})
    weak_svc_def = services.get(weak_svc, {})

    def _resolve_effort(cfg_key: str, svc_def: dict, strong: bool, model_id: str = "") -> str:
        raw = cfg.get(cfg_key, "")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
        if service_supports_reasoning_effort(svc_def):
            return default_reasoning_effort(svc_def, strong=strong, model=model_id) or ""
        return ""

    strong_effort = _resolve_effort("strong_reasoning_effort", strong_svc_def, True, strong_mdl)
    weak_effort = _resolve_effort("weak_reasoning_effort", weak_svc_def, False, weak_mdl)

    strong = {
        "text": prompt,
        "service": strong_svc,
        "model": strong_mdl,
        "temperature": strong_temp,
    }
    if strong_effort:
        strong["reasoning_effort"] = strong_effort
    if strong_max_tokens > 0:
        strong["max_tokens"] = strong_max_tokens
    weak = {
        "text": prompt,
        "service": weak_svc,
        "model": weak_mdl,
        "temperature": weak_temp,
    }
    if weak_effort:
        weak["reasoning_effort"] = weak_effort
    if weak_max_tokens > 0:
        weak["max_tokens"] = weak_max_tokens
    return strong, weak


def get_web_search_prompt(orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Return the base system prompt for the web-search sub-agent.

    Reads the orchestrator's ``web_search_prompt`` config key. Falls back to
    ``DEFAULT_WEB_SEARCH_PROMPT`` when the key is missing or empty so older
    orchestrators keep working unchanged.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    prompt = cfg.get("web_search_prompt", "")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    return DEFAULT_WEB_SEARCH_PROMPT


def get_web_search_config(orchestrator_slug: str = DEVAGENT_SLUG) -> Dict[str, Any]:
    """Return the web-search config for an orchestrator.

    Returns {"service": str, "model": str, "temperature": float,
    "max_tool_calls": int, "prompt": str} or empty strings/0 for
    service/model when not configured.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    search_svc = cfg.get("search_service", "") or ""
    search_svc_def = _get_services().get(search_svc, {})
    search_effort = cfg.get("search_reasoning_effort", "")
    search_mdl = cfg.get("search_model", "") or ""
    if not (isinstance(search_effort, str) and search_effort.strip()):
        if service_supports_reasoning_effort(search_svc_def):
            search_effort = default_reasoning_effort(search_svc_def, strong=False, model=search_mdl) or "high"
        else:
            search_effort = ""
    return {
        "service": search_svc,
        "model": cfg.get("search_model", "") or "",
        "temperature": float(cfg.get("search_temperature", 0.3) or 0.3),
        "max_tool_calls": int(cfg.get("search_max_tool_calls", 1) or 1),
        "reasoning_effort": str(search_effort).strip().lower() if search_effort else "",
        "prompt": get_web_search_prompt(orchestrator_slug),
    }


def get_economy_tail_messages(orchestrator_slug: str = DEVAGENT_SLUG) -> int:
    """Return the number of recent messages passed in economy mode.

    Reads the value from the orchestrator config. If no value is stored
    (first boot), falls back to the default from the canonical DevAgent
    orchestrator bundle (orchestrators/dev_agent/orchestrator.json).
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    raw = cfg.get("economy_tail_messages")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return _default_economy_tail_messages()


def get_economy_cache_enabled(orchestrator_slug: str = DEVAGENT_SLUG) -> bool:
    """Return whether cache-friendly economy mode is enabled for an orchestrator.

    Reads the value from the orchestrator config. If no value is stored
    (first boot or older config), falls back to the default from the
    canonical DevAgent orchestrator bundle
    (orchestrators/dev_agent/orchestrator.json).
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    raw = cfg.get("economy_cache_enabled")
    if raw is not None:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return _default_economy_cache_enabled()


def get_economy_cache_multiplier(orchestrator_slug: str = DEVAGENT_SLUG) -> int:
    """Return the cache-window multiplier (xN) for an orchestrator.

    Reads the value from the orchestrator config. If no value is stored
    (first boot or older config), falls back to the default from the
    canonical DevAgent orchestrator bundle
    (orchestrators/dev_agent/orchestrator.json).
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    raw = cfg.get("economy_cache_multiplier")
    if raw is not None:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return _default_economy_cache_multiplier()


def get_economy_config(orchestrator_slug: str = DEVAGENT_SLUG) -> Dict[str, Any]:
    """Return economy-mode settings for an orchestrator."""
    return {
        "tail_messages": get_economy_tail_messages(orchestrator_slug),
        "cache_enabled": get_economy_cache_enabled(orchestrator_slug),
        "cache_multiplier": get_economy_cache_multiplier(orchestrator_slug),
    }


# ─── Export / Import ─────────────────────────────────────────────────────

_EXPORT_FORMAT = "sagaai_orchestrator/v1"


def export_orchestrator(slug: str) -> Optional[Dict[str, Any]]:
    """Export an orchestrator as a JSON-serializable dict.

    Includes the orchestrator's personal folder data: custom functions
    (raw Python source) and orchestrator-specific instructions. The global
    DevAgent instructions are NOT included in this export.
    """
    orch = get_orchestrator(slug)
    if orch is None:
        return None

    # Gather orchestrator-specific instructions from the folder.
    orch_instr = []
    for inst in list_orchestrator_instructions(slug):
        full = get_orchestrator_instruction(slug, inst["id"])
        if full:
            orch_instr.append({
                "id": full.get("id", ""),
                "name": full.get("name", ""),
                "description": full.get("description", ""),
                "prompt_text": full.get("text", ""),
            })

    # Gather custom functions (raw source code).
    functions = {}
    for fn in list_orchestrator_functions(slug):
        full = get_orchestrator_function(slug, fn["name"])
        if full:
            functions[fn["name"]] = full["code"]

    return {
        "format": _EXPORT_FORMAT,
        "slug": orch.get("slug", ""),
        "name": orch.get("name", ""),
        "description": orch.get("description", ""),
        "prompt_text": orch.get("prompt_text", ""),
        "config": orch.get("config", {}),
        "tools": orch.get("tools", []),
        "max_steps": orch.get("max_steps", 100),
        "auto_apply": orch.get("auto_apply", True),
        "instructions": orch_instr,
        "functions": functions,
        "exported_at": datetime.now().isoformat(),
    }


def _validate_imported_tools(tools: list) -> Tuple[list, List[str]]:
    """Validate a tool-name list against the known catalog.

    Returns (filtered_tools, rejected_tool_names). Unknown tools are
    silently dropped; rejected names are returned for reporting.
    """
    if not tools:
        return [], []
    known = _get_known_tool_names()
    filtered: list = []
    rejected: List[str] = []
    for t in tools:
        if isinstance(t, str):
            if t in known:
                filtered.append(t)
            else:
                rejected.append(t)
        elif isinstance(t, dict):
            # Dict tools (e.g. {"type": "web_search", ...}) are always
            # accepted; they are validated at runtime by the API layer.
            filtered.append(t)
        else:
            rejected.append(str(t))
    return filtered, rejected


def import_orchestrator(data: Dict[str, Any], overwrite: bool = False) -> Dict[str, Any]:
    """Import an orchestrator from an export dict.

    Returns {"ok": bool, "slug": str, "error": str, "instructions_imported": int,
             "functions_imported": int, "tools_filtered": int, "tools_rejected": [...]}.
    If ``overwrite`` is True and the slug already exists, the existing
    orchestrator is updated in-place. Otherwise a new slug is generated.

    Validation performed during import:
      - The format marker must be "sagaai_orchestrator/v1".
      - prompt_text is truncated if it exceeds the configured limit.
      - tools are filtered: unknown tool names are silently dropped.
      - For built-in orchestrators (is_builtin=True), prompt_text is
        NEVER overwritten - it always comes from the bundled
        system_prompt.md file.
    """
    # Validate format.
    if not isinstance(data, dict):
        return {"ok": False, "error": "Invalid export data: not a dict.", "slug": ""}
    if data.get("format") != _EXPORT_FORMAT:
        return {"ok": False, "error": f"Unknown format: {data.get('format', 'none')}", "slug": ""}

    raw = str(data.get("slug") or "").strip()
    slug = safe_orchestrator_slug(raw)
    # Reject slugs that contain anything beyond [a-z0-9_] (a path-traversal
    # attempt like '../evil' normalizes to something else and must not be
    # silently accepted as a different slug).
    if not slug or slug != raw.lower():
        return {"ok": False, "error": "Missing or invalid slug in export data.", "slug": ""}

    name = data.get("name", "").strip() or slug
    description = data.get("description", "")
    prompt_text = data.get("prompt_text", "")
    config = data.get("config", {}) or {}
    raw_tools = data.get("tools", []) or []
    max_steps = int(data.get("max_steps", 100) or 100)
    auto_apply = bool(data.get("auto_apply", True))

    # Validate prompt_text length.
    if isinstance(prompt_text, str) and len(prompt_text) > _MAX_PROMPT_TEXT_LENGTH:
        prompt_text = prompt_text[:_MAX_PROMPT_TEXT_LENGTH] + "\n...[truncated on import]...\n"

    # Validate tools against the known catalog.
    tools, tools_rejected = _validate_imported_tools(raw_tools)

    existing = repo_get_orchestrator_with_text(slug)

    if existing:
        if not overwrite:
            # Generate a new slug: append "-2", "-3", etc.
            base = slug.rstrip("_0123456789")
            suffix = 2
            while True:
                candidate = f"{base}_{suffix}"
                if not repo_get_orchestrator_with_text(candidate):
                    slug = candidate
                    break
                suffix += 1
        else:
            # Update existing.
            # For built-in orchestrators, NEVER overwrite prompt_text -
            # it is always refreshed from the bundled file on boot.
            update_kwargs: Dict[str, Any] = dict(
                name=name,
                description=description,
                config=config,
                tools=tools,
                max_steps=max_steps,
                auto_apply=auto_apply,
            )
            if not existing.get("is_builtin"):
                update_kwargs["prompt_text"] = prompt_text
            repo_update_orchestrator(existing["id"], **update_kwargs)

            instructions_imported = _import_instructions(slug, data.get("instructions", []))
            functions_imported = _import_functions(slug, data.get("functions", {}))
            _sync_orchestrator_folder(slug)
            return {
                "ok": True,
                "slug": slug,
                "action": "updated",
                "instructions_imported": instructions_imported,
                "functions_imported": functions_imported,
                "tools_filtered": len(tools),
                "tools_rejected": tools_rejected,
            }

    # Create new.
    orch_id = uuid.uuid4().hex[:8]
    ok = repo_create_orchestrator(
        orchestrator_id=orch_id,
        slug=slug,
        name=name,
        description=description,
        prompt_text=prompt_text,
        config=config,
        tools=tools,
        max_steps=max_steps,
        auto_apply=auto_apply,
        is_builtin=False,
        sort_order=100,
    )
    if not ok:
        return {"ok": False, "error": "Failed to create orchestrator in DB.", "slug": ""}

    instructions_imported = _import_instructions(slug, data.get("instructions", []))
    functions_imported = _import_functions(slug, data.get("functions", {}))
    _sync_orchestrator_folder(slug)
    return {
        "ok": True,
        "slug": slug,
        "action": "created",
        "instructions_imported": instructions_imported,
        "functions_imported": functions_imported,
        "tools_filtered": len(tools),
        "tools_rejected": tools_rejected,
    }


def _import_instructions(slug: str, instructions: list) -> int:
    """Import instruction dicts into the orchestrator folder. Returns count.

    Instructions are saved as orchestrator-specific instructions of *slug*
    (same location export_orchestrator reads them from). Existing ids are
    overwritten with the imported content. The global instructions table is
    never touched by imports.
    """
    imported = 0
    for inst in (instructions or []):
        if not isinstance(inst, dict):
            continue
        iid = str(inst.get("id", "") or "").strip()
        if not iid:
            continue
        ok = save_orchestrator_instruction(
            slug,
            iid,
            name=str(inst.get("name", "") or iid),
            description=str(inst.get("description", "") or ""),
            prompt_text=str(inst.get("prompt_text", "") or ""),
        )
        if ok:
            imported += 1
    return imported


def _import_functions(slug: str, functions: dict) -> int:
    """Import custom functions into the orchestrator folder. Returns count."""
    imported = 0
    if not isinstance(functions, dict):
        return imported
    ensure_orchestrator_dir(slug)
    for fname, code in functions.items():
        if not isinstance(fname, str) or not isinstance(code, str):
            continue
        if save_orchestrator_function(slug, fname, code):
            imported += 1
    return imported


# ─── Orchestrator-specific instructions (folder storage) ────────────────

# Re-exported for convenience and for the UI.

def orch_list_instructions(slug: str) -> list:
    """Return metadata for orchestrator-specific instructions."""
    return list_orchestrator_instructions(slug)


def orch_get_instruction(slug: str, instruction_id: str) -> Optional[dict]:
    """Return a full orchestrator-specific instruction, or None."""
    return get_orchestrator_instruction(slug, instruction_id)


def orch_save_instruction(slug: str, instruction_id: str, name: str,
                          description: str = "", prompt_text: str = "") -> str:
    """Create or update an orchestrator-specific instruction.

    Returns the effective instruction id on success, '' on failure.
    """
    return save_orchestrator_instruction(
        slug, instruction_id, name, description, prompt_text
    )


def orch_delete_instruction(slug: str, instruction_id: str) -> bool:
    """Delete an orchestrator-specific instruction."""
    return delete_orchestrator_instruction(slug, instruction_id)


# ─── Orchestrator-specific functions (folder storage) ────────────────────

def orch_list_functions(slug: str) -> list:
    """Return metadata for custom functions of an orchestrator."""
    return list_orchestrator_functions(slug)


def orch_get_function(slug: str, name: str) -> Optional[dict]:
    """Return a full custom function dict including source code."""
    return get_orchestrator_function(slug, name)


def orch_save_function(slug: str, name: str, code: str) -> bool:
    """Create or update a custom function."""
    return save_orchestrator_function(slug, name, code)


def orch_delete_function(slug: str, name: str) -> bool:
    """Delete a custom function."""
    return delete_orchestrator_function(slug, name)



# ─── Bootstrap ───────────────────────────────────────────────────────────


def ensure_builtin_orchestrators() -> Dict[str, str]:
    """Ensure the built-in 'dev_agent' orchestrator exists in the DB.

    On first run, migrates settings from KV-store (devagent.* keys) and
    system_prompt.md into the orchestrators table.  On subsequent runs,
    always updates the prompt_text from the bundled system_prompt.md file
    so it stays current with the code, and backfills missing config fields
    from the current defaults without overwriting user-chosen values.

    After the built-in DevAgent is ensured, bundled DEFAULT orchestrators
    (defaults/orchestrators/*) are created if their slugs do not exist yet.

    Returns a status dict, e.g. {"dev_agent": "created",
    "ya_agent": "created"} or {"dev_agent": "updated"}.
    """
    from pathlib import Path
    from core.config import load_devagent_config as load_legacy_dev_cfg

    # Load the single bundled system prompt from dev_agent/system_prompt.md.
    # There is intentionally NO defaults/ copy: this file is the one source of
    # truth for the built-in DevAgent prompt.
    prompt_text = ""
    prompt_file = (
        Path(__file__).resolve().parent.parent
        / "dev_agent" / "system_prompt.md"
    )
    try:
        prompt_text = prompt_file.read_text(encoding="utf-8")
    except Exception:
        prompt_text = "You are DevAgent, the embedded developer of SagaAI."

    existing = repo_get_orchestrator_with_text(DEVAGENT_SLUG)

    if existing is None:
        # First boot: migrate from legacy KV config if available.
        legacy = load_legacy_dev_cfg()
        config = {
            "strong_service": legacy.get("strong_service", "") or "DeepSeek",
            "strong_model": legacy.get("strong_model", "") or "deepseek-v4-pro",
            "strong_temperature": float(legacy.get("strong_temperature", 0.4) or 0.4),
            "strong_max_tokens": int(legacy.get("strong_max_tokens") or get_default_strong_max_tokens()) or get_default_strong_max_tokens(),
            "strong_reasoning_effort": "max",
            "weak_service": legacy.get("weak_service", "") or "DeepSeek",
            "weak_model": legacy.get("weak_model", "") or "deepseek-v4-pro",
            "weak_temperature": float(legacy.get("weak_temperature", 0.4) or 0.4),
            "weak_max_tokens": int(legacy.get("weak_max_tokens") or get_default_weak_max_tokens()) or get_default_weak_max_tokens(),
            "weak_reasoning_effort": "max",
            "search_service": legacy.get("search_service", "") or "YandexAI",
            "search_model": legacy.get("search_model", "") or "aliceai-llm-flash",
            "search_temperature": float(legacy.get("search_temperature", 0.3) or 0.3),
            "search_max_tool_calls": int(legacy.get("search_max_tool_calls", 1) or 1),
            "search_reasoning_effort": "high",
            "web_search_prompt": str(legacy.get("web_search_prompt", "") or DEFAULT_WEB_SEARCH_PROMPT),
            "economy_tail_messages": int(legacy.get("economy_tail_messages", _default_economy_tail_messages())
                                         or _default_economy_tail_messages()),
            "economy_cache_enabled": _default_economy_cache_enabled(),
            "economy_cache_multiplier": _default_economy_cache_multiplier(),
            "enabled_skills": [],
        }

        # Build tools list from the tool executor catalog.
        try:
            from dev_agent.tool_executor import TOOL_CATALOG as CORE_TOOLS
            from dev_agent.universal_agent import WORKSPACE_TOOL_CATALOG
            tool_names = [t["name"] for t in CORE_TOOLS] + [t["name"] for t in WORKSPACE_TOOL_CATALOG]
        except Exception:
            tool_names = []

        orch_id = uuid.uuid4().hex[:8]
        ok = repo_create_orchestrator(
            orchestrator_id=orch_id,
            slug=DEVAGENT_SLUG,
            name="DevAgent",
            description="Universal developer orchestrator - creates, debugs and maintains software projects.",
            prompt_text=prompt_text,
            config=config,
            tools=tool_names,
            max_steps=100,
            auto_apply=True,
            is_builtin=True,
            sort_order=200,  # after user orchestrators
        )
        if ok:
            # Write the fresh bundle (orchestrator.json) with current defaults
            # so the folder on disk matches the clean-install state.
            _sync_orchestrator_folder(DEVAGENT_SLUG)
        result = {DEVAGENT_SLUG: "created" if ok else "error"}
        result.update(_ensure_default_orchestrators())
        return result

    # Existing orchestrator - update prompt_text and backfill missing config fields.
    config = existing.get("config", {})
    if not isinstance(config, dict):
        config = {}

    # Backfill defaults for fields that did not exist when the config was created.
    # Do NOT overwrite existing user-chosen values.
    config_defaults = {
        "strong_service": "DeepSeek",
        "strong_model": "deepseek-v4-pro",
        "strong_temperature": 0.4,
        "strong_max_tokens": get_default_strong_max_tokens(),
        "strong_reasoning_effort": "max",
        "weak_service": "DeepSeek",
        "weak_model": "deepseek-v4-pro",
        "weak_temperature": 0.4,
        "weak_max_tokens": get_default_weak_max_tokens(),
        "weak_reasoning_effort": "max",
        "search_service": "YandexAI",
        "search_model": "aliceai-llm-flash",
        "search_temperature": 0.3,
        "search_max_tool_calls": 1,
        "search_reasoning_effort": "high",
        "web_search_prompt": DEFAULT_WEB_SEARCH_PROMPT,
        "economy_tail_messages": _default_economy_tail_messages(),
        "economy_cache_enabled": _default_economy_cache_enabled(),
        "economy_cache_multiplier": _default_economy_cache_multiplier(),
    }

    backfilled = False

    # Add enabled_skills if missing (old configs created before the feature).
    if "enabled_skills" not in config:
        config["enabled_skills"] = []
        backfilled = True

    # Add enabled_connections if missing (old configs created before the feature).
    if "enabled_connections" not in config:
        config["enabled_connections"] = []
        backfilled = True

    # Legacy configs (created before cache-friendly economy mode) stored
    # economy_tail_messages=15 but have no cache fields. Treat that as the
    # old built-in default and upgrade it to the new default (30).
    has_cache_fields = ("economy_cache_enabled" in config) or ("economy_cache_multiplier" in config)
    if not has_cache_fields and config.get("economy_tail_messages") == 15:
        config["economy_tail_messages"] = _default_economy_tail_messages()
        backfilled = True

    for k, v in config_defaults.items():
        cur = config.get(k)
        if cur is None or cur == "":
            config[k] = v
            backfilled = True
        elif k in ("strong_max_tokens", "weak_max_tokens"):
            # A stored 0 means "use the model default", but the required
            # default for the built-in DevAgent is 384000 output tokens.
            # Treat 0 as unset here so buggy first-boot configs are fixed.
            try:
                if int(cur) <= 0:
                    config[k] = v
                    backfilled = True
            except (TypeError, ValueError):
                config[k] = v
                backfilled = True

    if backfilled:
        repo_update_orchestrator(existing["id"], prompt_text=prompt_text, config=config)
        _sync_orchestrator_folder(DEVAGENT_SLUG)
    else:
        repo_update_orchestrator(existing["id"], prompt_text=prompt_text)
    result = {DEVAGENT_SLUG: "updated"}
    result.update(_ensure_default_orchestrators())
    return result


# ─── Backward compatibility ──────────────────────────────────────────────
# load_devagent_config / save_devagent_config are re-exported from core.config
# but now use the orchestrator under the hood.

def load_devagent_config(orch_slug: str = DEVAGENT_SLUG) -> Dict[str, str]:
    """Read DevAgent-compatible config dict from the orchestrator store.

    Returns a flat dict with string values, compatible with the legacy
    ``core.config.load_devagent_config()`` contract.
    """
    orch = get_orchestrator(orch_slug)
    cfg = orch.get("config", {}) if orch else {}
    return {
        "service": cfg.get("strong_service", "") or "DeepSeek",
        "model": cfg.get("strong_model", "") or "deepseek-v4-pro",
        "temperature": str(cfg.get("strong_temperature", 0.2) or 0.2),
        "prompt_text": orch.get("prompt_text", "") if orch else "",
        "strong_service": cfg.get("strong_service", "") or "DeepSeek",
        "strong_model": cfg.get("strong_model", "") or "deepseek-v4-pro",
        "strong_temperature": str(cfg.get("strong_temperature", 0.4) or 0.4),
        "strong_max_tokens": str(cfg.get("strong_max_tokens", 0) or 0),
        "weak_service": cfg.get("weak_service", "") or "DeepSeek",
        "weak_model": cfg.get("weak_model", "") or "deepseek-v4-pro",
        "weak_temperature": str(cfg.get("weak_temperature", 0.4) or 0.4),
        "weak_max_tokens": str(cfg.get("weak_max_tokens", 0) or 0),
        "search_service": cfg.get("search_service", "") or "YandexAI",
        "search_model": cfg.get("search_model", "") or "aliceai-llm-flash",
        "search_temperature": str(cfg.get("search_temperature", 0.3) or 0.3),
        "search_max_tool_calls": str(cfg.get("search_max_tool_calls", 1) or 1),
        "web_search_prompt": get_web_search_prompt(orch_slug),
        "economy_tail_messages": str(get_economy_tail_messages(orch_slug)),
        "economy_cache_enabled": str(get_economy_cache_enabled(orch_slug)),
        "economy_cache_multiplier": str(get_economy_cache_multiplier(orch_slug)),
        "enabled_skills": repr(get_enabled_skills(orch_slug)),
    }


def save_devagent_config(
    service: str = "", model: str = "", temperature: float = 0.2,
    prompt_text: str = "",
    strong_service: str = "", strong_model: str = "",
    strong_temperature: float = 0.2,
    strong_max_tokens: int = 0,
    weak_service: str = "", weak_model: str = "",
    weak_temperature: float = 0.5,
    weak_max_tokens: int = 0,
    search_service: str = "", search_model: str = "",
    search_temperature: float = 0.3,
    search_max_tool_calls: int = 3,
    web_search_prompt: Optional[str] = None,
    economy_tail_messages: Optional[int] = None,
    economy_cache_enabled: Optional[bool] = None,
    economy_cache_multiplier: Optional[int] = None,
) -> bool:
    """Save DevAgent configuration to the orchestrator store.

    Kept for backward compatibility with the existing settings UI.
    """
    orch = get_orchestrator(DEVAGENT_SLUG)
    if orch is None:
        return False
    config = orch.get("config", {})
    config["strong_service"] = strong_service or service
    config["strong_model"] = strong_model or model
    config["strong_temperature"] = strong_temperature or temperature
    config["strong_max_tokens"] = strong_max_tokens or get_default_strong_max_tokens()
    config["weak_service"] = weak_service or strong_service or service
    config["weak_model"] = weak_model or strong_model or model
    config["weak_temperature"] = weak_temperature or 0.5
    config["weak_max_tokens"] = weak_max_tokens or get_default_weak_max_tokens()
    config["search_service"] = search_service or ""
    config["search_model"] = search_model or ""
    config["search_temperature"] = search_temperature
    config["search_max_tool_calls"] = search_max_tool_calls
    if web_search_prompt is not None:
        config["web_search_prompt"] = web_search_prompt
    if economy_tail_messages is not None:
        config["economy_tail_messages"] = economy_tail_messages
    if economy_cache_enabled is not None:
        config["economy_cache_enabled"] = economy_cache_enabled
    if economy_cache_multiplier is not None:
        config["economy_cache_multiplier"] = max(1, int(economy_cache_multiplier))

    ok = repo_update_orchestrator(
        orch["id"],
        prompt_text=prompt_text,
        config=config,
    )
    if ok:
        _sync_orchestrator_folder(DEVAGENT_SLUG)
    return ok


# ─── Legacy aliases (old "skill" terminology) ────────────────────────────────
# Kept so older UI code keeps working; new code must use build_assistant_dicts.
build_skill_dicts = build_assistant_dicts
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
