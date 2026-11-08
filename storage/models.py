# -*- coding: utf-8 -*-
"""
storage.models - SQLAlchemy ORM models for SagaAI.
Tables: Assistant, Thread, Message, ConfigKV, Instruction, Orchestrator,
        OrchestratorInstruction.
"""
from sqlalchemy import Boolean, Column, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

# Default autonomous-loop step limit for orchestrators. Orchestrators created
# before the 500-step default keep their stored value; migration code in
# core/orchestrators and core/default_imports upgrades the legacy 100 default
# exactly once (guarded by the config marker ``max_steps_default_migrated``).
DEFAULT_MAX_STEPS = 500


class Assistant(Base):
    """Represents a named AI assistant (system prompt + metadata)."""
    __tablename__ = "assistants"

    id             = Column(String(8),   primary_key=True)
    slug           = Column(String(64),  unique=True, nullable=True, default=None)
    name           = Column(String(256), nullable=False, default="")
    service        = Column(String(128), nullable=False, default="")
    model          = Column(String(128), nullable=False, default="")
    temperature    = Column(Float,       nullable=False, default=0.7)
    description    = Column(Text,        nullable=False, default="")
    prompt_text    = Column(Text,        nullable=False, default="")
    tools          = Column(Text,        nullable=False, default="[]")
    max_tool_calls = Column(Integer,     nullable=True)
    max_tokens     = Column(Integer,     nullable=True)
    reasoning_effort = Column(String(32), nullable=True, default=None)
    # Localized names (see core.naming). `name` above is the user-defined
    # name - the only slot a locally created assistant fills; an assistant
    # installed from the marketplace fills the three translations instead.
    name_en        = Column(String(256), nullable=True, default=None)
    name_ru        = Column(String(256), nullable=True, default=None)
    name_zh        = Column(String(256), nullable=True, default=None)
    created_at     = Column(String(32),  nullable=False, default="")
    updated_at     = Column(String(32),  nullable=False, default="")

    threads = relationship("Thread", back_populates="assistant_rel",
                           foreign_keys="Thread.assistant_id",
                           primaryjoin="Assistant.id == Thread.assistant_id",
                           cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        import json
        try:
            tools_list = json.loads(self.tools) if self.tools else []
        except Exception:
            tools_list = []
        return {
            "id":             self.id,
            "slug":           self.slug,
            "name":           self.name,
            "service":        self.service,
            "model":          self.model,
            "temperature":    self.temperature,
            "description":    self.description,
            "tools":          tools_list,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens":     self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "name_en":        self.name_en or "",
            "name_ru":        self.name_ru or "",
            "name_zh":        self.name_zh or "",
            "created_at":     self.created_at,
            "updated_at":     self.updated_at,
        }


class Thread(Base):
    """Represents a dialogue thread (conversation)."""
    __tablename__ = "threads"

    thread_id      = Column(String(64),  primary_key=True)
    assistant_id   = Column(String(8),   ForeignKey("assistants.id", ondelete="SET NULL"),
                            nullable=True)
    assistant_name = Column(String(256), nullable=False, default="")
    title          = Column(String(256), nullable=False, default="")
    type           = Column(String(32),  nullable=False, default="chat")
    created_at     = Column(String(32),  nullable=False, default="")
    updated_at     = Column(String(32),  nullable=False, default="")
    workspace      = Column(String(1024), nullable=True, default=None)
    target_file    = Column(String(1024), nullable=True, default=None)

    # ── Transcript index ──────────────────────────────────────────────────
    # Message CONTENT lives in a per-thread append-only file outside SQLite
    # (see dev_agent/transcript.py); this row is the INDEX over it. Keeping
    # the row small is the whole point: listing 300 dialogs must not read
    # 300 MB of message text, and a growing transcript must not grow the
    # database. Every column here is nullable, and NULL carries a meaning:
    # it marks a LEGACY thread whose messages are still in ``messages`` and
    # were never moved to a file. The reader falls back to SQLite for those,
    # so an un-migrated database keeps working unchanged.
    parent_thread_id = Column(String(64), nullable=True, default=None)
    # 'dialog' for a user conversation, 'subagent' for a delegated child.
    # NULL means a legacy row, which can only be a dialog.
    kind           = Column(String(32),  nullable=True, default=None)
    message_count  = Column(Integer,     nullable=True, default=None)
    total_bytes    = Column(Integer,     nullable=True, default=None)
    last_message_at = Column(String(32), nullable=True, default=None)
    tokens_in      = Column(Integer,     nullable=True, default=None)
    tokens_out     = Column(Integer,     nullable=True, default=None)
    tokens_cache   = Column(Integer,     nullable=True, default=None)
    transcript_path = Column(String(1024), nullable=True, default=None)
    # 'jsonl' or 'jsonl.gz'. Stored explicitly so a reader never has to guess
    # a format from a file name, and a compressed file can be introduced for
    # old threads without touching the records themselves.
    transcript_format = Column(String(16), nullable=True, default=None)
    transcript_bytes = Column(Integer,    nullable=True, default=None)
    # Version of the per-line record format, so a future format change can be
    # read without a migration of every file at once.
    content_schema = Column(Integer,     nullable=True, default=None)

    assistant_rel = relationship("Assistant", back_populates="threads",
                                 foreign_keys=[assistant_id])
    messages      = relationship("Message", back_populates="thread",
                                 order_by="Message.id",
                                 cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "thread_id":     self.thread_id,
            "assistant_id":  self.assistant_id,
            "assistant_name": self.assistant_name,
            "title":         self.title,
            "type":          self.type,
            "created_at":    self.created_at,
            "updated_at":    self.updated_at,
            "workspace":     self.workspace,
            "target_file":   self.target_file,
            "parent_thread_id":  self.parent_thread_id,
            "kind":              self.kind,
            "message_count":     self.message_count,
            "total_bytes":       self.total_bytes,
            "last_message_at":   self.last_message_at,
            "tokens_in":         self.tokens_in,
            "tokens_out":        self.tokens_out,
            "tokens_cache":      self.tokens_cache,
            "transcript_path":   self.transcript_path,
            "transcript_format": self.transcript_format,
            "transcript_bytes":  self.transcript_bytes,
            "content_schema":    self.content_schema,
        }


class Message(Base):
    """A single message within a Thread."""
    __tablename__ = "messages"

    __table_args__ = (
        Index("ix_messages_thread_id_id", "thread_id", "id"),
    )

    id         = Column(Integer,      primary_key=True, autoincrement=True)
    thread_id  = Column(String(64),   ForeignKey("threads.thread_id", ondelete="CASCADE"),
                        nullable=False)
    role       = Column(String(32),   nullable=False, default="user")
    content    = Column(Text,         nullable=False, default="")
    ts         = Column(String(32),   nullable=False, default="")
    file_name  = Column(String(256),  nullable=False, default="")
    file_chars = Column(Integer,      nullable=False, default=0)

    thread = relationship("Thread", back_populates="messages")

    def to_dict(self) -> dict:
        return {
            "role":       self.role,
            "content":    self.content,
            "ts":         self.ts,
            "file_name":  self.file_name,
            "file_chars": self.file_chars,
        }


class ConfigKV(Base):
    """Key-value store for application configuration."""
    __tablename__ = "config_kv"

    key   = Column(String(256), primary_key=True)
    value = Column(Text,        nullable=False, default="")

    def to_dict(self) -> dict:
        """Return a dict with the real DB columns (key, value)."""
        return {
            "key":   self.key,
            "value": self.value,
        }


class Instruction(Base):
    """Represents an internal instruction (system prompt template) for DevAgent.

    Instructions are similar to assistants but are NOT exposed to users as
    selectable chat assistants. They are used internally by DevAgent for
    specific meta-tasks, such as Assistant Creator.
    """
    __tablename__ = "instructions"

    id          = Column(String(64),  primary_key=True)
    name        = Column(String(256), nullable=False, default="")
    description = Column(Text,        nullable=False, default="")
    prompt_text = Column(Text,        nullable=False, default="")
    created_at  = Column(String(32),  nullable=False, default="")
    updated_at  = Column(String(32),  nullable=False, default="")

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "name":        self.name,
            "description": self.description,
            "created_at":  self.created_at,
            "updated_at":  self.updated_at,
        }


