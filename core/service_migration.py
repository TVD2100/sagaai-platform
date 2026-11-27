"""
core.service_migration - carry user settings from a retired service profile.

When a provider leaves the platform, the owner's saved choices - the API key
in the configuration store, the service and model of every assistant and the
per-role model fields of every orchestrator - still point at it. Left alone
they surface as "service not found" instead of an honest answer, and every
owner has to re-pick every model by hand.

This module performs that rewrite once. It is idempotent: after the first
run no record mentions the retired service, so later runs find nothing and
return an all-zero report. It never raises - a record that cannot be read or
written is reported per step and left untouched, because a half-rewritten
configuration is worse than an unmigrated one.

Retired services are described as a plain dict, so a future retirement adds
a table entry instead of a new function.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("core.service_migration")

#: Services that left the platform, with everything needed to rewrite a record.
#:
#: ``drop_config_keys`` names the stored values that belonged to the retired
#: service and are removed: an API key is issued by one provider for its own
#: gateway, so it cannot authenticate against the replacement and keeping it
#: in the slot of the new service turns an honest "key not set" into an
#: authorization failure. The owner enters the new key once; every record that
#: names the service migrates on its own. ``models`` maps a retired model id to
#: its successor; ``default_model`` is used when the retired id has no explicit
#: successor.
RETIRED_SERVICES: Dict[str, Dict[str, Any]] = {
    "GigaChat": {
        "replacement": "Cloud",
        "drop_config_keys": ("GIGACHAT_API_KEY", "GIGACHAT_SCOPE"),
        "models": {
            "GigaChat-3-Ultra": "GigaChat/GigaChat-4-708B-A41",
            "GigaChat-2-Max": "GigaChat/GigaChat-4-708B-A41",
            "GigaChat-3-Pro": "GigaChat/GigaChat-3-Pro",
            "GigaChat-2-Pro": "GigaChat/GigaChat-3-Pro",
            "GigaChat-3-Lightning": "ai-sage/GigaChat3-10B-A1.8B",
            "GigaChat-2": "ai-sage/GigaChat3-10B-A1.8B",
        },
        "default_model": "GigaChat/GigaChat-4-708B-A41",
    },
}

#: Orchestrator config fields holding a service / model pair, service first.
_ROLE_FIELDS: Tuple[str, ...] = (
    "strong_service", "strong_model",
    "vision_service", "vision_model",
    "image_service", "image_model",
    "search_service", "search_model",
)


def migrated_model(table: Dict[str, Any], model_id: str) -> str:
    """Return the successor of *model_id* for one retirement *table*.

    An exact entry wins; an unknown id falls back to the table's default, so a
    record can never keep a model the replacement service cannot serve.
    """
    models = table.get("models") or {}
    return str(models.get(model_id) or table.get("default_model") or "")


def _as_int(value: Any) -> Optional[int]:
    """The stored value as an int, or None when it is absent or unusable.

    A retired record may hold a numeric setting as a string, or hold nothing
    at all; the update call treats ``None`` as "clear the field", which is
    what a value that cannot be read should do.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _migrate_config(table: Dict[str, Any], report: Dict[str, Any]) -> None:
    """Drop the stored values that belonged to the retired service."""
    from core.config import load_stored_config, save_config

    drop = tuple(name for name in (table.get("drop_config_keys") or ()) if name)
    if not drop:
        return
    try:
        config = load_stored_config()
    except Exception as exc:  # pragma: no cover - defensive
        report["config"] = "error: " + str(exc)
        return

    removed = [name for name in drop if name in config]
    if not removed:
        report["config"] = "unchanged"
        return
    for name in removed:
        config.pop(name, None)
    try:
        ok = save_config(config)
    except Exception as exc:  # pragma: no cover - defensive
        report["config"] = "error: " + str(exc)
        return
    report["config"] = "updated" if ok else "error: save failed"
    report["config_keys_removed"] = removed


