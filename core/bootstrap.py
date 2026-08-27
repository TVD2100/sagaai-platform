"""
core.bootstrap - first-run provisioning for the SagaAI package.

Seeds built-in config values (such as the DevAgent system prompt) and
internal instructions (e.g. Assistant Creator, Employee Creator) so the
platform is ready to use without manual setup. Idempotent: running it
repeatedly refreshes the prompt text but never overwrites user-chosen
service/model/temperature.

Built-in instructions (Assistant Creator, Employee Creator, Self-Reflection)
are sourced from defaults/orchestrators/dev_agent/instructions/*.md - the
canonical single-source .md files (the same files used by
core.default_imports.ensure_default_instructions). They are stored as
ORCHESTRATOR-SPECIFIC instructions of the built-in DevAgent orchestrator
(DATA_DIR/orchestrators/dev_agent/instructions.json). The global
instructions table is intentionally left EMPTY for built-in rows - the global
instruction mechanism itself remains available for user-created instructions.

Assistant Creator and Employee Creator are instruction templates used by
DevAgent, not user-selectable chat assistants.

Employees are seeded via ``core.orchestrators.ensure_builtin_orchestrators()``.
"""
import os
from pathlib import Path

from core.config import (
    load_devagent_config,
    save_devagent_config,
    get_default_economy_tail_messages,
)
from core.instructions import get_instruction, delete_instruction


# Stable id for the built-in Assistant Creator instruction.
ASSISTANT_CREATOR_INSTRUCTION_ID = "assistant_creator"

# Legacy id used before the skill -> assistant terminology change.
SKILL_CREATOR_INSTRUCTION_ID = "skill_creator"

# Stable id for the built-in Employee Creator instruction.
EMPLOYEE_CREATOR_INSTRUCTION_ID = "employee_creator"

# Legacy id used by the old hardcoded bootstrap before the defaults/*.md era.
ORCHESTRATOR_CREATOR_INSTRUCTION_ID = "orchestrator_creator"
# Stable id for the built-in Self-Reflection instruction.
SELF_REFLECTION_INSTRUCTION_ID = "self_reflection"

# Built-in instruction ids that must never live in the global instructions table.
_BUILTIN_INSTRUCTION_IDS = (
    ASSISTANT_CREATOR_INSTRUCTION_ID,
    SKILL_CREATOR_INSTRUCTION_ID,
    EMPLOYEE_CREATOR_INSTRUCTION_ID,
    ORCHESTRATOR_CREATOR_INSTRUCTION_ID,
    SELF_REFLECTION_INSTRUCTION_ID,
)


def ensure_default_skills() -> dict:
    """Deprecated no-op, kept for backward compatibility."""
    return {}


