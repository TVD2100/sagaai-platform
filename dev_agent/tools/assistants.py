"""Assistant profiles, detection and creation."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.assistants import update_assistant
from core.naming import merge_slots, slots_error

from ..assistant_detector import ASSISTANT_CREATOR_INSTRUCTION_ID
from .assistant_drafts import (
    assistant_create_kwargs,
    assistant_creation_schema,
    assistant_update_kwargs,
    build_creation_evaluation,
    parse_assistant_draft,
    strip_prompt_fence,
)
from .deps import ToolMixin


class AssistantTools(ToolMixin):
    """Assistant profiles, detection and creation."""

    def list_assistants(self) -> Dict[str, Any]:
        """Return all user assistants from the database as a list of dicts.

        Each dict contains: id, name, description, service, model, temperature,
        created_at, updated_at. Does NOT include the full prompt_text (use
        get_assistant_by_id for that).
        """
        assistants = self.deps().list_all_assistants_for_detection()
        return {"ok": True, "count": len(assistants), "assistants": assistants}

    def get_assistant_by_id(self, assistant_id: str) -> Dict[str, Any]:
        """Return full assistant info including prompt_text."""
        assistant = self.deps().get_assistant_by_id(assistant_id)
        if assistant is None:
            return {"ok": False, "error": f"Assistant not found: {assistant_id}"}
        return {"ok": True, "assistant": assistant}

    def update_assistant_by_id(self, assistant_id: str, name: Optional[str] = None,
                           description: Optional[str] = None,
                           prompt_text: Optional[str] = None,
                           service: Optional[str] = None,
                           model: Optional[str] = None,
                           temperature: Optional[float] = None,
                           tools: Optional[List[str]] = None,
                           max_tool_calls: Optional[int] = None,
                           max_tokens: Optional[int] = None,
                           reasoning_effort: Optional[str] = None,
                           name_en: Optional[str] = None,
                           name_ru: Optional[str] = None,
                           name_zh: Optional[str] = None) -> Dict[str, Any]:
        """Update an existing assistant by its id.

        Only the provided fields will be changed; omitted fields keep their
        current values. Use get_assistant_by_id first to inspect the current state.
        Before editing, ALWAYS load the Assistant Creator instruction and
        show its name/description to the user, get confirmation, then call
        this tool with the desired changes.

        Name handling: ``name`` is the user-defined slot and
        ``name_en``/``name_ru``/``name_zh`` are its translations. A translation
        left out of the call keeps its stored value; an explicitly passed empty
        string clears that slot. The saved record must keep at least one
        non-empty name, otherwise the interface would show a bare slug instead
        of a label - such a call is refused.
        """
        current = self.deps().get_assistant_by_id(assistant_id)
        if current is None:
            return {"ok": False, "error": f"Assistant not found: {assistant_id}"}

        slots = merge_slots(current, name, name_en, name_ru, name_zh)
        problem = slots_error(slots, "assistant")
        if problem:
            return {"ok": False, "error": problem}
        overrides = {
            "description": description, "prompt_text": prompt_text,
            "service": service, "model": model, "temperature": temperature,
            "tools": tools, "max_tool_calls": max_tool_calls,
            "max_tokens": max_tokens, "reasoning_effort": reasoning_effort,
        }
        ok = update_assistant(
            pid=assistant_id, **assistant_update_kwargs(current, overrides, slots)
        )
        if not ok:
            return {"ok": False, "error": "Failed to update assistant in database."}

        updated = self.deps().get_assistant_by_id(assistant_id)
        return {
            "ok": True,
            "assistant_id": assistant_id,
            "assistant_name": (updated or {}).get("name") or slots["name"],
            "updated": True,
            "assistant": updated,
        }

    def detect_and_select_assistant(self, task: str) -> Dict[str, Any]:
        """Analyse the user's task and find an appropriate self-contained assistant.

        1. Lists all existing assistants (name + description).
        2. Uses the LLM to find candidates matching the task.
        3. Evaluates each candidate's full prompt_text for relevance.
        4. If best score < 6, returns with empty prompt_text -- no auto-creation.
        5. Returns the chosen assistant's ID and prompt text (or empty if none).

        This tool REQUIRES that `set_send_request` was called with an LLM callable.

        Guard: this tool is only usable during the assistant_detection phase of the
        agent loop (controlled by `_assistant_detection_phase`). If called at any
        other time (e.g., mid-cycle by the LLM), it returns an error to prevent
        unnecessary duplicate assistant creation.
        """
        if not self._assistant_detection_phase:
            return {
                "ok": False,
                "error": (
                    "detect_and_select_assistant is only available during the initial "
                    "assistant-detection phase of the agent loop. It cannot be called "
                    "in the middle of a task by the LLM. The assistant is "
                    "already selected; use list_assistants or get_assistant_by_id to inspect assistants."
                ),
            }
        if self._send_request_fn is None:
            return {
                "ok": False,
                "error": "Assistant detection unavailable: no LLM backend configured. Configure the main model in DevAgent Settings.",
            }
        return self.deps().detect_and_select_assistant(task, self._send_request_fn)

    def _orchestrator_instruction_text(self, instruction_id: str) -> str:
        """Return the text of an orchestrator-specific instruction, or '' .

        Looks up the instruction in the folder of the orchestrator this
        executor currently serves. Falls back to the global instructions
        table when the lookup fails (older installations).
        """
        try:
            from core.orchestrators import orch_get_instruction
            inst = orch_get_instruction(self._orchestrator_slug, instruction_id)
            if inst:
                return str(inst.get("text", "") or "")
        except Exception:
            pass
        return self.deps().get_instruction_prompt(instruction_id)

    def _request_assistant_draft(self, task: str):
        """Ask the Assistant Creator instruction for a draft assistant profile.

        Returns ``(response_text, None)`` on success or ``(None, error_result)``
        when the backend or the instruction is missing or the call fails.
        """
        send_request_fn = self._send_request_fn
        if send_request_fn is None:
            return None, {"ok": False, "error": "Assistant creation unavailable: no LLM backend configured."}

        # Read Assistant Creator as an INSTRUCTION (not an assistant).
        # Prefer the global instruction; fall back to the orchestrator one.
        creator_prompt = self.deps().get_instruction_prompt(ASSISTANT_CREATOR_INSTRUCTION_ID)
        if not creator_prompt:
            creator_prompt = self._orchestrator_instruction_text(ASSISTANT_CREATOR_INSTRUCTION_ID)
        if not creator_prompt:
            return None, {"ok": False, "error": "Assistant Creator instruction not found. Re-run bootstrap or check the orchestrator's instructions."}

        try:
            response = self.deps().call_llm_with_system(
                send_request_fn,
                user_message=(
                    f"Generate a new assistant for the following request:\n\n{task}\n\n"
                    "Return ONLY a valid JSON object with 'name' (user-defined "
                    "name slot), the optional translations 'name_en', 'name_ru', "
                    "'name_zh', 'description' and 'prompt'."
                ),
                system=creator_prompt,
                history=[],
                json_schema=assistant_creation_schema(),
                json_schema_name="assistant_creation",
            )
        except Exception:
            return None, {"ok": False, "error": "LLM call to Assistant Creator failed."}
        return response, None

    def _finish_assistant_creation(self, task: str, send_request_fn, name: str,
                                   description: str, prompt_text: str,
                                   name_en: str = "", name_ru: str = "",
                                   name_zh: str = "") -> Dict[str, Any]:
        """Classify the task, resolve the profile, save the assistant and report."""
        classification = self.deps().classify_assistant_requirements(task, send_request_fn)
        complexity = classification["complexity"]
        needs_web_search = classification["needs_web_search"]

        (
            service,
            model,
            tools,
            log_msg,
            web_search_supported,
            reasoning_effort,
            max_tool_calls,
        ) = self.deps().resolve_service_model_for_assistant(task, complexity, needs_web_search)

        new_id = self.deps().create_assistant(**assistant_create_kwargs(
            name, description, prompt_text,
            (service, model, tools, reasoning_effort, max_tool_calls),
            {"name_en": name_en, "name_ru": name_ru, "name_zh": name_zh},
        ))
        if new_id is None:
            return {"ok": False, "error": "Failed to save new assistant to database."}

        new_assistant = self.deps().get_assistant_by_id(new_id)
        if new_assistant is None:
            return {"ok": False, "error": "Assistant created but could not be loaded back."}

        assistant_name = new_assistant.get("name", "Auto-generated")
        assistant_desc = new_assistant.get("description", "")
        evaluation = build_creation_evaluation(
            assistant_name=assistant_name,
            assistant_desc=assistant_desc,
            complexity_str=classification.get("complexity", "?"),
            service=service,
            model=model,
            tools=tools,
            log_msg=log_msg,
            needs_web_search=needs_web_search,
            web_search_supported=web_search_supported,
        )

        return {
            "ok": True,
            "assistant_id": new_assistant.get("id", ""),
            "assistant_name": assistant_name,
            "prompt_text": new_assistant.get("text", ""),
            "service": service,
            "model": model,
            "tools": tools,
            "web_search_supported": web_search_supported,
            "reasoning_effort": reasoning_effort,
            "created_new": True,
            "evaluation": evaluation,
        }

    def _create_assistant_with_auto_model(self, task: str) -> Dict[str, Any]:
        """Create a new assistant via the Assistant Creator instruction.

        The flow:
          1. Classify the task (complexity + web_search need).
          2. Read the Assistant Creator instruction prompt (NOT an assistant).
          3. Invoke LLM with the instruction to generate name/description/prompt.
          4. Resolve service+model+tools+reasoning_effort+max_tool_calls.
          5. Save the assistant with the resolved profile.
        """
        response, error = self._request_assistant_draft(task)
        if error is not None:
            return error

        parsed, parse_error = parse_assistant_draft(response.strip())
        if parsed is None:
            return {"ok": False, "error": parse_error}

        name = parsed.get("name", "").strip()
        description = parsed.get("description", "").strip()
        prompt_text = parsed.get("prompt", "").strip()
        translations = {
            slot: str(parsed.get(slot) or "").strip()
            for slot in ("name_en", "name_ru", "name_zh")
        }
        if not name and not any(translations.values()):
            return {"ok": False, "error": "Assistant Creator returned no name at all."}
        if not prompt_text:
            return {"ok": False, "error": "Assistant Creator returned an empty prompt."}
        if len(prompt_text) < 50:
            return {"ok": False, "error": "Generated prompt too short (< 50 chars)."}

        prompt_text = strip_prompt_fence(prompt_text)
        return self._finish_assistant_creation(
            task, self._send_request_fn, name, description, prompt_text,
            **translations)

    def create_assistant_for_task(self, task: str) -> Dict[str, Any]:
        """Explicitly create a new assistant for the given task.

        Uses the Assistant Creator instruction to generate name/description/
        prompt and saves a new assistant profile. The task is automatically
        classified for complexity and web_search need. Service, model, tools,
        reasoning_effort and max_tool_calls are resolved based on the
        classification:
          - No web_search: the main model from DevAgent settings.
          - Web_search: a web-search-capable provider (YandexAI preferred)
            with the web_search tool activated.

        The returned dict includes assistant_id, assistant_name, prompt_text,
        service, model, tools, web_search_supported, reasoning_effort and a
        human-readable evaluation message.

        This tool REQUIRES that `set_send_request` was called with an LLM callable.
        """
        if self._send_request_fn is None:
            return {
                "ok": False,
                "error": "Assistant creation unavailable: no LLM backend configured. Configure the main model in DevAgent Settings.",
            }
        return self._create_assistant_with_auto_model(task)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
