# -*- coding: utf-8 -*-
"""tests/scenarios/test_provider_economy_settings_scenario.py - user-level
scenario tests for the provider settings cleanup and the new economy scale.

Scenarios (given -> when -> then):

  Scenario 1 - YandexAI provider settings expose only reasoning_effort:
               the removed web-search plumbing fields are gone from both
               service copies and from the effective runtime service.
  Scenario 2 - GigaChat environment fallback is limited to one key:
               SAGAAI_GIGACHAT_KEY2 no longer participates in env checks
               or env merging.
  Scenario 3 - economy defaults are tail=50 and multiplier=x2 for DevAgent
               (config getters + built-in fallback) and for the YaAgent
               preset bundle.
  Scenario 4 - the economy slider spans the new scale (4..100) and clamps
               legacy out-of-range values.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent


def _json(path: str) -> dict:
    """Load one repository JSON file relative to the project root."""
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _extra_field_keys(svc: dict) -> list:
    """Return the keys of a service's extra_fields entries."""
    return [f.get("key") for f in (svc.get("extra_fields") or []) if isinstance(f, dict)]


# ─── Scenario 1 - YandexAI provider fields ───────────────────────────────────


def test_yandex_extra_fields_expose_only_reasoning_effort():
    """Given the YandexAI provider definitions, when they are loaded, then
    only the reasoning_effort extra field remains and the removed web-search
    plumbing fields are absent from both service copies."""
    for path in ("services/yandex.json", "defaults/services/yandex.json"):
        svc = _json(path)
        assert _extra_field_keys(svc) == ["reasoning_effort"], path
        raw = (ROOT / path).read_text(encoding="utf-8")
        assert "web_search_context_size" not in raw, path
        assert "web_search_allowed_domains" not in raw, path

    # The runtime-effective service (defaults win) must agree as well.
    from core.services import _cached_services, discover_services
    _cached_services.cache_clear()
    effective = discover_services()["YandexAI"]
    assert _extra_field_keys(effective) == ["reasoning_effort"]


# ─── Scenario 2 - GigaChat env fallback limited to one key ───────────────────


def test_gigachat_env_fallback_limited_to_single_key(monkeypatch):
    """Given the GigaChat service declares env_key_fields=[config_key], when
    SAGAAI_GIGACHAT_KEY2 is set, then it is invisible to env checks and env
    merging; only SAGAAI_GIGACHAT_KEY fills the API key."""
    from core.config import (
        env_key_fields_for_service,
        is_env_key_set_for_service,
        env_key_name_for_service,
        _merge_env_keys,
    )

    monkeypatch.delenv("SAGAAI_GIGACHAT_KEY", raising=False)
    monkeypatch.delenv("SAGAAI_GIGACHAT_KEY2", raising=False)

    assert env_key_fields_for_service("GigaChat") == ("config_key",)
    assert is_env_key_set_for_service("GigaChat", "config_key") is False

    # KEY2 is set but must be completely invisible to the env machinery.
    monkeypatch.setenv("SAGAAI_GIGACHAT_KEY2", "scoped-key")
    assert is_env_key_set_for_service("GigaChat", "config_key2") is False

    cfg = {"GIGACHAT_API_KEY": "", "GIGACHAT_SCOPE": ""}
    _merge_env_keys(cfg)
    assert cfg["GIGACHAT_API_KEY"] == ""
    assert cfg["GIGACHAT_SCOPE"] == ""

    # Only the declared KEY variable fills the API key.
    monkeypatch.setenv("SAGAAI_GIGACHAT_KEY", "auth-key")
    assert is_env_key_set_for_service("GigaChat", "config_key") is True
    assert env_key_name_for_service("GigaChat", "config_key") == "SAGAAI_GIGACHAT_KEY"

    _merge_env_keys(cfg)
    assert cfg["GIGACHAT_API_KEY"] == "auth-key"
    assert cfg["GIGACHAT_SCOPE"] == ""


# ─── Scenario 3 - economy defaults 50 / x2 ────────────────────────────────────


def test_economy_defaults_tail_50_multiplier_x2():
    """Given the platform defaults, when they are read, then DevAgent and the
    YaAgent preset both default to tail=50 and multiplier=x2 with the
    cache-friendly mode enabled."""
    from core.config import (
        get_default_economy_tail_messages,
        get_default_economy_cache_multiplier,
        get_default_economy_cache_enabled,
        _DEVAGENT_FALLBACK_DEFAULTS,
    )

    assert get_default_economy_tail_messages() == 50
    assert get_default_economy_cache_multiplier() == 2
    assert get_default_economy_cache_enabled() is True
    assert int(_DEVAGENT_FALLBACK_DEFAULTS["economy_tail_messages"]) == 50
    assert int(_DEVAGENT_FALLBACK_DEFAULTS["economy_cache_multiplier"]) == 2

    ya_cfg = _json("defaults/orchestrators/ya_agent/orchestrator.json")["config"]
    assert ya_cfg["economy_tail_messages"] == 50
    assert ya_cfg["economy_cache_multiplier"] == 2
    assert ya_cfg["economy_cache_enabled"] is True


# ─── Scenario 4 - economy slider scale 4..100 ─────────────────────────────────

@pytest.fixture()
def ui_env():
    """Fresh ui.* modules under the Streamlit mock (same pattern as
    tests/test_ui_tooltips.py)."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st:
        st.session_state.update(ui_lang="English")
        yield st


def _slider_kwargs(st, slug):
    """Return the recorded kwargs of the economy tail slider."""
    sliders = [
        kwargs
        for name, _args, kwargs in st.calls
        if name == "slider" and kwargs.get("key") == "orch_economy_tail_" + slug
    ]
    assert len(sliders) == 1, "economy tail slider was not rendered"
    return sliders[0]


def test_economy_slider_spans_4_to_100_and_clamps_legacy_values(ui_env, monkeypatch):
    """Given the economy settings section, when a legacy config stores an
    out-of-range tail (150), then the slider renders with min=4/max=100 and
    a clamped value of 100; a normal stored value passes through unchanged."""
    import ui.pages.orchestrator as orch_mod

    slug = orch_mod.DEVAGENT_SLUG

    def _render(tail_stored):
        ui_env.calls.clear()
        monkeypatch.setattr(
            orch_mod,
            "get_orchestrator",
            lambda s: {"slug": slug, "name": "DevAgent", "config": {}},
        )
        monkeypatch.setattr(
            orch_mod,
            "get_economy_config",
            lambda s: {"tail_messages": tail_stored, "cache_enabled": True,
                       "cache_multiplier": 2},
        )
        monkeypatch.setattr(orch_mod, "get_economy_tail_messages", lambda s: 50)
        orch_mod._render_economy_settings(slug, "English")
        return _slider_kwargs(ui_env, slug)

    legacy = _render(150)
    assert legacy["min_value"] == 4
    assert legacy["max_value"] == 100
    assert legacy["value"] == 100

    normal = _render(50)
    assert normal["value"] == 50
