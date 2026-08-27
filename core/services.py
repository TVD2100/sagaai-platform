"""
core.services - service discovery (JSON files in services/ directory).
No streamlit imports; uses functools.lru_cache instead of @st.cache_data.

Default provider definitions live in defaults/services/; the legacy services/
folders remains a fallback for installations created before the defaults/
layout. Discovered services are merged (defaults win on name conflicts).
"""
import os
import json
from functools import lru_cache
from core.paths import SERVICES_DIR


def _scan_dir(directory: str, result: dict) -> None:
    """Scan one directory for *.json service definitions and merge them in."""
    if not os.path.isdir(directory):
        return
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(directory, fname)
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name", fname.replace(".json", ""))
            result[name] = data
        except Exception:
            pass


def discover_services() -> dict:
    """
    Scan the default and legacy service-definition directories for *.json
    files. Returns {service_name: service_data_dict}.
    """
    result: dict = {}
    # Legacy location first so defaults/ wins on name conflicts.
    _scan_dir(SERVICES_DIR, result)
    try:
        from core import defaults
        _scan_dir(defaults.services_dir(), result)
    except Exception:
        pass
    return result


def get_services() -> dict:
    """Return cached service definitions (TTL-less)."""
    return _cached_services()


@lru_cache(maxsize=1)
def _cached_services() -> dict:
    return discover_services()


# ─── Reasoning-effort helpers ──────────────────────────────────────────────

def get_reasoning_effort_options(svc: dict) -> list:
    """Return the reasoning-effort options declared by a service (if any).

    Options come from the service definition's ``extra_fields`` entry with
    key ``reasoning_effort`` (type ``select``). Returns [] when the service
    does not support reasoning-effort control.
    """
    if not isinstance(svc, dict):
        return []
    for field in svc.get("extra_fields", []) or []:
        if not isinstance(field, dict):
            continue
        if field.get("key") == "reasoning_effort" and field.get("type") == "select":
            opts = field.get("options", []) or []
            return [str(o) for o in opts]
    return []


def _model_entry(svc: dict, model_id: str) -> dict:
    """Return the models[] entry of *svc* matching *model_id* (or {}).

    Handles both dict entries (the normal case) and legacy plain-string
    entries. Empty model ids never match.
    """
    if not isinstance(svc, dict) or not model_id:
        return {}
    for entry in svc.get("models", []) or []:
        if isinstance(entry, dict) and str(entry.get("id", "")) == str(model_id):
            return entry
        if isinstance(entry, str) and entry == str(model_id):
            return {"id": entry}
    return {}


def get_model_reasoning_effort_options(svc: dict, model_id: str) -> list:
    """Return the reasoning-effort options supported by a specific model.

    Reads the model entry's ``reasoning_effort_options`` field (a more
    precise per-model list added to services/*.json). Falls back to the
    service-wide extra_fields selector when the model entry does not
    declare its own list (older presets and services without per-model
    restrictions). Returns [] when neither source is available.
    """
    entry = _model_entry(svc, model_id)
    opts = entry.get("reasoning_effort_options")
    if isinstance(opts, list) and opts:
        seen = []
        for opt in opts:
            val = str(opt)
            if val not in seen:
                seen.append(val)
        return seen
    return get_reasoning_effort_options(svc)


def service_supports_reasoning_effort(svc: dict) -> bool:
    """Return True when the service exposes a reasoning-effort selector."""
    return bool(get_reasoning_effort_options(svc))


def default_reasoning_effort(svc: dict, strong: bool = False, model: str = None) -> str:
    """Return the default reasoning effort for a service (and model).

    *strong* (orchestrator's main model): ``max`` when the service supports
    it, otherwise ``high``. For all other roles (weak/search models,
    assistants): ``high`` when available, otherwise the first supported
    option. When *model* is given and the model declares its own
    ``reasoning_effort_options``, only values supported by that model are
    considered. Returns "" when the service does not support reasoning
    effort or when "" is the only allowed option.
    """
    opts = get_reasoning_effort_options(svc)
    if not opts:
        return ""
    model_opts = []
    if model:
        model_opts = _model_entry(svc, model).get("reasoning_effort_options")
    if isinstance(model_opts, list):
        opts = [str(o) for o in model_opts]
    if not opts:
        return ""
    if strong:
        if "max" in opts:
            return "max"
    if "high" in opts:
        return "high"
    non_empty = [o for o in opts if o != ""]
    if non_empty:
        return non_empty[0]
    return ""


# ─── RAG models helpers ────────────────────────────────────────────────────

def get_embedding_models(svc: dict) -> list:
    """Return embedding/vectorization models declared by a service (if any).

    Each entry: {"id", "name", "dimension", "max_tokens", "label"} - matching
    the ``embedding_models`` block in the service JSON file. Returns [] when
    the service does not declare embedding models (e.g. GigaChat on stage 1).
    """
    if not isinstance(svc, dict):
        return []
    models = svc.get("embedding_models", []) or []
    return [m for m in models if isinstance(m, dict)]


def get_rag_models(svc: dict) -> list:
    """Return models recommended for RAG generation by a service (if any).

    Each entry: {"id", "label"} - matching the ``rag_models`` block in the
    service JSON file. Returns [] when the service does not declare RAG
    models.
    """
    if not isinstance(svc, dict):
        return []
    models = svc.get("rag_models", []) or []
    return [m for m in models if isinstance(m, dict)]


def service_supports_embeddings(svc: dict) -> bool:
    """Return True when the service declares at least one embedding model.

    A RAG base can only be indexed/searched when its provider supports
    vectorization; this flag is what the storage UI uses to enable/disable
    indexing for a selected provider.
    """
    return bool(get_embedding_models(svc))
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
