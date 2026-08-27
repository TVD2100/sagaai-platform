"""
storage.repository - high-level CRUD functions used by core.assistants, core.threads, core.config, core.instructions.
All public functions accept/return plain dicts or primitives (no ORM objects leak out).
"""
import json
from datetime import datetime
from storage.db import get_session
from storage.models import Assistant, Thread, Message, ConfigKV, Instruction, Orchestrator, OrchestratorInstruction


# ─── Assistants ───────────────────────────────────────────────────────────────

def repo_load_assistants() -> list:
    """Return all assistants as a list of dicts (without prompt_text)."""
    with get_session() as s:
        return [a.to_dict() for a in s.query(Assistant).all()]


def repo_get_assistant(assistant_id: str) -> dict | None:
    """Return a single assistant dict (without prompt_text), or None."""
    with get_session() as s:
        a = s.get(Assistant, assistant_id)
        return a.to_dict() if a else None


def repo_get_assistant_by_slug(slug: str) -> dict | None:
    """Return a single assistant dict by its slug (without prompt_text), or None."""
    if not slug:
        return None
    with get_session() as s:
        a = s.query(Assistant).filter(Assistant.slug == slug).first()
        return a.to_dict() if a else None


def repo_get_assistant_with_text(assistant_id: str) -> dict | None:
    """Return a single assistant dict INCLUDING prompt_text and tools, or None."""
    with get_session() as s:
        a = s.get(Assistant, assistant_id)
        if a is None:
            return None
        d = a.to_dict()
        d["text"] = a.prompt_text
        return d


def repo_create_assistant(assistant_id: str, name: str, service: str, model: str,
                          temperature: float, prompt_text: str,
                          description: str = "", tools: list = None,
                          max_tool_calls: int = None, max_tokens: int = None,
                          reasoning_effort: str = None,
                          slug: str = None) -> bool:
    """Insert a new Assistant row. Returns True on success."""
    if tools is None:
        tools = []
    try:
        with get_session() as s:
            now = datetime.now().isoformat()
            a = Assistant(
                id=assistant_id, slug=slug, name=name, service=service, model=model,
                temperature=float(temperature), description=description,
                prompt_text=prompt_text, tools=json.dumps(tools, ensure_ascii=False),
                max_tool_calls=max_tool_calls, max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                created_at=now, updated_at=now,
            )
            s.add(a)
            s.commit()
        return True
    except Exception:
        return False


def repo_update_assistant(assistant_id: str, name: str, service: str, model: str,
                          temperature: float, prompt_text: str,
                          description: str = "", tools: list = None,
                          max_tool_calls: int = None, max_tokens: int = None,
                          reasoning_effort: str = None,
                          slug: str = None) -> bool:
    """Update an existing Assistant. Returns True on success."""
    if tools is None:
        tools = []
    try:
        with get_session() as s:
            a = s.get(Assistant, assistant_id)
            if a is None:
                return False
            a.name           = name
            a.service        = service
            a.model          = model
            a.temperature    = float(temperature)
            a.description    = description
            a.prompt_text    = prompt_text
            a.tools          = json.dumps(tools, ensure_ascii=False)
            a.max_tool_calls = max_tool_calls
            a.max_tokens     = max_tokens
            a.reasoning_effort = reasoning_effort
            if slug:
                a.slug = slug
            a.updated_at     = datetime.now().isoformat()
            s.commit()
        return True
    except Exception:
        return False


def repo_set_assistant_slug(assistant_id: str, slug: str) -> bool:
    """Update only the slug column of an assistant. Returns True on success."""
    try:
        with get_session() as s:
            a = s.get(Assistant, assistant_id)
            if a is None:
                return False
            a.slug       = slug
            a.updated_at = datetime.now().isoformat()
            s.commit()
        return True
    except Exception:
        return False


def repo_delete_assistant(assistant_id: str) -> bool:
    """Delete an assistant by id. Returns True on success."""
    try:
        with get_session() as s:
            a = s.get(Assistant, assistant_id)
            if a:
                s.delete(a)
                s.commit()
        return True
    except Exception:
        return False


