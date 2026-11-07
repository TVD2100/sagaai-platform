# -*- coding: utf-8 -*-
"""
core.orchestrator_models - model, web-search, fetch and economy settings of an orchestrator.

Part of ``core.orchestrators``, which is the public surface of this
package. A name the tests may replace through the facade is resolved late,
at call time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
from typing import Any, Dict

from core.orchestrator_defaults import (
    DEFAULT_WEB_SEARCH_PROMPT,
    DEVAGENT_SLUG,
    _default_economy_cache_enabled,
    _default_economy_cache_multiplier,
    _default_economy_tail_messages,
)
from core.orchestrator_shared import late
from core.services import (
    default_reasoning_effort,
    get_fetch_models,
    service_supports_reasoning_effort,
    svc_supports_fetch,
)

# Resolved on the facade when called, never bound at import time.
_get_services = late("_get_services")
get_orchestrator = late("get_orchestrator")
get_web_search_prompt = late("get_web_search_prompt")


def get_web_search_prompt_impl(orchestrator_slug: str = DEVAGENT_SLUG) -> str:
    """Return the base system prompt for the web-search sub-agent.

    Reads the orchestrator's ``web_search_prompt`` config key. Falls back to
    ``DEFAULT_WEB_SEARCH_PROMPT`` when the key is missing or empty so older
    orchestrators keep working unchanged.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    prompt = cfg.get("web_search_prompt", "")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    return DEFAULT_WEB_SEARCH_PROMPT


def get_web_search_config(orchestrator_slug: str = DEVAGENT_SLUG) -> Dict[str, Any]:
    """Return the web-search config for an orchestrator.

    Returns {"service": str, "model": str, "temperature": float,
    "max_tool_calls": int, "prompt": str} or empty strings/0 for
    service/model when not configured.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    search_svc = cfg.get("search_service", "") or ""
    search_svc_def = _get_services().get(search_svc, {})
    search_effort = cfg.get("search_reasoning_effort", "")
    search_mdl = cfg.get("search_model", "") or ""
    if not (isinstance(search_effort, str) and search_effort.strip()):
        if service_supports_reasoning_effort(search_svc_def):
            search_effort = default_reasoning_effort(search_svc_def, strong=False, model=search_mdl) or "high"
        else:
            search_effort = ""
    return {
        "service": search_svc,
        "model": cfg.get("search_model", "") or "",
        "temperature": float(cfg.get("search_temperature", 0.4) or 0.4),
        "max_tool_calls": int(cfg.get("search_max_tool_calls", 3) or 3),
        "reasoning_effort": str(search_effort).strip().lower() if search_effort else "",
        "prompt": get_web_search_prompt(orchestrator_slug),
    }


def get_fetch_config(orchestrator_slug: str = DEVAGENT_SLUG) -> Dict[str, Any]:
    """Return the url-fetch config of an orchestrator.

    Two independent switches keep the ``url_fetch`` tool available:

    * a page-fetch model - pages are downloaded through the provider's
      Search/Fetch API (the entry is valid only when both the service and
      its catalog still contain the model, so a removed provider or model
      silently switches this branch off);
    * ``fetch_selenium`` - pages are rendered in a real browser by
      Selenium and the visible text is converted to Markdown by the
      orchestrator's main model.

    A direct PDF link works with either switch: the file is downloaded and
    its text is extracted locally.

    Returns:
        {"service": str, "model": str, "configured": bool,
        "selenium": bool, "llm_service": str, "llm_model": str}.
        ``service``/``model`` are "" when no usable fetch pair is assigned;
        ``selenium`` mirrors the checkbox; ``llm_service``/``llm_model``
        name the main model used for Markdown conversion ("" when the
        orchestrator has none). ``configured`` is True when url_fetch is
        usable at all - a fetch pair or Selenium.
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    svc_name = str(cfg.get("fetch_service", "") or "").strip()
    model = str(cfg.get("fetch_model", "") or "").strip()
    selenium = bool(cfg.get("fetch_selenium"))
    llm_service = str(cfg.get("strong_service", "") or "").strip()
    llm_model = str(cfg.get("strong_model", "") or "").strip()

    def _result(pair_service: str, pair_model: str, configured: bool):
        return {
            "service": pair_service,
            "model": pair_model,
            "configured": configured,
            "selenium": selenium,
            "llm_service": llm_service,
            "llm_model": llm_model,
        }

    if not svc_name or not model:
        return _result("", "", selenium)
    svc_def = _get_services().get(svc_name) or {}
    if not svc_supports_fetch(svc_def):
        return _result("", "", selenium)
    available = [
        str(m.get("id") if isinstance(m, dict) else m)
        for m in get_fetch_models(svc_def)
    ]
    if model not in available:
        return _result("", "", selenium)
    return _result(svc_name, model, True)


def get_economy_tail_messages(orchestrator_slug: str = DEVAGENT_SLUG) -> int:
    """Return the number of recent messages passed in economy mode.

    Reads the value from the orchestrator config. If no value is stored
    (first boot), falls back to the default from the canonical DevAgent
    orchestrator bundle (orchestrators/dev_agent/orchestrator.json).
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    raw = cfg.get("economy_tail_messages")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return _default_economy_tail_messages()


def get_economy_cache_enabled(orchestrator_slug: str = DEVAGENT_SLUG) -> bool:
    """Return whether cache-friendly economy mode is enabled for an orchestrator.

    Reads the value from the orchestrator config. If no value is stored
    (first boot or older config), falls back to the default from the
    canonical DevAgent orchestrator bundle
    (orchestrators/dev_agent/orchestrator.json).
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    raw = cfg.get("economy_cache_enabled")
    if raw is not None:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return _default_economy_cache_enabled()


def get_economy_cache_multiplier(orchestrator_slug: str = DEVAGENT_SLUG) -> int:
    """Return the cache-window multiplier (xN) for an orchestrator.

    Reads the value from the orchestrator config. If no value is stored
    (first boot or older config), falls back to the default from the
    canonical DevAgent orchestrator bundle
    (orchestrators/dev_agent/orchestrator.json).
    """
    orch = get_orchestrator(orchestrator_slug)
    cfg = orch.get("config", {}) if orch else {}
    raw = cfg.get("economy_cache_multiplier")
    if raw is not None:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return _default_economy_cache_multiplier()


def get_economy_config(orchestrator_slug: str = DEVAGENT_SLUG) -> Dict[str, Any]:
    """Return economy-mode settings for an orchestrator."""
    return {
        "tail_messages": get_economy_tail_messages(orchestrator_slug),
        "cache_enabled": get_economy_cache_enabled(orchestrator_slug),
        "cache_multiplier": get_economy_cache_multiplier(orchestrator_slug),
    }

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