def ensure_instructions() -> dict:
    """Seed the built-in instructions into the DevAgent orchestrator folder.

    Built-in instructions (Assistant Creator, Employee Creator, Self-Reflection)
    are read from the canonical defaults/orchestrators/dev_agent/instructions/*.md
    files (the same single-source files used by core.default_imports). They are
    stored as orchestrator-specific instructions of the built-in DevAgent
    orchestrator (DATA_DIR/orchestrators/dev_agent/instructions.json). The
    global instructions table is intentionally left EMPTY for built-in rows;
    the global-instruction mechanism remains available for user-created
    instructions.

    On existing databases, the legacy global rows are migrated into the DevAgent
    folder (preserving any user-edited text) and removed from the global table.
    Idempotent: once an instruction exists in the DevAgent folder it is kept
    as-is (user edits in the UI are preserved).

    When the defaults/ instruction files are missing, nothing is seeded - there
    are no hardcoded prompt copies in the codebase anymore.

    Returns:
        {"devagent_instructions": {id: "created"|"exists"|"error"},
         "global_cleaned": [ids removed from the global table]}
    """
    from core.orchestrators import (
        DEVAGENT_SLUG,
        orch_get_instruction,
        orch_save_instruction,
        orch_delete_instruction,
    )
    import core.defaults as defaults_mod

    instr_dir = os.path.join(defaults_mod.orchestrators_dir(), DEVAGENT_SLUG, "instructions")

    devagent_status = {}
    if os.path.isdir(instr_dir):
        for fname in sorted(os.listdir(instr_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(instr_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    raw = f.read()
            except OSError:
                continue
            default_id = fname[:-3]
            meta, body = defaults_mod.parse_front_matter(raw, default_id=default_id)
            iid = meta.get("id") or default_id
            name = meta.get("name") or iid
            description = meta.get("description", "")

            existing = orch_get_instruction(DEVAGENT_SLUG, iid)
            if iid == EMPLOYEE_CREATOR_INSTRUCTION_ID:
                # Migrate the legacy orchestrator_creator id: its saved text
                # (possibly user-edited) becomes the canonical employee_creator
                # instruction when the canonical one is missing; the legacy row
                # is always removed.
                legacy_orch = orch_get_instruction(
                    DEVAGENT_SLUG, ORCHESTRATOR_CREATOR_INSTRUCTION_ID
                )
                if legacy_orch:
                    if existing is None:
                        name = legacy_orch.get("name", "") or name
                        description = legacy_orch.get("description", "") or description
                        body = legacy_orch.get("text", "") or body
                    orch_delete_instruction(
                        DEVAGENT_SLUG, ORCHESTRATOR_CREATOR_INSTRUCTION_ID
                    )
            if existing:
                devagent_status[iid] = "exists"
                continue

            # Prefer the text of the legacy global row if present (user-edited).
            legacy = get_instruction(iid)
            if legacy is None and iid == ASSISTANT_CREATOR_INSTRUCTION_ID:
                legacy = get_instruction(SKILL_CREATOR_INSTRUCTION_ID)
            legacy = legacy or {}
            text = legacy.get("text", "") or body
            used_name = legacy.get("name", "") or name
            used_desc = legacy.get("description", "") or description
            ok = orch_save_instruction(
                DEVAGENT_SLUG, iid,
                name=used_name,
                description=used_desc,
                prompt_text=text,
            )
            devagent_status[iid] = "created" if ok else "error"

    # Keep the global instructions table free of built-in rows.
    removed = []
    for iid in _BUILTIN_INSTRUCTION_IDS:
        if get_instruction(iid):
            if delete_instruction(iid):
                removed.append(iid)

    return {"devagent_instructions": devagent_status, "global_cleaned": removed}


def ensure_devagent_settings() -> dict:
    """Seed DevAgent settings on first run.

    Delegates to ``core.orchestrators.ensure_builtin_orchestrators()``,
    which migrates the old KV-based settings into the orchestrators table.
    Idempotent: subsequent runs do not overwrite user-chosen settings.
    """
    try:
        from core.orchestrators import ensure_builtin_orchestrators
        return ensure_builtin_orchestrators()
    except Exception:
        # Fallback during very early bootstrap (e.g. in tests with no
        # orchestrator table yet).
        cfg = load_devagent_config()
        if not cfg.get("prompt_text", "").strip():
            prompt_file = (
                Path(__file__).resolve().parent.parent
                / "dev_agent" / "system_prompt.md"
            )
            try:
                prompt = prompt_file.read_text(encoding="utf-8")
            except Exception:
                prompt = "You are DevAgent, the embedded developer of SagaAI."
            save_devagent_config(
                service=cfg.get("service", "DeepSeek"),
                model=cfg.get("model", "deepseek-v4-pro"),
                temperature=float(cfg.get("temperature", 0.2)),
                prompt_text=prompt,
                strong_service=cfg.get("strong_service", "DeepSeek"),
                strong_model=cfg.get("strong_model", "deepseek-v4-pro"),
                strong_temperature=float(cfg.get("strong_temperature", 0.4)),
                weak_service=cfg.get("weak_service", "DeepSeek"),
                weak_model=cfg.get("weak_model", "deepseek-v4-pro"),
                weak_temperature=float(cfg.get("weak_temperature", 0.4)),
                search_service=cfg.get("search_service", "YandexAI"),
                search_model=cfg.get("search_model", "aliceai-llm-flash"),
                search_temperature=float(cfg.get("search_temperature", 0.3)),
                search_max_tool_calls=int(cfg.get("search_max_tool_calls", 1)),
                economy_tail_messages=int(cfg.get("economy_tail_messages", get_default_economy_tail_messages())),
            )
            return {"devagent_prompt": "seeded"}
        return {"devagent_prompt": "ok"}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