def repo_load_assistant_prompt_text(assistant_id: str) -> str:
    """Return the prompt_text column for an assistant."""
    with get_session() as s:
        a = s.get(Assistant, assistant_id)
        return a.prompt_text if a else ""


def repo_save_assistant_prompt_text(assistant_id: str, text: str) -> bool:
    """Update the prompt_text column for an assistant."""
    try:
        with get_session() as s:
            a = s.get(Assistant, assistant_id)
            if a is None:
                return False
            a.prompt_text = text
            a.updated_at  = datetime.now().isoformat()
            s.commit()
        return True
    except Exception:
        return False


# ─── Instructions ─────────────────────────────────────────────────────────────

def repo_list_instructions() -> list:
    """Return all instructions as a list of dicts (without prompt_text)."""
    with get_session() as s:
        return [inst.to_dict() for inst in s.query(Instruction).all()]


def repo_get_instruction(instruction_id: str) -> dict | None:
    """Return a single instruction dict (without prompt_text), or None."""
    with get_session() as s:
        inst = s.get(Instruction, instruction_id)
        return inst.to_dict() if inst else None


def repo_get_instruction_with_text(instruction_id: str) -> dict | None:
    """Return a single instruction dict INCLUDING prompt_text, or None."""
    with get_session() as s:
        inst = s.get(Instruction, instruction_id)
        if inst is None:
            return None
        d = inst.to_dict()
        d["text"] = inst.prompt_text
        return d


def repo_create_instruction(instruction_id: str, name: str,
                             description: str, prompt_text: str) -> bool:
    """Insert a new Instruction row. Returns True on success."""
    try:
        with get_session() as s:
            now = datetime.now().isoformat()
            inst = Instruction(
                id=instruction_id, name=name, description=description,
                prompt_text=prompt_text, created_at=now, updated_at=now,
            )
            s.add(inst)
            s.commit()
        return True
    except Exception:
        return False


def repo_update_instruction(instruction_id: str, name: str,
                             description: str, prompt_text: str) -> bool:
    """Update an existing Instruction. Returns True on success."""
    try:
        with get_session() as s:
            inst = s.get(Instruction, instruction_id)
            if inst is None:
                return False
            inst.name        = name
            inst.description = description
            inst.prompt_text = prompt_text
            inst.updated_at  = datetime.now().isoformat()
            s.commit()
        return True
    except Exception:
        return False


def repo_delete_instruction(instruction_id: str) -> bool:
    """Delete an instruction by id. Returns True on success."""
    try:
        with get_session() as s:
            inst = s.get(Instruction, instruction_id)
            if inst:
                s.delete(inst)
                s.commit()
        return True
    except Exception:
        return False


def repo_get_instruction_prompt_text(instruction_id: str) -> str:
    """Return the prompt_text column for an instruction."""
    with get_session() as s:
        inst = s.get(Instruction, instruction_id)
        return inst.prompt_text if inst else ""


def repo_save_instruction_prompt_text(instruction_id: str, text: str) -> bool:
    """Update the prompt_text column for an instruction."""
    try:
        with get_session() as s:
            inst = s.get(Instruction, instruction_id)
            if inst is None:
                return False
            inst.prompt_text = text
            inst.updated_at  = datetime.now().isoformat()
            s.commit()
        return True
    except Exception:
        return False


# ─── Orchestrator instructions (runtime cache) ───────────────────────────────

def repo_list_orchestrator_instructions(slug: str) -> list:
    """Return cached metadata for all instructions of an orchestrator."""
    with get_session() as s:
        rows = (
            s.query(OrchestratorInstruction)
            .filter(OrchestratorInstruction.orchestrator_slug == slug)
            .all()
        )
        return [r.to_dict() for r in rows]


def repo_get_orchestrator_instruction(slug: str, instruction_id: str) -> dict | None:
    """Return a full cached orchestrator instruction (with prompt_text), or None."""
    with get_session() as s:
        row = (
            s.query(OrchestratorInstruction)
            .filter(
                OrchestratorInstruction.orchestrator_slug == slug,
                OrchestratorInstruction.id == instruction_id,
            )
            .first()
        )
        return row.to_full_dict() if row else None


