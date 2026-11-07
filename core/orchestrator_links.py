# -*- coding: utf-8 -*-
"""
core.orchestrator_links - entities an orchestrator is linked to (skills, connections, RAG, delegation).

Part of ``core.orchestrators``, which is the public surface of this
package. A name the tests may replace through the facade is resolved late,
at call time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
from typing import Any, Dict, List

from core.orchestrator_defaults import (
    DEVAGENT_SLUG,
    UNIVERSAL_AGENT_CONFIG_KEY,
    UNIVERSAL_AGENT_SLUG,
)
from core.orchestrator_shared import late

# Resolved on the facade when called, never bound at import time.
_sync_orchestrator_folder = late("_sync_orchestrator_folder")
get_allowed_agents = late("get_allowed_agents")
get_orchestrator = late("get_orchestrator")
repo_update_orchestrator = late("repo_update_orchestrator")


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


def get_enabled_connections_impl(orchestrator_slug: str = DEVAGENT_SLUG) -> List[str]:
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


def get_allowed_agents_impl(orchestrator_slug: str = DEVAGENT_SLUG) -> List[str]:
    """Return the orchestrator slugs this orchestrator may spawn.

    Stored in the config under ``allowed_agents``. The orchestrator's OWN
    slug is always allowed (self-delegation needs no configuration), so it
    is not required in this list. Missing or invalid values yield an empty
    list - which still means "self only", never "nothing".
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    agents = cfg.get("allowed_agents") or []
    if not isinstance(agents, list):
        return []
    out: List[str] = []
    for a in agents:
        if isinstance(a, str) and a.strip() and a.strip() not in out:
            out.append(a.strip())
    return out


def set_allowed_agents(orchestrator_slug: str, agent_slugs: List[str]) -> bool:
    """Store the allowlist of spawnable orchestrators; syncs the folder.

    The orchestrator's own slug is dropped from the stored list: it is
    allowed by definition, and keeping it would confuse the settings UI
    (a checkbox for yourself next to the boxes for other agents).
    """
    normalized: List[str] = []
    for a in (agent_slugs or []):
        if not isinstance(a, str):
            continue
        val = a.strip()
        if not val or val == orchestrator_slug or val in normalized:
            continue
        normalized.append(val)
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg["allowed_agents"] = normalized
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


def get_universal_agent_enabled(orchestrator_slug: str = DEVAGENT_SLUG) -> bool:
    """Return whether the bundled Universal-Agent is offered to this orchestrator.

    The answer comes from ``config.universal_agent_enabled`` and defaults to
    True: the executor is available out of the box, and the user takes that
    right away per orchestrator with a checkbox. An ABSENT key therefore
    means "on" - that is what makes a freshly created employee delegate to
    it without any configuration step.

    A stored non-boolean value (hand-edited folder, older import) is read
    through its truthiness so a recorded decision is never silently lost.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    if not isinstance(cfg, dict):
        return True
    if UNIVERSAL_AGENT_CONFIG_KEY not in cfg:
        return True
    return bool(cfg.get(UNIVERSAL_AGENT_CONFIG_KEY))


def set_universal_agent_enabled(orchestrator_slug: str, enabled: bool) -> bool:
    """Store the Universal-Agent availability decision; syncs the folder.

    Only the explicit decision is written: the ``allowed_agents`` list stays
    exactly what the user chose, so a later settings save cannot lose the
    flag and the flag cannot turn into a stray list entry.
    """
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg[UNIVERSAL_AGENT_CONFIG_KEY] = bool(enabled)
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


def get_effective_agents_impl(orchestrator_slug: str = DEVAGENT_SLUG) -> List[str]:
    """Return every non-self agent this orchestrator may actually spawn.

    The configured ``allowed_agents`` plus the bundled Universal-Agent while
    its flag is on and the employee exists. This - not the raw allowlist -
    is the list the runtime enforces and the prompt advertises, so the two
    can never disagree about who is callable.

    The universal slug is skipped when the orchestrator IS that agent (an
    employee never delegates to itself through this entry - self-delegation
    is a separate, always-available right) and when the employee has been
    deleted from the installation.
    """
    agents = [a for a in get_allowed_agents(orchestrator_slug)
              if a != orchestrator_slug]
    if orchestrator_slug == UNIVERSAL_AGENT_SLUG:
        return agents
    try:
        enabled = get_universal_agent_enabled(orchestrator_slug)
    except Exception:
        enabled = True
    if not enabled or UNIVERSAL_AGENT_SLUG in agents:
        return agents
    try:
        live = get_orchestrator(UNIVERSAL_AGENT_SLUG)
    except Exception:
        live = None
    if not live:
        return agents
    return agents + [UNIVERSAL_AGENT_SLUG]


def get_allowed_assistants_impl(orchestrator_slug: str = DEVAGENT_SLUG) -> List[str]:
    """Return the assistant ids this orchestrator may call one-shot.

    Stored in the config under ``allowed_assistants``. Unlike ``spawn_agent``
    there is no default right to call an assistant: an empty list means
    ``invoke_assistant`` is not registered at all.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    ids = cfg.get("allowed_assistants") or []
    if not isinstance(ids, list):
        return []
    out: List[str] = []
    for a in ids:
        if isinstance(a, str) and a.strip() and a.strip() not in out:
            out.append(a.strip())
    return out