def _migrate_assistants(retired: str, table: Dict[str, Any],
                        report: Dict[str, Any]) -> None:
    """Repoint every assistant that references the retired service."""
    from core.assistants import update_assistant
    from storage.repository import repo_get_assistant_with_text, repo_load_assistants

    try:
        records = repo_load_assistants()
    except Exception as exc:  # pragma: no cover - defensive
        report["assistants"] = "error: " + str(exc)
        return

    affected: List[str] = [str(r.get("id") or "") for r in records
                           if str(r.get("service") or "") == retired]
    if not affected:
        report["assistants"] = "unchanged"
        return

    migrated = 0
    failed: List[str] = []
    for assistant_id in affected:
        full = repo_get_assistant_with_text(assistant_id)
        if not full:
            failed.append(assistant_id)
            continue
        max_tokens = full.get("max_tokens")
        try:
            ok = update_assistant(
                pid=assistant_id,
                name=full.get("name", ""),
                service=str(table["replacement"]),
                model=migrated_model(table, str(full.get("model") or "")),
                temperature=full.get("temperature", 0.7),
                text=full.get("text", ""),
                description=full.get("description", ""),
                tools=full.get("tools") or [],
                max_tool_calls=_as_int(full.get("max_tool_calls")),
                max_tokens=_as_int(max_tokens),
                reasoning_effort=(str(full["reasoning_effort"])
                                  if full.get("reasoning_effort") else None),
                name_en=full.get("name_en", ""),
                name_ru=full.get("name_ru", ""),
                name_zh=full.get("name_zh", ""),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("assistant %s not migrated: %s", assistant_id, exc)
            ok = False
        if ok:
            migrated += 1
        else:
            failed.append(assistant_id)
    report["assistants"] = migrated
    if failed:
        report["assistants_failed"] = failed


def _migrate_orchestrators(retired: str, table: Dict[str, Any],
                           report: Dict[str, Any]) -> None:
    """Rewrite the per-role model fields of every orchestrator config."""
    from storage.repository import repo_list_orchestrators, repo_update_orchestrator

    try:
        records = repo_list_orchestrators()
    except Exception as exc:  # pragma: no cover - defensive
        report["orchestrators"] = "error: " + str(exc)
        return

    migrated = 0
    failed: List[str] = []
    for record in records:
        config = record.get("config")
        if not isinstance(config, dict):
            continue
        changed = False
        for index in range(0, len(_ROLE_FIELDS) - 1, 2):
            service_field = _ROLE_FIELDS[index]
            model_field = _ROLE_FIELDS[index + 1]
            if str(config.get(service_field) or "") != retired:
                continue
            config[service_field] = str(table["replacement"])
            config[model_field] = migrated_model(table, str(config.get(model_field) or ""))
            changed = True
        if not changed:
            continue
        try:
            ok = repo_update_orchestrator(record["id"], config=config)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("orchestrator %s not migrated: %s", record.get("slug"), exc)
            ok = False
        if ok:
            migrated += 1
        else:
            failed.append(str(record.get("slug") or record.get("id") or ""))
    report["orchestrators"] = migrated
    if failed:
        report["orchestrators_failed"] = failed


def migrate_retired_services() -> Dict[str, Any]:
    """Carry every stored choice off the retired services.

    Returns ``{"ok": bool, "services": {retired: report}}`` where each report
    counts what changed and names the records that could not be rewritten.
    ``ok`` is False only when a write genuinely failed; nothing to migrate is
    the expected state of an already-current installation and reports
    ``"unchanged"`` per section.
    """
    result: Dict[str, Any] = {"ok": True, "services": {}}
    for retired, table in RETIRED_SERVICES.items():
        report: Dict[str, Any] = {}
        _migrate_config(table, report)
        _migrate_assistants(retired, table, report)
        _migrate_orchestrators(retired, table, report)
        failures = [key for key, value in report.items()
                    if isinstance(value, str) and value.startswith("error:")]
        failures += [key for key in ("assistants_failed", "orchestrators_failed")
                     if report.get(key)]
        if failures:
            result["ok"] = False
        result["services"][retired] = report
    return result

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