def repo_save_orchestrator_instruction(slug: str, instruction_id: str,
                                       name: str, description: str,
                                       prompt_text: str) -> bool:
    """Insert or update a cached orchestrator instruction. Returns True on success."""
    try:
        now = datetime.now().isoformat()
        with get_session() as s:
            row = (
                s.query(OrchestratorInstruction)
                .filter(
                    OrchestratorInstruction.orchestrator_slug == slug,
                    OrchestratorInstruction.id == instruction_id,
                )
                .first()
            )
            if row is None:
                row = OrchestratorInstruction(
                    orchestrator_slug=slug, id=instruction_id,
                    name=name, description=description, prompt_text=prompt_text,
                    created_at=now, updated_at=now,
                )
                s.add(row)
            else:
                row.name        = name
                row.description = description
                row.prompt_text = prompt_text
                row.updated_at  = now
            s.commit()
        return True
    except Exception:
        return False


def repo_delete_orchestrator_instruction(slug: str, instruction_id: str) -> bool:
    """Delete a cached orchestrator instruction. Returns True on success."""
    try:
        with get_session() as s:
            row = (
                s.query(OrchestratorInstruction)
                .filter(
                    OrchestratorInstruction.orchestrator_slug == slug,
                    OrchestratorInstruction.id == instruction_id,
                )
                .first()
            )
            if row:
                s.delete(row)
                s.commit()
        return True
    except Exception:
        return False


def repo_delete_all_orchestrator_instructions(slug: str) -> bool:
    """Delete all cached instructions of an orchestrator. Returns True on success."""
    try:
        with get_session() as s:
            s.query(OrchestratorInstruction).filter(
                OrchestratorInstruction.orchestrator_slug == slug
            ).delete()
            s.commit()
        return True
    except Exception:
        return False


# ─── Threads ──────────────────────────────────────────────────────────────────

def repo_create_thread(thread_id: str, assistant_id: str, assistant_name: str,
                       thread_type: str = "chat") -> bool:
    """Insert a new Thread row. Returns True on success."""
    try:
        with get_session() as s:
            now = datetime.now().isoformat()
            th = Thread(
                thread_id=thread_id, assistant_id=assistant_id or None,
                assistant_name=assistant_name, title="",
                type=thread_type,
                created_at=now, updated_at=now,
            )
            s.add(th)
            s.commit()
        return True
    except Exception:
        return False


def repo_load_thread_meta(thread_id: str) -> dict:
    """Return thread metadata dict, or {}."""
    with get_session() as s:
        th = s.get(Thread, thread_id)
        return th.to_dict() if th else {}


def repo_save_thread_meta(thread_id: str, meta: dict) -> bool:
    """Persist updated thread metadata (title, updated_at, etc.)."""
    try:
        with get_session() as s:
            th = s.get(Thread, thread_id)
            if th is None:
                return False
            th.title          = meta.get("title",          th.title)
            th.assistant_id   = meta.get("assistant_id",   th.assistant_id)
            th.assistant_name = meta.get("assistant_name", th.assistant_name)
            th.type           = meta.get("type",            th.type)
            th.updated_at     = meta.get("updated_at",      datetime.now().isoformat())
            s.commit()
        return True
    except Exception:
        return False


def repo_load_thread_messages(thread_id: str) -> list:
    """Return ordered list of message dicts for a thread."""
    with get_session() as s:
        msgs = (
            s.query(Message)
            .filter(Message.thread_id == thread_id)
            .order_by(Message.id)
            .all()
        )
        return [m.to_dict() for m in msgs]


def repo_save_thread_messages(thread_id: str, messages: list) -> bool:
    """Replace all messages for a thread with the given list."""
    try:
        with get_session() as s:
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


def repo_append_message(thread_id: str, role: str, content: str,
                        file_name: str = "", file_chars: int = 0) -> bool:
    """Append a single message to a thread."""
    try:
        with get_session() as s:
            m = Message(
                thread_id=thread_id, role=role, content=content,
                ts=datetime.now().isoformat(),
                file_name=file_name, file_chars=file_chars,
            )
            s.add(m)
            # update thread's updated_at and title
            th = s.get(Thread, thread_id)
            if th:
                th.updated_at = m.ts
                if not th.title and role == "user" and content.strip():
                    th.title = content.strip()[:60]
            s.commit()
        return True
    except Exception:
        return False


