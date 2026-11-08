# -*- coding: utf-8 -*-
"""
core.marketplace.agent_merge - which installed values an update may move.

An update refreshes an entity, it never overwrites a deliberate choice. The
rule is the same for both package kinds: a value the user never touched
follows the new package, a value they changed stays theirs. The baseline for
that comparison is the manifest of the installed version, read from the
cached package - without a cache every installed value counts as the user's
own, which is the safe direction (an update then changes nothing rather than
overwriting a deliberate setting).

Keeping the merge rules in one module lets the orchestrator and the helper
installers share them without importing each other's install flow.
"""
from typing import Optional

from core.marketplace import store
from core.naming import merge_name_slots


def _merge_untouched(installed, package, previous):
    """Return the package value when the user never changed the installed one.

    A value equal to what the installed package shipped is a value the user
    left alone, so the update may move it. Any other value is the user's own
    and stays.
    """
    if installed == previous:
        return package
    return installed


def _merge_update_config(installed: dict, package: dict, previous: dict) -> dict:
    """Return the config an update writes, keeping what the user changed.

    A key the user never touched takes the value the new package ships; a key
    the user did change keeps the user's value.
    """
    result = dict(installed or {})
    baseline = previous if isinstance(previous, dict) else {}
    for key, value in (package or {}).items():
        if key not in result:
            result[key] = value
            continue
        if result.get(key) == baseline.get(key):
            # Untouched since the installed version: the catalog may move it.
            result[key] = value
    return result


def _previous_package_meta(existing: Optional[dict],
                           names: tuple = ("orchestrator.json", "settings.json")) -> dict:
    """Return the bundle metadata the installed package shipped.

    The package of the installed version is cached under its sha256, so the
    baseline is read from there; an absent cache answers an empty dict, which
    makes the merge keep the installed values.
    """
    package_sha = str((existing or {}).get("package_sha256") or "")
    if not package_sha:
        return {}
    try:
        package = store.get_cached_package(package_sha)
    except Exception:
        return {}
    if not package:
        return {}
    import io
    import json
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name not in names:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                data = json.loads(handle.read().decode("utf-8"))
                if isinstance(data, dict):
                    return data
    except Exception:
        return {}
    return {}


def _previous_package_text(existing: Optional[dict], name: str) -> str:
    """Return one text member of the installed package, or '' when absent.

    The baseline for "did the user edit this" lives in the cached package of
    the installed version; an absent cache answers an empty string, which
    makes the caller keep the installed text.
    """
    package_sha = str((existing or {}).get("package_sha256") or "")
    if not package_sha:
        return ""
    try:
        package = store.get_cached_package(package_sha)
    except Exception:
        return ""
    if not package:
        return ""
    import io
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name != name:
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    return ""


def _merge_assistant_payload(payload: dict, installed_row: dict,
                             previous: dict, existing: Optional[dict]) -> dict:
    """Return the helper payload an update writes, keeping user choices.

    A field the user never changed follows the new package, a field they did
    change stays. Without a baseline every installed value counts as the
    user's own.
    """
    bundle = dict(payload.get("bundle") or {})
    for key in (
        "description", "service", "model", "temperature",
        "max_tool_calls", "max_tokens", "reasoning_effort",
    ):
        bundle[key] = _merge_untouched(
            installed_row.get(key), bundle.get(key), previous.get(key)
        )
    # The name slots follow the package while the user has not changed them;
    # the owner's own name survives an update (see ``core.naming``).
    bundle = merge_name_slots(bundle, installed_row, previous)
    bundle["tools"] = _merge_untouched(
        list(installed_row.get("tools") or []),
        list(bundle.get("tools") or []),
        list(previous.get("tools") or []),
    )
    previous_prompt = _previous_package_text(existing, "prompt.md")
    installed_prompt = str(installed_row.get("text") or "")
    if installed_prompt.strip() and installed_prompt != previous_prompt:
        payload["prompt_text"] = installed_prompt
    payload["bundle"] = bundle
    return payload


def _merge_orchestrator_update(bundle: dict, existing: Optional[dict], slug: str) -> dict:
    """Keep the values the user changed, move the ones they never touched."""
    from core.orchestrators import get_orchestrator

    previous = _previous_package_meta(existing)
    installed_row = get_orchestrator(slug) or {}
    bundle["config"] = _merge_update_config(
        installed_row.get("config") or {},
        bundle.get("config") or {},
        previous.get("config") or {},
    )
    bundle["tools"] = _merge_untouched(
        list(installed_row.get("tools") or []),
        list(bundle.get("tools") or []),
        list(previous.get("tools") or []),
    )
    if "max_steps" in previous:
        bundle["max_steps"] = _merge_untouched(
            installed_row.get("max_steps"), bundle.get("max_steps"), previous["max_steps"],
        )
    if "auto_apply" in previous:
        bundle["auto_apply"] = _merge_untouched(
            installed_row.get("auto_apply"), bundle.get("auto_apply"), previous["auto_apply"],
        )
    return merge_name_slots(bundle, installed_row, previous)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
