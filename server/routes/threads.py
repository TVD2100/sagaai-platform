"""
server.routes.threads - read the dialogs of the installation.

The scope is applied HERE, in the folder, not in the route: a thread outside
the token scope answers ``404``, exactly like a thread that does not exist.
A ``403`` would confirm that the id is real, which is precisely what an
enumeration wants to learn.

Paging is offset/limit with a hard ceiling. Search is part of v1 only for
message bodies: listing is a cheap index read, while a search walks the
messages of every thread it is allowed to see.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query

from core.gateway import threads as gateway_threads
from server import deps, schemas

router = APIRouter(tags=["threads"])


@router.get("/threads", response_model=schemas.ThreadListOut,
            summary="Dialogs this token may read")
def list_threads(slugs: str = Query(default="",
                                    description="Comma-separated employee slugs."),
                 date_from: str = Query(default=""),
                 date_to: str = Query(default=""),
                 date_field: str = Query(default="updated"),
                 limit: int = Query(default=50, ge=1, le=200),
                 offset: int = Query(default=0, ge=0),
                 order: str = Query(default="desc"),
                 scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Return one page of dialogs visible to the caller."""
    wanted = [item.strip() for item in str(slugs or "").split(",") if item.strip()]
    page = gateway_threads.list_threads(
        slugs=wanted or None,
        date_from=date_from, date_to=date_to, date_field=date_field,
        limit=limit, offset=offset, order=order,
        allowed_slugs=scope,
    )
    return schemas.ThreadListOut(
        items=[schemas.thread_out(item) for item in page.items],
        total=page.total, offset=page.offset, limit=page.limit,
        truncated=page.truncated,
    ).model_dump()


@router.get("/threads/{thread_id}", response_model=schemas.ThreadWindowOut,
            summary="Messages of one dialog")
def read_thread(thread_id: str,
                offset: int = Query(default=0, ge=0),
                limit: int = Query(default=50, ge=1, le=200),
                include_tool_results: bool = Query(default=False),
                scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Return one window of a dialog's messages."""
    key = deps.thread_id_path(thread_id)
    window = gateway_threads.read_messages(
        key, offset=offset, limit=limit,
        include_tool_results=include_tool_results, allowed_slugs=scope,
    )
    return schemas.ThreadWindowOut(
        thread=schemas.thread_out(window.thread) if window.thread else None,
        messages=[schemas.message_out(item) for item in window.messages],
        total=window.total, offset=window.offset, limit=window.limit,
        remaining=window.remaining,
    ).model_dump()


@router.get("/search", response_model=schemas.SearchOut,
            summary="Search message bodies")
def search(query: str = Query(min_length=1),
           slugs: str = Query(default=""),
           regex: bool = Query(default=False),
           include_tool_results: bool = Query(default=False),
           max_results: int = Query(default=20, ge=1, le=100),
           scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Search the messages of the dialogs this token may read."""
    wanted = [item.strip() for item in str(slugs or "").split(",") if item.strip()]
    page = gateway_threads.search_messages(
        query, slugs=wanted or None, regex=regex,
        include_tool_results=include_tool_results,
        max_results=max_results, allowed_slugs=scope,
    )
    return schemas.SearchOut(hits=page.hits, total=page.total,
                             query=page.query).model_dump()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