class Orchestrator(Base):
    """Represents an autonomous orchestrator agent (e.g. DevAgent or a custom one).

    Each orchestrator is a self-contained unit: it has its own system prompt,
    its own model configuration (one main model + optional vision/image
    models + a search model), its own tool set and its own economy-mode
    settings. Orchestrators can be exported/imported as JSON so users can
    share them.

    DevAgent is a built-in orchestrator with slug='dev_agent' and is_builtin=True.
    """
    __tablename__ = "orchestrators"

    id          = Column(String(8),   primary_key=True)
    slug        = Column(String(64),  unique=True, nullable=False)
    name        = Column(String(256), nullable=False, default="")
    description = Column(Text,        nullable=False, default="")
    prompt_text = Column(Text,        nullable=False, default="")
    config_json = Column(Text,        nullable=False, default="{}")
    tools       = Column(Text,        nullable=False, default="[]")
    max_steps   = Column(Integer,     nullable=False, default=DEFAULT_MAX_STEPS)
    auto_apply  = Column(Boolean,     nullable=False, default=True)
    is_builtin  = Column(Boolean,     nullable=False, default=False)
    sort_order  = Column(Integer,     nullable=False, default=100)
    # Localized names (see core.naming); `name` is the user-defined slot.
    name_en     = Column(String(256), nullable=True, default=None)
    name_ru     = Column(String(256), nullable=True, default=None)
    name_zh     = Column(String(256), nullable=True, default=None)
    created_at  = Column(String(32),  nullable=False, default="")
    updated_at  = Column(String(32),  nullable=False, default="")

    def to_dict(self) -> dict:
        import json
        try:
            cfg = json.loads(self.config_json) if self.config_json else {}
        except Exception:
            cfg = {}
        try:
            tools_list = json.loads(self.tools) if self.tools else []
        except Exception:
            tools_list = []
        return {
            "id":          self.id,
            "slug":        self.slug,
            "name":        self.name,
            "description": self.description,
            "config":      cfg,
            "tools":       tools_list,
            "max_steps":   self.max_steps,
            "auto_apply":  self.auto_apply,
            "is_builtin":  self.is_builtin,
            "sort_order":  self.sort_order,
            "name_en":     self.name_en or "",
            "name_ru":     self.name_ru or "",
            "name_zh":     self.name_zh or "",
            "created_at":  self.created_at,
            "updated_at":  self.updated_at,
        }

    def to_export_dict(self) -> dict:
        """Return a full dict for export (includes prompt_text)."""
        d = self.to_dict()
        d["prompt_text"] = self.prompt_text
        return d


class OrchestratorInstruction(Base):
    """A cached copy of an orchestrator-specific instruction.

    The folder (DATA_DIR/orchestrators/<slug>/instructions/*.md) is the
    source of truth. This table is a runtime cache rebuilt at startup and
    refreshed whenever instructions change through the UI or DevAgent, so
    the hot path never reads from disk.
    """
    __tablename__ = "orchestrator_instructions"

    orchestrator_slug = Column(String(64), primary_key=True)
    id                = Column(String(64), primary_key=True)
    name              = Column(String(256), nullable=False, default="")
    description       = Column(Text,        nullable=False, default="")
    prompt_text       = Column(Text,        nullable=False, default="")
    created_at        = Column(String(32),  nullable=False, default="")
    updated_at        = Column(String(32),  nullable=False, default="")

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "name":        self.name,
            "description": self.description,
            "created_at":  self.created_at,
            "updated_at":  self.updated_at,
        }

    def to_full_dict(self) -> dict:
        d = self.to_dict()
        d["prompt_text"] = self.prompt_text
        return d
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
