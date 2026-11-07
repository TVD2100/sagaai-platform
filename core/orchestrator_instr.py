# -*- coding: utf-8 -*-
"""
core.orchestrator_instr - instructions and custom functions stored in the orchestrator folder.

Part of ``core.orchestrators``, which is the public surface of this
package. A name the tests may replace through the facade is resolved late,
at call time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
from typing import Optional

from core.orchestrator_folders import (
    delete_orchestrator_function,
    delete_orchestrator_instruction,
    get_orchestrator_function,
    get_orchestrator_instruction,
    list_orchestrator_functions,
    list_orchestrator_instructions,
    save_orchestrator_function,
    save_orchestrator_instruction,
)


def orch_list_instructions(slug: str) -> list:
    """Return metadata for orchestrator-specific instructions."""
    return list_orchestrator_instructions(slug)


def orch_get_instruction_impl(slug: str, instruction_id: str) -> Optional[dict]:
    """Return a full orchestrator-specific instruction, or None."""
    return get_orchestrator_instruction(slug, instruction_id)


def orch_save_instruction(slug: str, instruction_id: str, name: str,
                          description: str = "", prompt_text: str = "") -> str:
    """Create or update an orchestrator-specific instruction.

    Returns the effective instruction id on success, '' on failure.
    """
    return save_orchestrator_instruction(
        slug, instruction_id, name, description, prompt_text
    )


def orch_delete_instruction(slug: str, instruction_id: str) -> bool:
    """Delete an orchestrator-specific instruction."""
    return delete_orchestrator_instruction(slug, instruction_id)


def orch_list_functions(slug: str) -> list:
    """Return metadata for custom functions of an orchestrator."""
    return list_orchestrator_functions(slug)


def orch_get_function(slug: str, name: str) -> Optional[dict]:
    """Return a full custom function dict including source code."""
    return get_orchestrator_function(slug, name)


def orch_save_function(slug: str, name: str, code: str) -> bool:
    """Create or update a custom function."""
    return save_orchestrator_function(slug, name, code)


def orch_delete_function(slug: str, name: str) -> bool:
    """Delete a custom function."""
    return delete_orchestrator_function(slug, name)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