def repo_list_all_threads() -> list:
    """Return all thread metadata dicts, sorted newest first."""
    with get_session() as s:
        threads = s.query(Thread).order_by(Thread.updated_at.desc()).all()
        return [th.to_dict() for th in threads]


def repo_list_threads_by_type(thread_type: str) -> list:
    """Return thread metadata dicts of a given type, sorted newest first."""
    with get_session() as s:
        threads = (
            s.query(Thread)
            .filter(Thread.type == thread_type)
            .order_by(Thread.updated_at.desc())
            .all()
        )
        return [th.to_dict() for th in threads]


def repo_list_chat_threads() -> list:
    """Return chat (assistant-based) thread metadata dicts, sorted newest first.

    Excludes DevAgent threads (type='devagent'). Used by the History page to
    show only assistant-based conversations.
    """
    return repo_list_threads_by_type("chat")


def repo_delete_thread(thread_id: str) -> bool:
    """Delete a thread (and its messages via cascade)."""
    try:
        with get_session() as s:
            th = s.get(Thread, thread_id)
            if th:
                s.delete(th)
                s.commit()
        return True
    except Exception:
        return False


def repo_delete_all_threads() -> bool:
    """Delete every thread and its messages."""
    try:
        with get_session() as s:
            s.query(Message).delete()
            s.query(Thread).delete()
            s.commit()
        return True
    except Exception:
        return False


# ─── Config ───────────────────────────────────────────────────────────────────

def repo_load_config() -> dict:
    """Load all config key-value pairs as a plain dict."""
    with get_session() as s:
        rows = s.query(ConfigKV).all()
        result = {}
        for row in rows:
            try:
                result[row.key] = json.loads(row.value)
            except Exception:
                result[row.key] = row.value
        return result


def repo_save_config(config: dict) -> bool:
    """Overwrite all config key-value pairs."""
    try:
        with get_session() as s:
            s.query(ConfigKV).delete()
            for key, val in config.items():
                kv = ConfigKV(key=key, value=json.dumps(val, ensure_ascii=False))
                s.add(kv)
            s.commit()
        return True
    except Exception:
        return False


# ─── Orchestrators ────────────────────────────────────────────────────────────

def repo_list_orchestrators() -> list:
    """Return all orchestrators as a list of dicts (without prompt_text),
    sorted by sort_order."""
    with get_session() as s:
        orch_list = s.query(Orchestrator).order_by(
            Orchestrator.sort_order, Orchestrator.created_at
        ).all()
        return [orch.to_dict() for orch in orch_list]


def repo_get_orchestrator_by_slug(slug: str) -> dict | None:
    """Return a single orchestrator dict (without prompt_text), or None."""
    with get_session() as s:
        orch = s.query(Orchestrator).filter(Orchestrator.slug == slug).first()
        return orch.to_dict() if orch else None


def repo_get_orchestrator_by_id(orchestrator_id: str) -> dict | None:
    """Return a single orchestrator dict (without prompt_text), or None."""
    with get_session() as s:
        orch = s.get(Orchestrator, orchestrator_id)
        return orch.to_dict() if orch else None


def repo_get_orchestrator_with_text(slug_or_id: str) -> dict | None:
    """Return a single orchestrator dict INCLUDING prompt_text, or None.

    Looks up by slug first, then by id.
    """
    with get_session() as s:
        orch = s.query(Orchestrator).filter(Orchestrator.slug == slug_or_id).first()
        if orch is None:
            orch = s.get(Orchestrator, slug_or_id)
        if orch is None:
            return None
        d = orch.to_dict()
        d["prompt_text"] = orch.prompt_text
        return d


