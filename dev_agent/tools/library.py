"""Skills library and instructions lookup."""
from __future__ import annotations

import os
from typing import Any, Dict

from core.instructions import get_instruction, get_instruction_for, list_instructions, list_instructions_for

from .deps import ToolMixin


class LibraryTools(ToolMixin):
    """Skills library and instructions lookup."""

    def list_skills_library(self) -> Dict[str, Any]:
        """List all installed skills from the skills library.

        Returns metadata of every skill: id, name, description, folder.
        Use this first to discover a skill, then call get_skill_folder /
        get_skill_prompt to load its instructions.
        """
        try:
            from core.skills_library import list_skills as lib_list_skills
            skills = lib_list_skills()
            return {"ok": True, "count": len(skills), "skills": skills}
        except Exception as e:
            return {"ok": False, "error": f"Failed to list skills library: {e}"}

    def get_skill_folder(self, skill_id: str) -> Dict[str, Any]:
        """Return the absolute folder path and file list of an installed skill.

        Args:
            skill_id: 8-char skill id from list_skills_library.
        Returns:
            {"ok": True, "skill": {id, name, description, folder,
             folder_path, files: [relative paths]}}
        """
        try:
            from core.skills_library import (
                get_skill,
            )
            from core.skills_library import (
                get_skill_folder as lib_get_folder,
            )
            from core.skills_library import (
                list_skill_files as lib_list_files,
            )
        except Exception as e:
            return {"ok": False, "error": f"Skills library unavailable: {e}"}
        rec = get_skill(skill_id)
        if rec is None:
            return {"ok": False, "error": f"Skill not found in library: {skill_id}"}
        folder = lib_get_folder(skill_id)
        if not folder:
            return {"ok": False, "error": f"Skill folder missing on disk: {skill_id}"}
        files = lib_list_files(skill_id)
        return {
            "ok": True,
            "skill": {
                "id": rec.get("id", skill_id),
                "name": rec.get("name", ""),
                "description": rec.get("description", ""),
                "folder": rec.get("folder", ""),
                "folder_path": folder,
                "files": files,
            },
        }

    def get_skill_prompt(self, skill_id: str) -> Dict[str, Any]:
        """Load the instructions of an installed skill.

        Returns the combined content of SKILL.md / AGENT_SYSTEM_PROMPT.md when
        present (preferring SKILL.md), plus the skill folder path and file list.
        Use this to "invoke" a skill: put its instructions into your context
        before performing the task the skill describes.
        """
        try:
            from core.skills_library import (
                get_skill,
            )
            from core.skills_library import (
                get_skill_folder as lib_get_folder,
            )
            from core.skills_library import (
                list_skill_files as lib_list_files,
            )
        except Exception as e:
            return {"ok": False, "error": f"Skills library unavailable: {e}"}
        rec = get_skill(skill_id)
        if rec is None:
            return {"ok": False, "error": f"Skill not found in library: {skill_id}"}
        folder = lib_get_folder(skill_id)
        if not folder:
            return {"ok": False, "error": f"Skill folder missing on disk: {skill_id}"}
        files = lib_list_files(skill_id)
        text_parts = []
        for preferred in ("SKILL.md", "AGENT_SYSTEM_PROMPT.md", "skill.md"):
            if preferred in files:
                fpath = os.path.join(folder, preferred)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        text_parts.append(f.read())
                    break
                except Exception:
                    continue
        return {
            "ok": True,
            "skill": {
                "id": rec.get("id", skill_id),
                "name": rec.get("name", ""),
                "description": rec.get("description", ""),
                "folder": rec.get("folder", ""),
                "folder_path": folder,
                "files": files,
            },
            "prompt": "\n\n".join(text_parts),
        }

    def get_skill_file(self, skill_id: str, filename: str) -> Dict[str, Any]:
        """Return the content of one file inside an installed skill folder.

        Args:
            skill_id: 8-char skill id from list_skills_library.
            filename: relative file path inside the skill folder
            (e.g. "SKILL.md" or "agent_bundle/skill_test.py").
        Path traversal outside the skill folder is rejected.
        """
        try:
            from core.skills_library import (
                get_skill,
            )
            from core.skills_library import (
                get_skill_folder as lib_get_folder,
            )
        except Exception as e:
            return {"ok": False, "error": f"Skills library unavailable: {e}"}
        rec = get_skill(skill_id)
        if rec is None:
            return {"ok": False, "error": f"Skill not found in library: {skill_id}"}
        folder = lib_get_folder(skill_id)
        if not folder:
            return {"ok": False, "error": f"Skill folder missing on disk: {skill_id}"}
        folder_abs = os.path.abspath(folder)
        target = os.path.abspath(os.path.join(folder_abs, filename))
        if os.path.commonpath([folder_abs, target]) != folder_abs:
            return {"ok": False, "error": "Path escapes the skill folder."}
        if not os.path.isfile(target):
            return {"ok": False, "error": f"File not found in skill: {filename}"}
        try:
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return {"ok": False, "error": f"Failed to read skill file: {e}"}
        return {"ok": True, "skill_id": skill_id, "filename": filename, "content": content}

    def mark_skill_adapted(self, skill_id: str) -> Dict[str, Any]:
        """Mark an installed skill as adapted for the SagaAI platform.

        Used by DevAgent after it completes the Skill Developer adaptation
        of a third-party skill. Returns the current record on success.
        """
        try:
            from core.skills_library import get_skill, set_skill_adapted
        except Exception as e:
            return {"ok": False, "error": f"Skills library unavailable: {e}"}
        if not set_skill_adapted(skill_id, True):
            return {"ok": False, "error": f"Unable to mark skill adapted: {skill_id}"}
        rec = get_skill(skill_id)
        return {"ok": True, "skill": rec}

    def list_instructions(self) -> Dict[str, Any]:
        """Return all internal DevAgent instructions (id, name, description).

        Instructions are NOT user-facing assistants; they are system prompts
        used by DevAgent for meta-tasks (e.g. Assistant Creator). This returns
        a list without the full prompt_text -- use get_instruction for that.
        Connector-backed global instructions are listed only when this
        orchestrator has a matching service connection enabled.
        """
        try:
            instructions = list_instructions_for(self._orchestrator_slug)
        except Exception:
            instructions = list_instructions()
        return {"ok": True, "count": len(instructions), "instructions": instructions}

    def get_instruction(self, instruction_id: str) -> Dict[str, Any]:
        """Return a single instruction id, name, description, and full prompt_text.

        Connector-backed global instructions resolve only when this
        orchestrator has a matching service connection enabled.
        """
        try:
            instr = get_instruction_for(self._orchestrator_slug, instruction_id)
        except Exception:
            instr = get_instruction(instruction_id)
        if instr is None:
            return {"ok": False, "error": "Instruction not found: " + instruction_id}
        return {"ok": True, "instruction": instr}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
