# -*- coding: utf-8 -*-
"""
core.orchestrator_transfer - export and import of an orchestrator bundle.

Part of ``core.orchestrators``, which is the public surface of this
package. A name the tests may replace through the facade is resolved late,
at call time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.naming import bundle_name_fields, names_from_bundle
from core.orchestrator_defaults import (
    _MAX_PROMPT_TEXT_LENGTH,
    _get_known_tool_names,
)
from core.orchestrator_folders import (
    get_orchestrator_function,
    get_orchestrator_instruction,
    list_orchestrator_functions,
    list_orchestrator_instructions,
    safe_orchestrator_slug,
)
from core.orchestrator_import_files import (  # noqa: F401  re-exported
    _import_functions,
    _import_instructions,
)
from core.orchestrator_shared import late
from storage.models import DEFAULT_MAX_STEPS
from storage.repository import repo_create_orchestrator

# Resolved on the facade when called, never bound at import time.
_sync_orchestrator_folder = late("_sync_orchestrator_folder")
get_orchestrator = late("get_orchestrator")
repo_get_orchestrator_with_text = late("repo_get_orchestrator_with_text")
repo_update_orchestrator = late("repo_update_orchestrator")


_EXPORT_FORMAT = "sagaai_orchestrator/v1"


def export_orchestrator(slug: str) -> Optional[Dict[str, Any]]:
    """Export an orchestrator as a JSON-serializable dict.

    Includes the orchestrator's personal folder data: custom functions
    (raw Python source) and orchestrator-specific instructions. The global
    DevAgent instructions are NOT included in this export.
    """
    orch = get_orchestrator(slug)
    if orch is None:
        return None

    # Gather orchestrator-specific instructions from the folder.
    orch_instr = []
    for inst in list_orchestrator_instructions(slug):
        full = get_orchestrator_instruction(slug, inst["id"])
        if full:
            orch_instr.append({
                "id": full.get("id", ""),
                "name": full.get("name", ""),
                "description": full.get("description", ""),
                "prompt_text": full.get("text", ""),
            })

    # Gather custom functions (raw source code).
    functions = {}
    for fn in list_orchestrator_functions(slug):
        full = get_orchestrator_function(slug, fn["name"])
        if full:
            functions[fn["name"]] = full["code"]

    return {
        "format": _EXPORT_FORMAT,
        "slug": orch.get("slug", ""),
        # The file keeps the four name slots apart: the user-defined name
        # goes to ``name`` and the translations to their own keys.
        **bundle_name_fields(orch),
        "description": orch.get("description", ""),
        "prompt_text": orch.get("prompt_text", ""),
        "config": orch.get("config", {}),
        "tools": orch.get("tools", []),
        "max_steps": orch.get("max_steps", DEFAULT_MAX_STEPS),
        "auto_apply": orch.get("auto_apply", True),
        "instructions": orch_instr,
        "functions": functions,
        "exported_at": datetime.now().isoformat(),
    }


def _validate_imported_tools(tools: list) -> Tuple[list, List[str]]:
    """Validate a tool-name list against the known catalog.

    Returns (filtered_tools, rejected_tool_names). Unknown tools are
    silently dropped; rejected names are returned for reporting.
    """
    if not tools:
        return [], []
    known = _get_known_tool_names()
    filtered: list = []
    rejected: List[str] = []
    for t in tools:
        if isinstance(t, str):
            if t in known:
                filtered.append(t)
            else:
                rejected.append(t)
        elif isinstance(t, dict):
            # Dict tools (e.g. {"type": "web_search", ...}) are always
            # accepted; they are validated at runtime by the API layer.
            filtered.append(t)
        else:
            rejected.append(str(t))
    return filtered, rejected


def _validated_bundle_slug(data: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return the slug of an import bundle, or the refusal to hand back.

    A slug that differs from its normalized form is refused: a path-traversal
    attempt like ``../evil`` normalizes to something else and must not be
    silently imported under a different name.
    """
    if not isinstance(data, dict):
        return "", {"ok": False, "error": "Invalid export data: not a dict.", "slug": ""}
    if data.get("format") != _EXPORT_FORMAT:
        return "", {"ok": False, "error": f"Unknown format: {data.get('format', 'none')}", "slug": ""}
    raw = str(data.get("slug") or "").strip()
    slug = safe_orchestrator_slug(raw)
    if not slug or slug != raw.lower():
        return "", {"ok": False, "error": "Missing or invalid slug in export data.", "slug": ""}
    return slug, None