def repo_create_orchestrator(orchestrator_id: str, slug: str, name: str,
                              description: str, prompt_text: str,
                              config: dict = None, tools: list = None,
                              max_steps: int = 100, auto_apply: bool = True,
                              is_builtin: bool = False, sort_order: int = 100) -> bool:
    """Insert a new Orchestrator row. Returns True on success."""
    if config is None:
        config = {}
    if tools is None:
        tools = []
    try:
        with get_session() as s:
            now = datetime.now().isoformat()
            orch = Orchestrator(
                id=orchestrator_id, slug=slug, name=name,
                description=description,
                prompt_text=prompt_text,
                config_json=json.dumps(config, ensure_ascii=False),
                tools=json.dumps(tools, ensure_ascii=False),
                max_steps=max_steps, auto_apply=auto_apply,
                is_builtin=is_builtin, sort_order=sort_order,
                created_at=now, updated_at=now,
            )
            s.add(orch)
            s.commit()
        return True
    except Exception:
        return False


def repo_update_orchestrator(orchestrator_id: str, **kwargs) -> bool:
    """Update an existing Orchestrator. Only provided kwargs are changed.

    Supported kwargs: slug, name, description, prompt_text, config (dict),
    tools (list), max_steps, auto_apply, is_builtin, sort_order.
    Returns True on success.
    """
    try:
        with get_session() as s:
            orch = s.get(Orchestrator, orchestrator_id)
            if orch is None:
                return False
            if "slug" in kwargs:
                orch.slug = kwargs["slug"]
            if "name" in kwargs:
                orch.name = kwargs["name"]
            if "description" in kwargs:
                orch.description = kwargs["description"]
            if "prompt_text" in kwargs:
                orch.prompt_text = kwargs["prompt_text"]
            if "config" in kwargs:
                orch.config_json = json.dumps(kwargs["config"], ensure_ascii=False)
            if "tools" in kwargs:
                orch.tools = json.dumps(kwargs["tools"], ensure_ascii=False)
            if "max_steps" in kwargs:
                orch.max_steps = kwargs["max_steps"]
            if "auto_apply" in kwargs:
                orch.auto_apply = kwargs["auto_apply"]
            if "is_builtin" in kwargs:
                orch.is_builtin = kwargs["is_builtin"]
            if "sort_order" in kwargs:
                orch.sort_order = kwargs["sort_order"]
            orch.updated_at = datetime.now().isoformat()
            s.commit()
        return True
    except Exception:
        return False


def repo_delete_orchestrator(orchestrator_id: str) -> bool:
    """Delete an orchestrator by id. Returns True on success."""
    try:
        with get_session() as s:
            orch = s.get(Orchestrator, orchestrator_id)
            if orch:
                s.delete(orch)
                s.commit()
        return True
    except Exception:
        return False


# ─── Legacy aliases (old "skill" terminology) ─────────────────────────────────
# Kept so older callers (and third-party code) keep working after the rename.
# New code must use the repo_*_assistant names above.

repo_load_skills = repo_load_assistants
repo_get_skill = repo_get_assistant
repo_get_skill_with_text = repo_get_assistant_with_text
repo_delete_skill = repo_delete_assistant
repo_load_prompt_text = repo_load_assistant_prompt_text
repo_save_prompt_text = repo_save_assistant_prompt_text


def repo_create_skill(skill_id: str, name: str, service: str, model: str,
                      temperature: float, prompt_text: str,
                      description: str = "", tools: list = None,
                      max_tool_calls: int = None, max_tokens: int = None,
                      reasoning_effort: str = None) -> bool:
    """Legacy wrapper (old "skill" terminology) around repo_create_assistant."""
    return repo_create_assistant(
        assistant_id=skill_id, name=name, service=service, model=model,
        temperature=temperature, prompt_text=prompt_text,
        description=description, tools=tools,
        max_tool_calls=max_tool_calls, max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )


def repo_update_skill(skill_id: str, name: str, service: str, model: str,
                      temperature: float, prompt_text: str,
                      description: str = "", tools: list = None,
                      max_tool_calls: int = None, max_tokens: int = None,
                      reasoning_effort: str = None) -> bool:
    """Legacy wrapper (old "skill" terminology) around repo_update_assistant."""
    return repo_update_assistant(
        assistant_id=skill_id, name=name, service=service, model=model,
        temperature=temperature, prompt_text=prompt_text,
        description=description, tools=tools,
        max_tool_calls=max_tool_calls, max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
