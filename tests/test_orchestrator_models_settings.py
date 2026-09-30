# -*- coding: utf-8 -*-
"""tests/test_orchestrator_models_settings.py - optional image-model settings.

Covers the single-model orchestrator settings tab:
  * the vision (recognition) and image (generation) selectors start on the
    "not selected" option when nothing is assigned;
  * a saved provider that disappeared raises the standard warning;
  * saving persists the assigned vision/image pair and drops legacy weak_* keys.

The page is rendered under the shared Streamlit mock; core.i18n.t is replaced
with an identity lambda so the assertions can rely on the i18n keys.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def ui_env():
    """Fresh ui.* modules under the Streamlit mock."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st_mock:
        st_mock.session_state.update(ui_lang="English")
        yield st_mock


def _invoke(fn):
    try:
        fn()
    except StopRerun:
        pass


def _selectboxes(st_mock):
    """Map widget key -> recorded kwargs for every rendered selectbox."""
    return {
        kwargs.get("key"): kwargs
        for name, _, kwargs in st_mock.calls
        if name == "selectbox"
    }


def test_optional_image_model_selectors(ui_env, monkeypatch):
    """Empty choice by default; a gone provider warns; save persists the pair."""
    import ui.pages.orchestrator as orch_mod
    from ui.pages.orchestrator import _render_models_settings

    orch_mod.st = ui_env
    monkeypatch.setattr(orch_mod, "t", lambda key, *a, **k: key)

    services = {
        "Svc": {
            "auth_type": "bearer",
            "base_url": "https://mock",
            "config_key": "k",
            "models": [{"id": "m1"}],
            "temp_min": 0, "temp_max": 1, "temp_step": 0.1,
            "tools_options": [{"key": "web_search"}],
            "max_tokens_default": 65536,
            "vision_models": [{"id": "vm1"}],
            "image_models": [{"id": "im1"}],
        }
    }
    orch = {
        "config": {
            "strong_service": "Svc", "strong_model": "m1",
            # Legacy weak_* leftovers plus a gone vision provider.
            "weak_service": "Svc", "weak_model": "m1",
            "vision_service": "Gone", "vision_model": "vm1",
            "search_service": "Svc", "search_model": "m1",
            "web_search_prompt": "p",
        },
        "prompt_text": "",
    }

    with patch.object(orch_mod, "get_orchestrator", return_value=orch), \
         patch.object(orch_mod, "get_services", return_value=services), \
         patch.object(orch_mod, "save_orchestrator", return_value=True) as mock_save:
        _invoke(lambda: _render_models_settings("myorch", "English"))

        assert not ui_env.errors
        assert any("orch_service_unavailable" in str(w) for w in ui_env.warnings)
        boxes = _selectboxes(ui_env)
        # The removed weak widgets must never render again.
        assert all("orch_set_weak" not in str(key) for key in boxes)
        # Both optional selectors offer the "not selected" option first.
        assert boxes["orch_set_vision_svc_myorch"]["options"] == ["orch_model_unset_option", "Svc"]
        assert boxes["orch_set_image_svc_myorch"]["options"] == ["orch_model_unset_option", "Svc"]
        # Nothing assigned - no model selectbox is rendered for either pair.
        assert "orch_set_vision_mdl_myorch" not in boxes
        assert "orch_set_image_mdl_myorch" not in boxes

        # Assign the vision pair and save; legacy weak_* keys must be dropped.
        ui_env._selectbox_returns["orch_set_vision_svc_myorch"] = "Svc"
        ui_env.click("orch_save_models_myorch")
        _invoke(lambda: _render_models_settings("myorch", "English"))

        boxes = _selectboxes(ui_env)
        assert boxes["orch_set_vision_mdl_myorch"]["options"] == ["vm1"]

    saved_cfg = mock_save.call_args.kwargs["config"]
    assert saved_cfg["vision_service"] == "Svc"
    assert saved_cfg["vision_model"] == "vm1"
    assert saved_cfg["image_service"] == ""
    assert saved_cfg["image_model"] == ""
    for legacy_key in ("weak_service", "weak_model", "weak_temperature",
                       "weak_max_tokens", "weak_reasoning_effort"):
        assert legacy_key not in saved_cfg
