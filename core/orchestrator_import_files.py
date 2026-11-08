# -*- coding: utf-8 -*-
"""
core.orchestrator_import_files - the file parts of an imported bundle.

An exported orchestrator carries its instructions and custom functions in
one JSON object. Importing them means writing them into the employee folder,
which is what this module does; the bundle validation and the database write
stay in ``core.orchestrator_transfer``.
"""
from core.orchestrator_folders import (
    ensure_orchestrator_dir,
    save_orchestrator_function,
    save_orchestrator_instruction,
)


def _import_instructions(slug: str, instructions: list) -> int:
    """Import instruction dicts into the orchestrator folder. Returns count.

    Instructions are saved as orchestrator-specific instructions of *slug*
    (same location export_orchestrator reads them from). Existing ids are
    overwritten with the imported content. The global instructions table is
    never touched by imports.
    """
    imported = 0
    for inst in (instructions or []):
        if not isinstance(inst, dict):
            continue
        iid = str(inst.get("id", "") or "").strip()
        if not iid:
            continue
        ok = save_orchestrator_instruction(
            slug,
            iid,
            name=str(inst.get("name", "") or iid),
            description=str(inst.get("description", "") or ""),
            prompt_text=str(inst.get("prompt_text", "") or ""),
        )
        if ok:
            imported += 1
    return imported


def _import_functions(slug: str, functions: dict) -> int:
    """Import custom functions into the orchestrator folder. Returns count."""
    imported = 0
    if not isinstance(functions, dict):
        return imported
    ensure_orchestrator_dir(slug)
    for fname, code in functions.items():
        if not isinstance(fname, str) or not isinstance(code, str):
            continue
        if save_orchestrator_function(slug, fname, code):
            imported += 1
    return imported


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
