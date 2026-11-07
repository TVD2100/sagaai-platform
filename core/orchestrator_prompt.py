# -*- coding: utf-8 -*-
"""
core.orchestrator_prompt - assembly of the system prompt sent to the model.

Part of ``core.orchestrators``, which is the public surface of this
package. A name the tests may replace through the facade is resolved late,
at call time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
from datetime import datetime
from typing import List, Optional, Tuple

from core.orchestrator_defaults import (
    DEVAGENT_SLUG,
    UNIVERSAL_AGENT_SLUG,
    _devagent_default_config,
)
from core.orchestrator_links import get_orchestrator_rag_bases
from core.orchestrator_shared import late
from core.prompt_blocks import (
    DIALOG_RULES_CHILD,
    DIALOG_RULES_PARENT,
    INVOKE_ASSISTANT_INTRO,
    MONTH_NAMES,
    SPAWN_AGENT_INTRO,
    VERIFY_RESULT_INTRO,
    WEEKDAY_NAMES,
    WEEKDAY_NAMES_RU,
    current_date_line,
    format_utc_offset,
)
from core.services import (
    default_reasoning_effort,
    service_supports_reasoning_effort,
)

# Resolved on the facade when called, never bound at import time.
_extend_prompt_with_connections = late("_extend_prompt_with_connections")
_extend_prompt_with_delegation = late("_extend_prompt_with_delegation")
_extend_prompt_with_instructions = late("_extend_prompt_with_instructions")
_extend_prompt_with_memory = late("_extend_prompt_with_memory")
_extend_prompt_with_personal_card = late("_extend_prompt_with_personal_card")
_extend_prompt_with_rag_bases = late("_extend_prompt_with_rag_bases")
_extend_prompt_with_skills = late("_extend_prompt_with_skills")
_extend_prompt_with_tools = late("_extend_prompt_with_tools")
_extend_prompt_with_working_memory = late("_extend_prompt_with_working_memory")
_get_services = late("_get_services")
get_allowed_assistants = late("get_allowed_assistants")
get_disabled_tools = late("get_disabled_tools")
get_effective_agents = late("get_effective_agents")
get_enabled_connections = late("get_enabled_connections")
get_orchestrator = late("get_orchestrator")
list_orchestrators = late("list_orchestrators")


def _extend_prompt_with_personal_card_impl(prompt: str,
                                      orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Append the owner profile card to the system prompt.

    The card holds what the platform knows about the owner - stable rules,
    preferences and environment facts - and is injected for every employee, so
    a new dialog never starts by re-asking the same questions.

    Args:
        prompt: current system prompt text.
        orchestrator_slug: employee that will read the card; kept for symmetry
            with the other extenders and future per-employee filtering.

    Returns:
        The prompt with the ``## Owner profile`` block appended, or the prompt
        unchanged when the card is empty or the memory layer is unavailable.
    """
    try:
        from core import personal_memory as pm
        body = pm.render_owner_card(pm.card_cap())
    except Exception:
        return prompt
    if not body.strip():
        return prompt

    block = "\n".join([
        "## Owner profile",
        "",
        "Facts about the person you work for, maintained across dialogs. Follow "
        "the rules and preferences below without asking again; update them "
        "with `set_personal_memory` when you learn something durable:",
        "",
        body,
    ])
    if prompt.strip():
        return f"{prompt}\n\n{block}"
    return block


