"""
core.gateway.entities - employees and assistants as transport cards.

Read-only by design. Creating an employee from a transport would need slug
validation, folder synchronisation with the marketplace bundle, secret
handling and conflict rules; none of that belongs in v1 of the API, and a
half-implemented create is worse than none.

The two entities disagree about one field name and this module fixes it: an
assistant prompt is stored under ``text``, an orchestrator prompt under
``prompt_text``. A transport that had to know which entity it was reading
would get it wrong on the first attempt, so both are normalised here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.gateway import errors
from core.gateway.dto import (
    AssistantCard,
    AssistantDetail,
    OrchestratorCard,
    OrchestratorDetail,
)

log = logging.getLogger("core.gateway.entities")


# ─── Orchestrators ──────────────────────────────────────────────────────────


def _orchestrator_card(row: Dict[str, Any]) -> OrchestratorCard:
    """Build an ``OrchestratorCard`` from a stored row."""
    row = row if isinstance(row, dict) else {}
    tools = row.get("tools") or []
    try:
        max_steps = int(row.get("max_steps") or 0)
    except (TypeError, ValueError):
        max_steps = 0
    return OrchestratorCard(
        slug=str(row.get("slug") or ""),
        name=str(row.get("name") or ""),
        description=str(row.get("description") or ""),
        is_builtin=bool(row.get("is_builtin")),
        tools=[str(t) for t in tools] if isinstance(tools, (list, tuple)) else [],
        max_steps=max_steps,
        auto_apply=bool(row.get("auto_apply", True)),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def list_orchestrators(*,
                       allowed_slugs: Optional[List[str]] = None
                       ) -> List[OrchestratorCard]:
    """Return the employees a caller may address.

    ``allowed_slugs`` is the token scope: ``None`` means the platform owner,
    ``["*"]`` means every employee, an empty list means none. A transport
    caller sees only what it may launch, so the listing doubles as the
    discovery of a valid ``agent_slug``.
    """
    from core.orchestrators import list_orchestrators as _list

    try:
        rows = list(_list() or [])
    except Exception as exc:
        log.warning("orchestrator listing failed: %s", exc)
        return []
    cards = [_orchestrator_card(row) for row in rows]
    if allowed_slugs is None:
        return cards
    allowed = {str(s).strip() for s in allowed_slugs if str(s).strip()}
    if not allowed:
        return []
    if "*" in allowed:
        return cards
    return [card for card in cards if card.slug in allowed]


def get_orchestrator(slug: str, *, with_prompt: bool = False,
                     allowed_slugs: Optional[List[str]] = None
                     ) -> OrchestratorDetail:
    """Return one employee, with or without its prompt.

    The prompt is withheld by default: it is the largest field of the row,
    it carries the platform conventions and it is not needed to decide
    whether to launch the employee.
    """
    from core.orchestrators import get_orchestrator as _get

    key = str(slug or "").strip()
    if not key:
        raise errors.invalid_request("agent slug is required", field="slug")
    if allowed_slugs is not None:
        allowed = {str(s).strip() for s in allowed_slugs if str(s).strip()}
        if "*" not in allowed and key not in allowed:
            raise errors.agent_not_allowed(key, allowed=sorted(allowed))
    try:
        row = _get(key)
    except Exception as exc:
        log.warning("orchestrator read failed for %s: %s", key, exc)
        row = None
    if not row:
        raise errors.unknown_agent(key)
    card = _orchestrator_card(row)
    config = row.get("config") if isinstance(row, dict) else {}
    return OrchestratorDetail(
        card=card,
        prompt_text=str(row.get("prompt_text") or "") if with_prompt else "",
        config=dict(config) if isinstance(config, dict) else {},
    )


def orchestrator_config(slug: str) -> Dict[str, Any]:
    """Return the config dict of an employee, or an empty dict.

    Launch limits are read from here; a missing employee is NOT an error at
    this level - the caller has already validated the slug and only needs
    the limits it would otherwise take from the defaults.
    """
    from core.orchestrators import get_orchestrator as _get

    try:
        row = _get(slug) or {}
    except Exception as exc:
        log.warning("orchestrator config read failed for %s: %s", slug, exc)
        return {}
    config = row.get("config") if isinstance(row, dict) else {}
    return dict(config) if isinstance(config, dict) else {}


def auto_apply_of(slug: str) -> bool:
    """Return the ``auto_apply`` flag stored on the employee card.

    Both existing launch paths pin this to True, which silently overrides
    the card. A transport must honour the stored value: an agent that was
    explicitly forbidden to apply edits must not apply them because it was
    started over HTTP.
    """
    from core.orchestrators import get_orchestrator as _get

    try:
        row = _get(slug) or {}
    except Exception:  # pragma: no cover - defensive
        return False
    return bool((row or {}).get("auto_apply", True))


# ─── Assistants ─────────────────────────────────────────────────────────────


def _assistant_card(row: Dict[str, Any]) -> AssistantCard:
    """Build an ``AssistantCard`` from a stored row."""
    row = row if isinstance(row, dict) else {}
    tools = row.get("tools") or []
    try:
        temperature = float(row.get("temperature") or 0.0)
    except (TypeError, ValueError):
        temperature = 0.0
    return AssistantCard(
        assistant_id=str(row.get("id") or row.get("assistant_id") or ""),
        slug=str(row.get("slug") or ""),
        name=str(row.get("name") or ""),
        description=str(row.get("description") or ""),
        service=str(row.get("service") or ""),
        model=str(row.get("model") or ""),
        temperature=temperature,
        tools=[str(t) for t in tools] if isinstance(tools, (list, tuple)) else [],
        max_tool_calls=row.get("max_tool_calls"),
        max_tokens=row.get("max_tokens"),
        reasoning_effort=str(row.get("reasoning_effort") or ""),
        created_at=str(row.get("created_at") or ""),
    )


def list_assistants() -> List[AssistantCard]:
    """Return every assistant of the installation."""
    from core.assistants import load_assistants_index

    try:
        rows = list(load_assistants_index() or [])
    except Exception as exc:
        log.warning("assistant listing failed: %s", exc)
        return []
    return [_assistant_card(row) for row in rows]


def get_assistant(assistant_id: str, *, with_prompt: bool = False
                  ) -> AssistantDetail:
    """Return one assistant, with or without its prompt."""
    from core.assistants import get_assistant_by_id

    key = str(assistant_id or "").strip()
    if not key:
        raise errors.invalid_request("assistant id is required",
                                     field="assistant_id")
    try:
        row = get_assistant_by_id(key)
    except Exception as exc:
        log.warning("assistant read failed for %s: %s", key, exc)
        row = None
    if not row:
        raise errors.GatewayError("unknown_assistant",
                                  "Unknown assistant: " + key,
                                  details={"assistant_id": key})
    card = _assistant_card(row)
    # The storage key is ``text``; the card says ``prompt_text``. One
    # normalisation, here, so no transport ever sees both names.
    prompt = row.get("text") if isinstance(row, dict) else ""
    if prompt is None and isinstance(row, dict):
        prompt = row.get("prompt_text")
    return AssistantDetail(card=card,
                           prompt_text=str(prompt or "") if with_prompt else "")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
