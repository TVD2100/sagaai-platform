"""
dev_agent.assistant_detector - assistant creation logic.

No longer implements two-phase assistant detection (removed from agent_loop).
Now only provides:
- detect_and_select_assistant(): kept for backward compatibility (always returns empty).
- list_all_assistants_for_detection(): lists user assistants.

Note: create_assistant_for_task() has been moved to tool_executor.py and now uses
assistant_model_resolver for automatic strong/weak + web_search classification.
Assistant Creator is now an INSTRUCTION (not an assistant), identified by
ASSISTANT_CREATOR_INSTRUCTION_ID.

Explicit assistant creation:
   When the user explicitly asks to create an assistant (e.g. "Create a spelling check assistant"),
   DevAgent calls create_assistant_for_task() which invokes the Assistant Creator instruction.
   The Assistant Creator returns JSON with "name", "description", "prompt".
   The created assistant is automatically assigned a working service+model:
   - The system classifies the task (strong/weak complexity, needs_web_search).
   - If web_search is NOT needed: model is picked from DevAgent settings
     (strong_service/strong_model or weak_service/weak_model).
   - If web_search IS needed: model is picked from YandexAI service
     (yandexgpt-5-pro / yandexgpt-5.1 for strong, yandexgpt-lite / yandexgpt-5-lite for weak),
     and the web_search tool is activated for the assistant.
   - If the user explicitly specifies a service or model in the request, that is used.
"""

import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.assistants import get_assistant_by_id, create_assistant
from storage.repository import repo_load_assistants

# The stable ID of the built-in Assistant Creator instruction (seeded in bootstrap.py).
# Now an INSTRUCTION, not an assistant.
ASSISTANT_CREATOR_INSTRUCTION_ID = "assistant_creator"

# Legacy aliases for backward compatibility with code that still references the
# old "skill" terminology.
SKILL_CREATOR_INSTRUCTION_ID = ASSISTANT_CREATOR_INSTRUCTION_ID
SKILL_CREATOR_ID = ASSISTANT_CREATOR_INSTRUCTION_ID


# --- Public API ------------------------------------------------------------------


def list_all_assistants_for_detection() -> List[Dict[str, Any]]:
    """Return all user assistants excluding the built-in Assistant Creator (if it still
    exists as an assistant for backward compatibility)."""
    assistants = repo_load_assistants()
    return [a for a in assistants if a.get("id") not in (ASSISTANT_CREATOR_INSTRUCTION_ID, "skill_creator", "builtin_skill_creator")]


# Legacy alias (old "skill" terminology).
list_all_skills_for_detection = list_all_assistants_for_detection


def detect_and_select_assistant(
    task: str,
    send_request_fn: Callable,
) -> Dict[str, Any]:
    """Kept for backward compatibility. No longer performs assistant detection.
    Always returns ok=True with empty prompt_text.
    """
    return {
        "ok": True,
        "assistant_id": "",
        "assistant_name": "",
        "prompt_text": "",
        "created_new": False,
        "evaluation": "Assistant detection has been removed. Assistants are created only on explicit user request.",
    }


# Legacy alias (old "skill" terminology).
detect_and_select_skill = detect_and_select_assistant
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