def import_orchestrator(data: Dict[str, Any], overwrite: bool = False) -> Dict[str, Any]:
    """Import an orchestrator from an export dict.

    Returns {"ok": bool, "slug": str, "error": str, "instructions_imported": int,
             "functions_imported": int, "tools_filtered": int, "tools_rejected": [...]}.
    If ``overwrite`` is True and the slug already exists, the existing
    orchestrator is updated in-place. Otherwise a new slug is generated.

    Validation performed during import:
      - The format marker must be "sagaai_orchestrator/v1".
      - prompt_text is truncated if it exceeds the configured limit.
      - tools are filtered: unknown tool names are silently dropped.
      - For built-in orchestrators (is_builtin=True), prompt_text is
        NEVER overwritten - it always comes from the bundled
        system_prompt.md file.
    """
    slug, error = _validated_bundle_slug(data)
    if error:
        return error

    name_fields = names_from_bundle(data)
    name = name_fields["name"]
    description = data.get("description", "")
    prompt_text = data.get("prompt_text", "")
    config = data.get("config", {}) or {}
    raw_tools = data.get("tools", []) or []
    max_steps = int(data.get("max_steps", DEFAULT_MAX_STEPS) or DEFAULT_MAX_STEPS)
    auto_apply = bool(data.get("auto_apply", True))

    # Validate prompt_text length.
    if isinstance(prompt_text, str) and len(prompt_text) > _MAX_PROMPT_TEXT_LENGTH:
        prompt_text = prompt_text[:_MAX_PROMPT_TEXT_LENGTH] + "\n...[truncated on import]...\n"

    # Validate tools against the known catalog.
    tools, tools_rejected = _validate_imported_tools(raw_tools)

    existing = repo_get_orchestrator_with_text(slug)

    if existing:
        if not overwrite:
            # Generate a new slug: append "-2", "-3", etc.
            base = slug.rstrip("_0123456789")
            suffix = 2
            while True:
                candidate = f"{base}_{suffix}"
                if not repo_get_orchestrator_with_text(candidate):
                    slug = candidate
                    break
                suffix += 1
        else:
            # Update existing.
            # For built-in orchestrators, NEVER overwrite prompt_text -
            # it is always refreshed from the bundled file on boot.
            update_kwargs: Dict[str, Any] = dict(
                name=name,
                description=description,
                config=config,
                tools=tools,
                max_steps=max_steps,
                auto_apply=auto_apply,
                **{key: value for key, value in name_fields.items() if key != "name"},
            )
            if not existing.get("is_builtin"):
                update_kwargs["prompt_text"] = prompt_text
            repo_update_orchestrator(existing["id"], **update_kwargs)

            instructions_imported = _import_instructions(slug, data.get("instructions", []))
            functions_imported = _import_functions(slug, data.get("functions", {}))
            _sync_orchestrator_folder(slug)
            return {
                "ok": True,
                "slug": slug,
                "action": "updated",
                "instructions_imported": instructions_imported,
                "functions_imported": functions_imported,
                "tools_filtered": len(tools),
                "tools_rejected": tools_rejected,
            }

    # Create new.
    orch_id = uuid.uuid4().hex[:8]
    ok = repo_create_orchestrator(
        orchestrator_id=orch_id,
        slug=slug,
        name=name,
        description=description,
        prompt_text=prompt_text,
        config=config,
        tools=tools,
        max_steps=max_steps,
        auto_apply=auto_apply,
        is_builtin=False,
        sort_order=100,
        **{key: value for key, value in name_fields.items() if key != "name"},
    )
    if not ok:
        return {"ok": False, "error": "Failed to create orchestrator in DB.", "slug": ""}

    instructions_imported = _import_instructions(slug, data.get("instructions", []))
    functions_imported = _import_functions(slug, data.get("functions", {}))
    _sync_orchestrator_folder(slug)
    return {
        "ok": True,
        "slug": slug,
        "action": "created",
        "instructions_imported": instructions_imported,
        "functions_imported": functions_imported,
        "tools_filtered": len(tools),
        "tools_rejected": tools_rejected,
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
