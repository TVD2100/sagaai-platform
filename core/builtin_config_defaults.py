# -*- coding: utf-8 -*-
"""
core.builtin_config_defaults - the config a fresh built-in DevAgent starts with.

Kept apart from ``core.orchestrator_builtin`` so the boot module stays a flow
of decisions rather than a wall of literals: the backfill compares a stored
config against this table and fills only the keys that are missing or unset.
"""
from typing import Any, Dict


def builtin_config_defaults() -> Dict[str, Any]:
    """Return the default config of the built-in DevAgent.

    A fresh dict every call: the caller writes into it while backfilling and
    must never share state with another boot.
    """
    from core.config import get_default_strong_max_tokens
    from core.orchestrator_defaults import (
        DEFAULT_WEB_SEARCH_PROMPT,
        _default_economy_cache_enabled,
        _default_economy_cache_multiplier,
        _default_economy_tail_messages,
    )

    return {
        "strong_service": "DeepSeek",
        "strong_model": "deepseek-flash",
        "strong_temperature": 0.4,
        "strong_max_tokens": get_default_strong_max_tokens(),
        "strong_reasoning_effort": "xhigh",
        "vision_service": "DeepSeek",
        "vision_model": "deepseek-flash",
        "image_service": "",
        "image_model": "",
        "search_service": "DeepSeek",
        "search_model": "deepseek-flash",
        "search_temperature": 0.4,
        "search_max_tool_calls": 3,
        "search_reasoning_effort": "high",
        "web_search_prompt": DEFAULT_WEB_SEARCH_PROMPT,
        "fetch_service": "",
        "fetch_model": "",
        "fetch_selenium": False,
        "economy_tail_messages": _default_economy_tail_messages(),
        "economy_cache_enabled": _default_economy_cache_enabled(),
        "economy_cache_multiplier": _default_economy_cache_multiplier(),
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
