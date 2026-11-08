# -*- coding: utf-8 -*-
"""
core.orchestrator_strong - which provider and model an employee runs on.

An agent installed from a catalog does not have to name a model: the platform
supplies one. The chat page that decides whether the employee can run at all,
the prompt builder that actually calls the provider and the fetch settings all
ask this module, so the answers cannot drift apart.

The bundled employee slug is re-exported here: the page that asks the question
also labels that employee, and one import serves both.
"""
from typing import Any, Dict, Tuple

from core.orchestrator_defaults import DEVAGENT_SLUG
from core.orchestrator_shared import late

# Resolved on the facade when called, never bound at import time.
get_orchestrator = late("get_orchestrator")

__all__ = [
    "DEFAULT_STRONG_MODEL",
    "DEFAULT_STRONG_SERVICE",
    "DEVAGENT_SLUG",
    "get_strong_model",
    "resolve_strong_model",
    "resolve_strong_service",
    "strong_pair",
]

#: Provider and model an orchestrator runs on when its config names neither.
#: These are the values every bundled employee starts with, so an agent
#: installed from a catalog runs like a bundled one after install.
DEFAULT_STRONG_SERVICE = "DeepSeek"


DEFAULT_STRONG_MODEL = "deepseek-flash"


def _pick(cfg: Dict[str, Any], primary: str, legacy: str, fallback: str) -> str:
    """Return the first non-empty value of *primary* or *legacy*, else the fallback."""
    for key in (primary, legacy):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return fallback


def resolve_strong_model(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Return the provider and model *cfg* resolves to.

    A config does not have to name a model: an agent installed from a catalog
    may ship only its own settings and rely on the platform for the rest. The
    chain is ``strong_service`` -> ``service`` -> the platform default, and
    the same for the model, so a config written in the older single-model
    format keeps working.

    Parameters:
        cfg: the ``config`` mapping of an orchestrator; a non-mapping is
            treated as empty.

    Returns:
        ``{"service": str, "model": str}`` - both non-empty for any input.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "service": _pick(cfg, "strong_service", "service", DEFAULT_STRONG_SERVICE),
        "model": _pick(cfg, "strong_model", "model", DEFAULT_STRONG_MODEL),
    }


def strong_pair(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """Return ``(service, model)`` of *cfg* for callers that need both."""
    resolved = resolve_strong_model(cfg)
    return resolved["service"], resolved["model"]


def resolve_strong_service(cfg: Dict[str, Any]) -> str:
    """Return only the provider *cfg* runs on; never empty for any input."""
    return resolve_strong_model(cfg)["service"]


def get_strong_model(orchestrator_slug: str = DEVAGENT_SLUG) -> Dict[str, str]:
    """Return the provider and model *orchestrator_slug* resolves to.

    An unknown slug answers empty strings: the caller then has no agent to
    run at all, which is a different situation from an agent whose config
    simply names no model.
    """
    orch = get_orchestrator(orchestrator_slug)
    if not orch:
        return {"service": "", "model": ""}
    return resolve_strong_model(orch.get("config") or {})


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
