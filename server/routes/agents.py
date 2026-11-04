"""
server.routes.agents - who may be addressed, and what they are.

Read-only in v1. The listing is also the discovery mechanism for a valid
``agent_slug``: a caller sees exactly the employees its token may launch,
so a slug it can read is a slug it can use.

Prompts are withheld unless explicitly asked for. They are the largest field
of the row, they carry the platform conventions and they are not needed to
decide whether to launch the employee; a client that wants them says so.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query

from core.gateway import entities as gateway_entities
from server import deps, schemas

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=schemas.AgentListOut,
            summary="Employees this token may address")
def list_agents(scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Return the employees visible to the caller."""
    cards = gateway_entities.list_orchestrators(allowed_slugs=scope)
    items = [schemas.agent_out(card) for card in cards]
    return schemas.AgentListOut(items=items, total=len(items)).model_dump()


@router.get("/agents/{slug}", response_model=schemas.AgentDetailOut,
            summary="One employee")
def get_agent(slug: str,
              with_prompt: bool = Query(default=False),
              scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Return one employee; the prompt only when asked for."""
    detail = gateway_entities.get_orchestrator(slug, with_prompt=with_prompt,
                                               allowed_slugs=scope)
    return schemas.agent_detail_out(detail)


@router.get("/assistants", response_model=schemas.AssistantListOut,
            summary="Assistants of the installation")
def list_assistants(scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Return every assistant.

    No scope filter: an assistant is not an employee, carries no thread and
    cannot be launched over HTTP in v1, so there is nothing to restrict.
    """
    cards = gateway_entities.list_assistants()
    items = [schemas.assistant_out(card) for card in cards]
    return schemas.AssistantListOut(items=items, total=len(items)).model_dump()


@router.get("/assistants/{assistant_id}", response_model=schemas.AssistantDetailOut,
            summary="One assistant")
def get_assistant(assistant_id: str,
                  with_prompt: bool = Query(default=False),
                  scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Return one assistant; the prompt only when asked for."""
    detail = gateway_entities.get_assistant(assistant_id, with_prompt=with_prompt)
    return schemas.assistant_detail_out(detail)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