def _extend_prompt_with_memory_impl(prompt: str,
                               orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Append the personal-memory rules and the employee's own memory size.

    The block is emitted for EVERY employee, even when no note exists yet: the
    rules tell the employee that keeping notes is part of the job, and a fresh
    install never starts the practice otherwise. The notes themselves are NOT
    inlined - a scope can reach 64 KB - so the block only reports the state of
    the employee's own scope and how to read it.

    Args:
        prompt: current system prompt text.
        orchestrator_slug: employee whose own scope is described.

    Returns:
        The prompt with the ``## Memory`` block appended.
    """
    from core import personal_memory as pm

    scope = pm.format_scope(pm.SCOPE_ORCHESTRATOR, orchestrator_slug)
    size_line = ('Your own scope `current` is empty - add the first note worth '
                 'keeping.')
    try:
        meta = pm.memory_meta(scope)
    except Exception:
        meta = {}
    if meta.get('exists'):
        size = int(meta.get('bytes') or 0)
        limit = int(meta.get('max_bytes') or 0)
        revision = meta.get('revision', 0)
        updated = meta.get('updated_at') or 'unknown'
        size_line = (f'Your own scope `current` holds {size} of {limit} bytes '
                     f'of notes (revision {revision}, last updated {updated}); '
                     'its text is not inlined here.')

    lines = [
        '## Memory',
        '',
        'Your memory outlives a single dialog. Two stores exist and never '
        'merge: personal memory holds HOW to work (agreements, preferences, '
        'limits), and the working-data folder holds the CONTENT of the work '
        '(files, registries, journals).',
        '',
        size_line,
        '',
        f'Scopes: `current` - your own notes (`{scope}`); `user` - the owner '
        'profile shared by every employee; `project` - notes about the active '
        'working folder.',
        '',
        '- Read `current` with `get_personal_memory` before a task that may '
        'depend on earlier decisions; read `user` when you need the rules or '
        'preferences of the owner.',
        '- Write a durable note with `set_personal_memory` (mode `append`) '
        'when the owner corrects your approach, when you settle a decision '
        'that will matter next time, or when you discover a limit worth '
        'remembering. Skip one-off details and anything already visible in '
        'this prompt.',
        '- `user` is the card of the owner: extend it with `append`. A rewrite '
        'with mode `replace` is refused unless the owner asked for it and the '
        'call passes `overwrite_owner_profile=true`.',
        '- Keep notes short and factual; never store credentials there.',
    ]
    block = '\n'.join(lines)
    if prompt.strip():
        return f'{prompt}\n\n{block}'
    return block


def _extend_prompt_with_working_memory_impl(prompt: str,
                                       orchestrator_slug: str = DEVAGENT_SLUG,
                                       workspace: str = "") -> str:
    """Append the ``## Working memory`` block - where the role's data lives.

    The block is emitted for EVERY orchestrator: the path of the working-data
    folder and the way to address it are platform facts, not role specifics.
    Without it every new employee had to be told the path in its own prompt,
    so the same fact drifted out of sync with the resolver and a role that
    forgot the line wrote nowhere. The role's prompt keeps only what is
    genuinely its own - the unit of work, the file layout, the update rules.

    Args:
        prompt: current system prompt text.
        orchestrator_slug: employee whose folder is described.
        workspace: active project folder; empty means "no project selected".

    Returns:
        The prompt with the block appended, or the prompt unchanged when the
        resolver is unavailable (the prompt must never break over metadata).
    """
    try:
        from core import orchestrator_folders as of
        resolved = of.resolve_data_dir(orchestrator_slug, workspace)
    except Exception:
        return prompt
    path = str(resolved.get("path") or "")
    if not path:
        return prompt

    source = str(resolved.get("source") or "default")
    scope = str(resolved.get("scope") or "central")
    lines = [
        "## Working memory",
        "",
        "Where the CONTENT of your work lives - registries, cards, journals. "
        "It is not the same as personal memory: personal memory keeps HOW to "
        "work, this folder keeps WHAT you work on.",
        "",
        f"- folder: `{path}`",
        f"- set by: `{source}` (scope: `{scope}`)",
        "- reach it with the file tools (read_file, propose_file, apply_patch, "
        "verify_file, list_files, search_in_files) using the `memory:` prefix "
        "- memory:registry.md, memory:projects/<key>/card.md - or the absolute "
        "path above.",
        "- the folder does not depend on the project a dialog works in, so the "
        "same memory is available in every dialog, even before a project "
        "folder is chosen.",
        "- the file layout, the record formats and the update rules are yours "
        "to define - see the memory section of this prompt.",
    ]
    legacy = resolved.get("legacy")
    if isinstance(legacy, dict) and legacy.get("files") and not legacy.get("is_active"):
        lines.append(
            f"- a folder from the previous layout still holds {legacy.get('files')} "
            f"file(s): `{legacy.get('path')}`. Do not move or delete it on your "
            "own; report it to the owner."
        )
    if not resolved.get("exists"):
        lines.append(
            "- the folder does not exist yet: create it with the first write."
        )

    block = "\n".join(lines)
    if prompt.strip():
        return f"{prompt}\n\n{block}"
    return block


def _extend_prompt_with_connections_impl(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Append a metadata block describing enabled connections to the prompt.

    The block lists each enabled connection (service, name, account - never
    the token) and the connection tools (GitHub REST, SSH) available to the
    orchestrator with their signatures. Returns the original prompt when no
    connections are enabled or on error (best effort).
    """
    enabled = get_enabled_connections(orchestrator_slug)
    if not enabled:
        return prompt
    try:
        from core.connectors import get_connection
    except Exception:
        return prompt

    # Determine which tool sets the enabled connections need.
    services = set()
    conn_lines = []
    for conn_id in enabled:
        conn = get_connection(conn_id)
        if not isinstance(conn, dict):
            continue
        service = str(conn.get("service") or "?")
        services.add(service)
        name = str(conn.get("name") or "?")
        account = str(conn.get("account") or "")
        line = f"- `{conn_id}` - {service} ({name})"
        if account:
            line += f", account: {account}"
        if str(conn.get("credentials_status") or "") == "pending":
            line += " (credentials pending - fill them on the Connectors page)"
        conn_lines.append(line)

    lines = [
        "## Available service connections",
        "",
        "The following external service connections are enabled for you. "
        "Use them via the connection tools listed below; tokens and "
        "credentials are handled by the platform and are never passed to "
        "you:",
        "",
    ]
    lines.extend(conn_lines)

    lines.append("")
    lines.append("Available connection tools:")
    try:
        if "github_rest" in services:
            from core.github_tools_rest import get_tools as get_github_rest_tools
            for tool in get_github_rest_tools():
                lines.append(f"- `{tool['name']}` - {tool['desc']}")
        if "ssh" in services:
            from core.ssh_tools import get_tools as get_ssh_tools
            for tool in get_ssh_tools():
                lines.append(f"- `{tool['name']}` - {tool['desc']}")
    except Exception:
        pass

    # Compact usage notes so orchestrators can call the tools correctly even
    # without loading the full connector instruction.
    lines.append("")
    lines.append("Quick usage notes:")
    lines.append("- Always pass `connector_id` (from the list above) as the first argument.")
    lines.append("- Each connection keeps a memory notes file: read it with `get_connection_memory` (and load the matching connector instruction) before working with the connection; after finishing a task, offer to update that memory with `set_connection_memory` (newly discovered limits, paths, rules - never secrets).")
    if "github_rest" in services:
        lines.append("- `repo` accepts `owner/repo` or a bare repo name of the authenticated user.")
        lines.append("- New repo names must be lowercase, without spaces.")
        lines.append("- `ghr_upload_file` creates a NEW file; use `ghr_update_file` to change an existing file.")
        lines.append("- Prefer `ghr_batch_commit` / `ghr_batch_commit_paths` / `ghr_batch_upsert` to publish many files in ONE commit.")
        lines.append("- Before updating a file, read it with `ghr_read_file` first.")
        lines.append("- For the full usage guide, load the `github_connector` instruction if it is listed in `## Available instructions` of this orchestrator.")
    if "ssh" in services:
        lines.append("- `ssh_exec` runs one shell command and returns the exit status, stdout and stderr (each truncated to 100 KB).")
        lines.append("- `ssh_upload_file` streams a local workspace file to the server (capped at 50 MB) and verifies the transfer by sha256.")
        lines.append("- `ssh_write_file` overwrites the remote file (max 1 MB); pass `create_dirs=true` to create the parent chain.")
        lines.append("- Before overwriting a file, read it with `ssh_read_file` first.")
        lines.append("- For the full usage guide, load the `ssh_connector` instruction if it is listed in `## Available instructions` of this orchestrator.")

    block = "\n".join(lines)
    if prompt.strip():
        return f"{prompt}\n\n{block}"
    return block


def _extend_prompt_with_tools_impl(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Append the '## Available tools' block to an orchestrator prompt.

    Lists every tool the orchestrator may call (system tools, enabled
    connection tools and custom functions), excluding the tools disabled via
    the ``disabled_tools`` config key. Returns the original prompt when the
    catalog is empty or unreadable (best effort).
    """
    try:
        from core.orchestrator_tools import render_available_tools_block
        disabled = set(get_disabled_tools(orchestrator_slug))
        block = render_available_tools_block(orchestrator_slug, disabled=disabled)
        if not block:
            return prompt
        if prompt.strip():
            return f"{prompt}\n\n{block}"
        return block
    except Exception:
        return prompt


def _extend_prompt_with_rag_bases_impl(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG) -> str:
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
        # with_stats=False: the prompt block only needs metadata (name,
        # status, active); opening every SQLite index here slows down
        # orchestrator renders with large bases.
        bases = list_bases_with_activity(with_stats=False)
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
            mode = str(b.get("search_mode") or "").strip().lower() or "bm25"
            flag = "" if active else " [INACTIVE: provider API keys required]"
            rows.append(f"- `{bslug}` — {name} (status: {status}, search: {mode}){flag}")
        if not rows:
            return prompt
        block = (
            "## Available RAG knowledge bases\n\n"
            "Use only these bases with the `rag_search` tool. Bases not listed "
            "here are forbidden even if you know their identifiers. The "
            "'search' tag is the base's search mode (bm25 = own lexical "
            "index, no API keys; vector = embeddings; hybrid = both).\n\n"
            + "\n".join(rows)
        )
        if prompt.strip():
            return f"{prompt}\n\n{block}"
        return block
    except Exception:
        return prompt


def _delegation_availability(orch_slug: str, enabled) -> Tuple[bool, bool, bool]:
    """Return ``(agents, assistants, verify)`` availability flags for *orch_slug*.

    *enabled* is the ``is_conditional_tool_enabled`` probe; a failure keeps the
    historical default of each flag.
    """
    try:
        agents = bool(enabled("spawn_agent", orch_slug))
    except Exception:
        agents = True
    try:
        assistants = bool(enabled("invoke_assistant", orch_slug))
    except Exception:
        assistants = False
    try:
        verify = bool(enabled("verify_result", orch_slug))
    except Exception:
        verify = False
    return agents, assistants, verify


def _agent_delegation_lines(orch_slug: str) -> List[str]:
    """Return the block listing the agents this orchestrator may spawn."""
    entries: List[str] = []
    own_name = ""
    try:
        own = get_orchestrator(orch_slug)
        own_name = str((own or {}).get("name") or "")
    except Exception:
        own_name = ""
    entries.append("- `%s` - %s (you; self-delegation is always allowed)"
                   % (orch_slug, own_name or "this orchestrator"))
    try:
        live = {str(o.get("slug") or ""): str(o.get("name") or "")
                for o in list_orchestrators()}
    except Exception:
        live = {}
    for slug in get_effective_agents(orch_slug):
        if slug == orch_slug:
            continue
        suffix = ""
        if slug == UNIVERSAL_AGENT_SLUG:
            suffix = (" (available by default; your user can switch "
                      "this right off in the delegation settings)")
        entries.append("- `%s` - %s%s" % (slug, live.get(slug) or "name unknown",
                                          suffix))
    lines = list(SPAWN_AGENT_INTRO)
    lines.extend(entries)
    return lines


def _assistant_delegation_lines(orch_slug: str) -> List[str]:
    """Return the block listing the assistants this orchestrator may call."""
    a_entries: List[str] = []
    try:
        from core.assistants import load_assistants_index
        cards = {str(c.get("id") or ""): str(c.get("name") or "")
                 for c in (load_assistants_index() or [])}
    except Exception:
        cards = {}
    for aid in get_allowed_assistants(orch_slug):
        a_entries.append("- `%s` - %s" % (aid, cards.get(aid) or "name unknown"))
    lines = list(INVOKE_ASSISTANT_INTRO)
    lines.extend(a_entries)
    return lines


def _verify_delegation_lines() -> List[str]:
    """Return the block explaining how delegated results are verified."""
    return list(VERIFY_RESULT_INTRO)


def _extend_prompt_with_delegation_impl(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Append a block listing the agents and assistants this orchestrator may call.

    The block answers the question the model otherwise spends a step probing:
    "who exactly can I delegate to, and under what name?". Every entry carries
    the call argument (``agent_slug`` or ``assistant_id``) next to the human
    name, because the tool contract wants the identifier while the model
    reasons about the name.

    Self-delegation is always listed: an orchestrator may always spawn itself,
    so the list is never empty even with no other agent configured.

    Returns the original prompt on error or when neither delegation tool is
    available (best effort - the orchestrator still works without the block).
    """
    try:
        from core.orchestrator_tools import is_conditional_tool_enabled
    except Exception:
        return prompt

    agents_available, assistants_available, verify_available = _delegation_availability(
        orchestrator_slug, is_conditional_tool_enabled)

    lines: List[str] = []
    if agents_available:
        lines.extend(_agent_delegation_lines(orchestrator_slug))

    if assistants_available:
        if lines:
            lines.append("")
        lines.extend(_assistant_delegation_lines(orchestrator_slug))

    if verify_available:
        if lines:
            lines.append("")
        lines.extend(_verify_delegation_lines())

    if not lines:
        return prompt
    block = "\n".join(lines)
    if prompt.strip():
        return f"{prompt}\n\n{block}"
    return block


def _extend_prompt_with_skills_impl(prompt: str, enabled_skills: List[str]) -> str:
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


def _extend_prompt_with_instructions_impl(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG) -> str:
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


_WEEKDAY_NAMES = WEEKDAY_NAMES


_WEEKDAY_NAMES_RU = WEEKDAY_NAMES_RU


_MONTH_NAMES = MONTH_NAMES


def _format_utc_offset(moment: datetime) -> str:
    """Return the UTC offset of *moment* as ``UTC+HH:MM``."""
    return format_utc_offset(moment)


def _current_date_line(moment: datetime) -> str:
    """Return the one-line date statement for *moment*.

    The weekday and the month are spelled out so the model does not derive
    them from a numeric date - a frequent source of off-by-one errors.
    """
    return current_date_line(moment)


def _extend_prompt_with_current_date(prompt: str,
                                     orchestrator_slug: str = DEVAGENT_SLUG,
                                     now: Optional[datetime] = None) -> str:
    """Append the ``## Current date`` block - the only source of "today".

    A model has no clock: without this block every date-dependent answer is a
    guess or a reading of a file that recorded the date it was WRITTEN, not
    the current one. The block is emitted for EVERY orchestrator, so no role
    prompt has to carry the date itself.

    The block states the date only, with no usage instructions: the system
    prompt is already large and the fact speaks for itself. ``
    build_assistant_dicts`` appends it LAST - the only block that changes over
    time sits at the tail, so the provider's cached prefix survives until the
    date rolls over.

    Args:
        prompt: current system prompt text.
        orchestrator_slug: employee whose config may switch the block off.
        now: moment to report; defaults to the current local time. Tests pass
            a fixed value to keep the output deterministic.

    Returns:
        The prompt with the block appended, or the prompt unchanged when the
        orchestrator disabled it (``current_date_enabled: false``) or the
        moment cannot be resolved (the prompt must never break over metadata).
    """
    try:
        orch = get_orchestrator(orchestrator_slug)
        cfg = (orch or {}).get("config", {}) or {}
        flag = cfg.get("current_date_enabled", True)
        if isinstance(flag, str):
            enabled = flag.strip().lower() not in ("0", "false", "no", "off")
        else:
            enabled = bool(flag)
        if not enabled:
            return prompt

        moment = now if now is not None else datetime.now()
        if moment.tzinfo is None:
            # A naive value is local by definition: attach the system zone so
            # the offset line reports what the clock actually means.
            moment = moment.astimezone()
        tz_name = moment.tzname() or ""
        tz_part = _format_utc_offset(moment)
        tz_text = "%s (%s)" % (tz_name, tz_part) if tz_name else tz_part
        lines = [
            "## Current date",
            "",
            _current_date_line(moment),
            "Installation timezone: %s." % tz_text,
        ]
    except Exception:
        return prompt

    block = "\n".join(lines)
    if prompt.strip():
        return f"{prompt}\n\n{block}"
    return block


_DIALOG_RULES_PARENT = DIALOG_RULES_PARENT


_DIALOG_RULES_CHILD = DIALOG_RULES_CHILD


def _extend_prompt_with_dialog(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG,
                               role: str = "parent") -> str:
    """Append the rules of the agent dialog for the given role.

    ``role`` is ``parent`` for an orchestrator that may answer a parked child
    and ``child`` for the subagent that may ask. The block is best effort: a
    config read failure returns the prompt unchanged rather than breaking it.
    """
    try:
        from core.orchestrator_tools import is_conditional_tool_enabled
        tool = "answer_child" if role == "parent" else "ask_parent"
        if not is_conditional_tool_enabled(tool, orchestrator_slug):
            return prompt
    except Exception:
        return prompt
    lines = _DIALOG_RULES_PARENT if role == "parent" else _DIALOG_RULES_CHILD
    block = "".join(lines).rstrip()
    if prompt.strip():
        return prompt + "\n\n" + block
    return block


def build_assistant_dicts_impl(orchestrator_slug: str = DEVAGENT_SLUG,
                          workspace: str = "",
                          dialog_role: str = "parent") -> Tuple[dict, dict]:
    """Build (strong_assistant, weak_assistant) from an orchestrator's config.

    Assistant dicts are compatible with ``core.api_layer.send_request``.
    Falls back to DevAgent defaults if the orchestrator is missing.

    If the orchestrator has enabled standardized skills (config key
    "enabled_skills"), their metadata is appended to the system prompt sent
    to the model.

    *workspace* is the project folder of the calling dialog. It is needed to
    expand a project-relative ``MEMORY_DIR`` override; empty means "no project
    selected", which the working-memory block reports honestly instead of
    guessing a path.
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
    # Append the owner profile card shared by all employees.
    prompt = _extend_prompt_with_personal_card(prompt, orchestrator_slug)
    # Append the employee's own memory size and the scope tools it may use.
    prompt = _extend_prompt_with_memory(prompt, orchestrator_slug)
    # Append the working-data folder: path plus the way to address it. This is
    # a platform fact, so no employee prompt has to repeat it (and drift).
    prompt = _extend_prompt_with_working_memory(prompt, orchestrator_slug, workspace)
    # Append enabled external service connections (connectors).
    prompt = _extend_prompt_with_connections(prompt, orchestrator_slug)
    # Append assigned RAG knowledge bases (DevAgent: all; others: assigned).
    prompt = _extend_prompt_with_rag_bases(prompt, orchestrator_slug)
    # Append the agents and assistants this orchestrator may delegate to.
    prompt = _extend_prompt_with_delegation(prompt, orchestrator_slug)
    # Append the rules of the agent dialog before the tool catalog: the rules
    # talk about the two tools, and a reader who meets the rules first already
    # knows what the catalog entries are for. The dialog role decides the
    # text: a top-level dialog answers its children, a child asks its parent.
    prompt = _extend_prompt_with_dialog(prompt, orchestrator_slug,
                                        role=dialog_role)
    # Append the available-tools catalog (system + custom functions),
    # excluding disabled tools.
    prompt = _extend_prompt_with_tools(prompt, orchestrator_slug)
    # Append the current date LAST: it is the only block that changes over
    # time, so keeping it at the tail preserves the provider's cached prefix
    # until the date rolls over.
    prompt = _extend_prompt_with_current_date(prompt, orchestrator_slug)

    strong_svc = cfg.get("strong_service", "") or cfg.get("service", "DeepSeek")
    strong_mdl = cfg.get("strong_model", "") or cfg.get("model", "deepseek-flash")
    strong_temp = float(cfg.get("strong_temperature", 0.2) or 0.2)
    strong_max_tokens = int(cfg.get("strong_max_tokens", 0) or 0)

    services = _get_services()
    strong_svc_def = services.get(strong_svc, {})

    def _resolve_effort(cfg_key: str, svc_def: dict, strong: bool, model_id: str = "") -> str:
        raw = cfg.get(cfg_key, "")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
        if service_supports_reasoning_effort(svc_def):
            return default_reasoning_effort(svc_def, strong=strong, model=model_id) or ""
        return ""

    strong_effort = _resolve_effort("strong_reasoning_effort", strong_svc_def, True, strong_mdl)

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
    # Single-model mode: the weak alias receives an identical copy of the
    # main assistant so legacy consumers keep working unchanged.
    weak = dict(strong)
    return strong, weak

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
