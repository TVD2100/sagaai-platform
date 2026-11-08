# -*- coding: utf-8 -*-
"""
dev_agent.orchestrator_tools - the orchestrator-management tool wrappers.

The universal dispatcher exposes the ``core.orchestrators`` API as tools: an
employee is created, renamed, reloaded and inspected through the same
dispatch surface as the file tools. These wrappers are thin adapters - they
reject the arguments a model may leave out and hand the call to the public
API.

Kept in their own module so the dispatcher stays a routing table: the
wrappers form one cohesive block, and a name rule (an employee must keep at
least one non-empty name slot) is enforced in exactly one place.
"""
from typing import Any, Dict, Optional

from core.naming import name_update_error, slots_error, split_name_slots
from storage.models import DEFAULT_MAX_STEPS


class OrchestratorToolsMixin:
    """The orchestrator-management tools of the universal dispatch surface."""

    def _list_orchestrators(self, **kwargs) -> Dict[str, Any]:
        from core.orchestrators import list_orchestrators
        orch_list = list_orchestrators()
        return {"ok": True, "count": len(orch_list), "orchestrators": orch_list}

    def _get_orchestrator(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import get_orchestrator
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        orch = get_orchestrator(slug)
        if orch is None:
            return {"ok": False, "error": f"Orchestrator not found: {slug}"}
        return {"ok": True, "orchestrator": orch}

    def _create_orchestrator(self, slug: str = "", name: str = "", description: str = "",
                             prompt_text: str = "", config: Optional[dict] = None,
                             tools: Optional[list] = None, max_steps: int = DEFAULT_MAX_STEPS,
                             auto_apply: bool = True, memory_dir: str = "",
                             name_en: str = "", name_ru: str = "", name_zh: str = "",
                             **kwargs) -> Dict[str, Any]:
        """Create an employee; a translated-only name is a valid name."""
        from core.orchestrator_folders import safe_orchestrator_slug
        from core.orchestrators import create_orchestrator

        effective_slug = safe_orchestrator_slug(slug)
        user_name, translations = split_name_slots(name, name_en, name_ru, name_zh)
        if not effective_slug or slots_error(dict(translations, name=user_name), "employee"):
            return {
                "ok": False,
                "error": (
                    "'slug' and at least one name are required: pass 'name' "
                    "(the user-defined slot) or one of 'name_en'/'name_ru'/'name_zh'."
                ),
            }
        # The working-data folder is stored inside config (the same key the
        # prompt assembly reads), so a bare top-level argument is never dropped.
        cfg = dict(config or {})
        folder = str(memory_dir or "").strip()
        if folder:
            cfg["memory_dir"] = folder
        orch_id = create_orchestrator(
            slug=slug.strip(),
            name=user_name,
            description=description or "",
            prompt_text=prompt_text or "",
            config=cfg,
            tools=tools or [],
            max_steps=int(max_steps or DEFAULT_MAX_STEPS),
            auto_apply=bool(auto_apply),
            **translations,
        )
        if orch_id is None:
            return {"ok": False, "error": f"Failed to create orchestrator (slug may already exist): {slug}"}
        return {
            "ok": True,
            "orchestrator_id": orch_id,
            "slug": effective_slug,
            "name": user_name,
            "names": dict(translations, name=user_name),
        }

    def _update_orchestrator(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        """Update an employee; a call that strips every name is refused."""
        from core.orchestrators import get_orchestrator, save_orchestrator

        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        allowed = {"name", "name_en", "name_ru", "name_zh", "description",
                   "prompt_text", "config", "tools", "max_steps", "auto_apply",
                   "sort_order", "memory_dir"}
        update_kwargs = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        name_slots = ("name", "name_en", "name_ru", "name_zh")
        if any(slot in update_kwargs for slot in name_slots):
            problem = name_update_error(get_orchestrator(slug) or {}, update_kwargs)
            if problem:
                return {"ok": False, "error": problem}
        ok = save_orchestrator(slug, **update_kwargs)
        if not ok:
            return {"ok": False, "error": f"Failed to update orchestrator: {slug}"}
        return {"ok": True, "slug": slug, "updated": True, "fields": list(update_kwargs.keys())}

    def _delete_orchestrator(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import delete_orchestrator
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        ok = delete_orchestrator(slug)
        if not ok:
            return {"ok": False, "error": f"Failed to delete orchestrator (may be built-in or not found): {slug}"}
        return {"ok": True, "slug": slug, "deleted": True}

    def _reload_orchestrator(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import reload_orchestrator_from_folder
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        res = reload_orchestrator_from_folder(slug)
        if not res.get("ok"):
            return {"ok": False, "error": f"Failed to reload orchestrator: {slug} ({res.get('error', 'unknown')})"}
        return {"ok": True, "slug": slug, "action": res.get("action"), "reloaded": True}

    def _list_orchestrator_functions(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_list_functions
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        functions = orch_list_functions(slug)
        return {"ok": True, "slug": slug, "count": len(functions), "functions": functions}

    def _get_orchestrator_function(self, slug: str = "", name: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_get_function
        if not slug or not name:
            return {"ok": False, "error": "Both 'slug' and 'name' are required."}
        fn = orch_get_function(slug, name)
        if fn is None:
            return {"ok": False, "error": f"Function not found: {slug}/{name}"}
        return {"ok": True, "function": fn}

    def _save_orchestrator_function(self, slug: str = "", name: str = "", code: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_save_function
        if not slug or not name or not code:
            return {"ok": False, "error": "Arguments 'slug', 'name' and 'code' are required."}
        ok = orch_save_function(slug, name, code)
        if not ok:
            return {"ok": False, "error": f"Failed to save function (name must be a valid Python identifier): {slug}/{name}"}
        return {"ok": True, "slug": slug, "name": name, "saved": True}

    def _delete_orchestrator_function(self, slug: str = "", name: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_delete_function
        if not slug or not name:
            return {"ok": False, "error": "Both 'slug' and 'name' are required."}
        ok = orch_delete_function(slug, name)
        if not ok:
            return {"ok": False, "error": f"Failed to delete function: {slug}/{name}"}
        return {"ok": True, "slug": slug, "name": name, "deleted": True}

    def _list_orchestrator_instructions(self, slug: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_list_instructions
        if not slug:
            return {"ok": False, "error": "Missing required argument 'slug'."}
        instructions = orch_list_instructions(slug)
        return {"ok": True, "slug": slug, "count": len(instructions), "instructions": instructions}

    def _get_orchestrator_instruction(self, slug: str = "", instruction_id: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_get_instruction
        if not slug or not instruction_id:
            return {"ok": False, "error": "Both 'slug' and 'instruction_id' are required."}
        inst = orch_get_instruction(slug, instruction_id)
        if inst is None:
            return {"ok": False, "error": f"Instruction not found: {slug}/{instruction_id}"}
        return {"ok": True, "instruction": inst}

    def _save_orchestrator_instruction(self, slug: str = "", instruction_id: str = "", name: str = "",
                                      description: str = "", prompt_text: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_save_instruction
        if not slug or not name:
            return {"ok": False, "error": "Arguments 'slug' and 'name' are required."}
        new_id = orch_save_instruction(
            slug, instruction_id=instruction_id, name=name,
            description=description or "", prompt_text=prompt_text or "",
        )
        if not new_id:
            return {"ok": False, "error": f"Failed to save instruction: {slug}"}
        return {"ok": True, "slug": slug, "instruction_id": new_id, "saved": True}

    def _delete_orchestrator_instruction(self, slug: str = "", instruction_id: str = "", **kwargs) -> Dict[str, Any]:
        from core.orchestrators import orch_delete_instruction
        if not slug or not instruction_id:
            return {"ok": False, "error": "Both 'slug' and 'instruction_id' are required."}
        ok = orch_delete_instruction(slug, instruction_id)
        if not ok:
            return {"ok": False, "error": f"Failed to delete instruction: {slug}/{instruction_id}"}
        return {"ok": True, "slug": slug, "instruction_id": instruction_id, "deleted": True}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
