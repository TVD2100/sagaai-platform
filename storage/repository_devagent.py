# -*- coding: utf-8 -*-
"""
storage.repository_devagent - CRUD functions for DevAgent threads and messages.

Uses the isolated DevAgent database (devagent.db) via ``get_devagent_session()``.
All public functions accept/return plain dicts or primitives (no ORM objects leak out).

Each thread is associated with an orchestrator via ``assistant_id`` (slug) and
``assistant_name`` (display name). This enables per-orchestrator history.
Since the workspace-restoration feature, each thread also persists the LAST
active workspace (``workspace`` / ``target_file``) so reopening a saved dialog
switches DevAgent back to the correct project folder.
"""
import json
from datetime import datetime
from storage.db import get_devagent_session
from storage.models import Thread, Message


# ─── Threads (DevAgent DB) ────────────────────────────────────────────────────


def repo_devagent_create_thread(thread_id: str, title: str = "",
                                orchestrator_slug: str = None,
                                orchestrator_name: str = "DevAgent",
                                workspace: str = None,
                                target_file: str = None) -> bool:
    """Insert a new DevAgent Thread row. Returns True on success.

    The orchestrator slug is stored in ``assistant_id`` so we can filter by it.
    The orchestrator display name is stored in ``assistant_name``.
    ``workspace`` / ``target_file`` are optional and store the active project
    folder (and optional single-file target) at thread creation.
    """
    try:
        with get_devagent_session() as s:
            now = datetime.now().isoformat()
            th = Thread(
                thread_id=thread_id,
                assistant_id=orchestrator_slug or None,
                assistant_name=orchestrator_name or "DevAgent",
                title=title,
                type="devagent",
                created_at=now,
                updated_at=now,
                workspace=workspace or None,
                target_file=target_file or None,
            )
            s.add(th)
            s.commit()
        return True
    except Exception:
        return False


def repo_devagent_load_thread_meta(thread_id: str) -> dict:
    """Return thread metadata dict, or {}."""
    with get_devagent_session() as s:
        th = s.get(Thread, thread_id)
        return th.to_dict() if th else {}


def repo_devagent_save_thread_meta(thread_id: str, meta: dict) -> bool:
    """Persist updated thread metadata (title, assistant_id, assistant_name,
    workspace, target_file, updated_at)."""
    try:
        with get_devagent_session() as s:
            th = s.get(Thread, thread_id)
            if th is None:
                return False
            th.title = meta.get("title", th.title)
            th.assistant_id = meta.get("assistant_id", th.assistant_id)
            th.assistant_name = meta.get("assistant_name", th.assistant_name)
            if "workspace" in meta:
                th.workspace = meta.get("workspace") or None
            if "target_file" in meta:
                th.target_file = meta.get("target_file") or None
            th.updated_at = meta.get("updated_at", datetime.now().isoformat())
            s.commit()
        return True
    except Exception:
        return False


def repo_devagent_load_thread_messages(thread_id: str) -> list:
    """Return ordered list of message dicts for a thread."""
    with get_devagent_session() as s:
        msgs = (
            s.query(Message)
            .filter(Message.thread_id == thread_id)
            .order_by(Message.id)
            .all()
        )
        return [m.to_dict() for m in msgs]


def repo_devagent_save_thread_messages(thread_id: str, messages: list) -> bool:
    """Replace all messages for a thread with the given list."""
    try:
        with get_devagent_session() as s:
            s.query(Message).filter(Message.thread_id == thread_id).delete()
            for msg in messages:
                m = Message(
                    thread_id=thread_id,
                    role=msg.get("role", "user"),
                    content=msg.get("content", ""),
                    ts=msg.get("ts", datetime.now().isoformat()),
                    file_name=msg.get("file_name", ""),
                    file_chars=int(msg.get("file_chars", 0)),
                )
                s.add(m)
            s.commit()
        return True
    except Exception:
        return False


def repo_devagent_append_message(thread_id: str, role: str, content: str,
                                 file_name: str = "", file_chars: int = 0) -> bool:
    """Append a single message to a DevAgent thread."""
    try:
        with get_devagent_session() as s:
            now = datetime.now().isoformat()
            m = Message(
                thread_id=thread_id, role=role, content=content,
                ts=now,
                file_name=file_name, file_chars=file_chars,
            )
            s.add(m)
            # update thread's updated_at and title
            th = s.get(Thread, thread_id)
            if th:
                th.updated_at = now
                if not th.title and role == "user" and content.strip():
                    th.title = content.strip()[:60]
            s.commit()
        return True
    except Exception:
        return False


def repo_devagent_list_threads(slug: str = None) -> list:
    """Return DevAgent thread metadata dicts, sorted newest first.

    If ``slug`` is None, returns ALL devagent threads. Otherwise only threads
    for the given orchestrator slug (stored in assistant_id).
    """
    try:
        with get_devagent_session() as s:
            q = s.query(Thread).filter(Thread.type == "devagent")
            if slug:
                q = q.filter(Thread.assistant_id == slug)
            threads = q.order_by(Thread.updated_at.desc()).all()
            return [th.to_dict() for th in threads]
    except Exception:
        return []


def repo_devagent_delete_thread(thread_id: str) -> bool:
    """Delete a DevAgent thread (and its messages via cascade)."""
    try:
        with get_devagent_session() as s:
            th = s.get(Thread, thread_id)
            if th:
                s.delete(th)
                s.commit()
        return True
    except Exception:
        return False


def repo_devagent_delete_all_threads(slug: str = None) -> bool:
    """Delete DevAgent threads. If ``slug`` given, only that orchestrator's threads."""
    try:
        with get_devagent_session() as s:
            if slug:
                sub = s.query(Thread.thread_id).filter(Thread.type == "devagent", Thread.assistant_id == slug)
                s.query(Message).filter(
                    Message.thread_id.in_(sub)
                ).delete(synchronize_session=False)
                s.query(Thread).filter(Thread.type == "devagent", Thread.assistant_id == slug).delete()
            else:
                s.query(Message).filter(
                    Message.thread_id.in_(
                        s.query(Thread.thread_id).filter(Thread.type == "devagent")
                    )
                ).delete(synchronize_session=False)
                s.query(Thread).filter(Thread.type == "devagent").delete()
            s.commit()
        return True
    except Exception:
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