def set_allowed_assistants(orchestrator_slug: str, assistant_ids: List[str]) -> bool:
    """Store the allowlist of callable assistants; syncs the folder.

    Two safeguards live here rather than in the settings UI, because any
    caller (import, API) must get the same protection: ids are normalised
    and, when the assistants table is readable, an id that matches no live
    card is dropped. A stale id would otherwise sit in the config and turn
    every call of that assistant into ``unknown_assistant``.
    """
    normalized: List[str] = []
    for a in (assistant_ids or []):
        if isinstance(a, str) and a.strip() and a.strip() not in normalized:
            normalized.append(a.strip())
    try:
        from core.assistants import get_assistant_by_id
        normalized = [aid for aid in normalized if get_assistant_by_id(aid)]
    except Exception:
        pass
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg["allowed_assistants"] = normalized
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


def get_verifier_agent_impl(orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Return the agent that checks this orchestrator's delegated results.

    Stored in the config under ``verifier_agent``. An empty string means "not
    appointed": the ``verify_result`` tool is not offered, and a call to it is
    refused with ``verifier_not_configured`` rather than falling back to some
    default agent - a silent substitution would hide the fact that nobody is
    checking anything.

    The slug is deliberately NOT required to appear in ``allowed_agents``:
    the right to CHECK a result is a separate right, and folding it into the
    call allowlist would make verification depend on an unrelated list.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    if not isinstance(cfg, dict):
        return ""
    return str(cfg.get("verifier_agent") or "").strip()


def set_verifier_agent(orchestrator_slug: str, agent_slug: str) -> bool:
    """Appoint (or clear) the verifier of an orchestrator; syncs the folder.

    An empty value clears the appointment. A non-empty value must name a LIVE
    orchestrator: a stale slug would make every verification fail with
    ``unknown_agent`` while the settings page shows a choice that looks valid.
    """
    slug = str(agent_slug or "").strip()
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    if slug and get_orchestrator(slug) is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg["verifier_agent"] = slug
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


DELEGATION_LIMIT_KEYS = (
    ("max_depth", 1),
    ("max_active_subagents", 5),
    ("subagent_max_steps", 300),
    ("subagent_budget_tokens", 0),
    ("subagent_timeout_seconds", 3600),
    ("total_subagent_runs", 64),
    # ── Agent dialog ───────────────────────────────────────────────────────
    # Kept in the SAME tuple as the delegation limits: the dialog is part of
    # the delegation surface, and a separate settings block would let the two
    # drift apart in the UI and in the resolver.
    ("dialog_rounds", 5),
    ("question_max_chars", 1500),
    ("answer_max_chars", 4000),
    ("answer_wait_seconds", 900),
    ("max_parked_runs", 8),
)


def get_delegation_limits(orchestrator_slug: str = DEVAGENT_SLUG) -> Dict[str, int]:
    """Return the six delegation limits of an orchestrator.

    Values come from the config (``max_depth``, ``max_active_subagents``,
    ``subagent_max_steps``, ``subagent_budget_tokens``,
    ``subagent_timeout_seconds``, ``total_subagent_runs``) and fall back to
    the documented defaults. Read-only: the runtime uses the same resolver,
    ``core.orchestration.service.read_limits``, so UI and runtime cannot
    disagree about a missing key.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        from core.orchestration.service import read_limits
        return read_limits(cfg)
    except Exception:
        return {key: default for key, default in DELEGATION_LIMIT_KEYS}


def set_delegation_limits(orchestrator_slug: str, limits: Dict[str, Any]) -> bool:
    """Store the six delegation limits; syncs the folder.

    Only known keys are written, values are clamped to their safe ranges
    (a non-positive pool or timeout is meaningless and would break spawning),
    and unknown keys are ignored so a caller cannot smuggle arbitrary config
    through this helper.
    """
    from core.orchestration.service import read_limits
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    incoming = limits if isinstance(limits, dict) else {}
    merged = {key: incoming.get(key, cfg.get(key, default))
              for key, default in DELEGATION_LIMIT_KEYS}
    resolved = read_limits(merged)
    for key, _default in DELEGATION_LIMIT_KEYS:
        cfg[key] = int(resolved[key])
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


def get_disabled_tools_impl(orchestrator_slug: str = DEVAGENT_SLUG) -> List[str]:
    """Return the list of disabled tool names for an orchestrator.

    The info is stored in the orchestrator's config under "disabled_tools".
    An empty list means "all tools enabled". Names are stored as canonical
    tool names (legacy aliases resolved); missing/invalid values are treated
    as an empty list.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    tools = cfg.get("disabled_tools") or []
    if not isinstance(tools, list):
        return []
    out = []
    for t in tools:
        if isinstance(t, str) and t.strip() and t.strip() not in out:
            out.append(t.strip())
    return out


def set_disabled_tools(orchestrator_slug: str, tool_names: List[str]) -> bool:
    """Store the list of disabled tool names for an orchestrator.

    Entries are normalized to canonical tool names via
    ``core.orchestrator_tools.normalize_disabled`` (legacy aliases are
    resolved, so disabling ``list_skills`` disables ``list_assistants``) and
    the orchestrator bundle on disk is synced. Returns True on success.
    """
    from core.orchestrator_tools import normalize_disabled
    normalized = normalize_disabled(tool_names)
    orch = get_orchestrator(orchestrator_slug)
    if orch is None:
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg["disabled_tools"] = normalized
    ok = repo_update_orchestrator(orch["id"], config=cfg)
    if ok:
        _sync_orchestrator_folder(orchestrator_slug)
    return ok


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

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
