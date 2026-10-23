# -*- coding: utf-8 -*-
"""tests/scenarios/test_devagent_defaults_scenario.py - user-level scenarios
for the shipped DevAgent defaults, the Selenium checkbox and the placement of
save confirmations.

Scenarios (given -> when -> then), walking the same public entry points the
platform and the UI use:

  Scenario 1 - a fresh install seeds DevAgent with DeepSeek/deepseek-flash on
               every role (main with xhigh reasoning, image analysis, web
               search with temperature 0.4 and three tool calls).
  Scenario 2 - an existing DevAgent keeps user-chosen models: the backfill
               fills only the fields that were never set.
  Scenario 3 - the Models tab hides the page-fetch model selectors while the
               Selenium checkbox is on, and keeps the stored pair intact.
  Scenario 4 - clearing the Selenium checkbox brings the selectors back.
  Scenario 5 - a save confirmation is painted below the page body, not above
               the page title.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture
def isolated_env():
    """Temporary DATA_DIR with a clean database and fresh module state."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_defaults_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)

    import core.paths

    saved = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR"):
        saved[attr] = getattr(core.paths, attr, None)
    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    from storage.db import reset_engine, reset_devagent_engine

    reset_engine()
    reset_devagent_engine()

    from core.config import reload_devagent_defaults

    reload_devagent_defaults()

    yield tmp

    reset_engine()
    reset_devagent_engine()
    if old_data_dir:
        os.environ["SAGAAI_DATA_DIR"] = old_data_dir
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    for attr, value in saved.items():
        if value is not None:
            setattr(core.paths, attr, value)
    reload_devagent_defaults()
    shutil.rmtree(tmp, ignore_errors=True)


DEEPSEEK = {
    "name": "DeepSeek",
    "config_key": "DEEPSEEK_API_KEY",
    "base_url": "https://api.deepseek.com/responses",
    "fetch_base_url": "https://api.deepseek.com/v1/tools/fetch",
    "models": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}],
    "vision_models": [{"id": "deepseek-flash"}],
    "fetch_models": [{"id": "deepseek-flash"}],
    "tools_options": [{"key": "web_search"}],
    "temp_min": 0, "temp_max": 2, "temp_step": 0.1,
}


def _bootstrap():
    """Seed the built-in DevAgent the way the platform does on first boot."""
    from core.orchestrators import ensure_builtin_orchestrators

    return ensure_builtin_orchestrators()


# ─── Scenario 1: shipped defaults ────────────────────────────────────────────

def test_scenario_1_fresh_install_uses_deepseek_flash_everywhere(isolated_env):
    """GIVEN a fresh install, WHEN DevAgent is seeded, THEN all roles match."""
    _bootstrap()

    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator

    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]

    # Main model: DeepSeek flash with the deepest reasoning level.
    assert cfg["strong_service"] == "DeepSeek"
    assert cfg["strong_model"] == "deepseek-flash"
    assert cfg["strong_reasoning_effort"] == "xhigh"

    # Image analysis uses the same multimodal model.
    assert cfg["vision_service"] == "DeepSeek"
    assert cfg["vision_model"] == "deepseek-flash"

    # Web search: same model, temperature 0.4, three tool calls, high effort.
    assert cfg["search_service"] == "DeepSeek"
    assert cfg["search_model"] == "deepseek-flash"
    assert cfg["search_temperature"] == pytest.approx(0.4)
    assert cfg["search_max_tool_calls"] == 3
    assert cfg["search_reasoning_effort"] == "high"

    # Image generation stays optional.
    assert cfg["image_service"] == ""
    assert cfg["image_model"] == ""


def test_scenario_1b_search_config_reaches_the_search_agent(isolated_env):
    """GIVEN the seeded defaults, WHEN the search agent is built, THEN 0.4/3."""
    _bootstrap()

    from core.orchestrators import DEVAGENT_SLUG, get_web_search_config

    with patch("core.orchestrators._get_services", return_value={"DeepSeek": DEEPSEEK}):
        search = get_web_search_config(DEVAGENT_SLUG)

    assert search["service"] == "DeepSeek"
    assert search["model"] == "deepseek-flash"
    assert search["temperature"] == pytest.approx(0.4)
    assert search["max_tool_calls"] == 3
    assert search["reasoning_effort"] == "high"


# ─── Scenario 2: backfill never overwrites a user choice ─────────────────────

def test_scenario_2_backfill_keeps_user_chosen_models(isolated_env):
    """GIVEN a stored choice, WHEN the backfill runs, THEN it is preserved."""
    _bootstrap()

    from core.orchestrators import (
        DEVAGENT_SLUG, ensure_builtin_orchestrators, get_orchestrator,
        save_orchestrator,
    )

    cfg = dict(get_orchestrator(DEVAGENT_SLUG)["config"])
    cfg["strong_model"] = "deepseek-v4-pro"
    cfg["search_model"] = "deepseek-v4-pro"
    assert save_orchestrator(DEVAGENT_SLUG, config=cfg) is True

    ensure_builtin_orchestrators()
    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]
    assert cfg["strong_model"] == "deepseek-v4-pro"
    assert cfg["search_model"] == "deepseek-v4-pro"


def test_scenario_2b_backfill_fills_fields_that_were_never_set(isolated_env):
    """GIVEN a legacy config without a vision model, WHEN backfilled, THEN set."""
    _bootstrap()

    from core.orchestrators import (
        DEVAGENT_SLUG, ensure_builtin_orchestrators, get_orchestrator,
        save_orchestrator,
    )

    cfg = dict(get_orchestrator(DEVAGENT_SLUG)["config"])
    cfg["vision_service"] = ""
    cfg["vision_model"] = ""
    assert save_orchestrator(DEVAGENT_SLUG, config=cfg) is True

    ensure_builtin_orchestrators()
    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]
    assert cfg["vision_service"] == "DeepSeek"
    assert cfg["vision_model"] == "deepseek-flash"


# ─── Scenarios 3-4: the Selenium checkbox hides the fetch selectors ─────────

def _render_models(st_mock, slug, cfg, services):
    """Render the Models tab once under the mock and return the widget keys."""
    import ui.pages.orchestrator as orch_mod

    orch_mod.st = st_mock
    with patch.object(orch_mod, "get_orchestrator",
                      return_value={"config": dict(cfg), "prompt_text": ""}), \
         patch.object(orch_mod, "get_services", return_value=services), \
         patch.object(orch_mod, "t", side_effect=lambda key, *a, **k: key), \
         patch.object(orch_mod, "save_orchestrator", return_value=True) as save:
        try:
            orch_mod._render_models_settings(slug, "English")
        except StopRerun:
            pass
    return save


def _keys(st_mock, kind):
    """Widget keys recorded for one Streamlit call kind."""
    return [
        kwargs.get("key")
        for name, _args, kwargs in st_mock.calls
        if name == kind
    ]


def test_scenario_3_selenium_hides_the_fetch_model_selectors(isolated_env):
    """GIVEN Selenium is on, WHEN the Models tab renders, THEN no fetch pair."""
    _bootstrap()
    slug = "dev_agent"

    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)

    with install_streamlit_mock() as st_mock:
        st_mock.session_state.update(ui_lang="English")
        cfg = {"strong_service": "DeepSeek", "strong_model": "deepseek-flash",
               "fetch_selenium": True,
               "fetch_service": "DeepSeek", "fetch_model": "deepseek-flash"}
        save = _render_models(st_mock, slug, cfg, {"DeepSeek": DEEPSEEK})

        boxes = _keys(st_mock, "selectbox")
        assert "orch_set_fetch_svc_%s" % slug not in boxes
        assert "orch_set_fetch_mdl_%s" % slug not in boxes
        # The main-model selector is still rendered.
        assert "orch_set_strong_mdl_%s" % slug in boxes
        save.assert_not_called()

        # Saving keeps the stored pair untouched for when Selenium is off.
        st_mock.click("orch_save_models_%s" % slug)
        save = _render_models(st_mock, slug, cfg, {"DeepSeek": DEEPSEEK})

    saved = save.call_args.kwargs["config"]
    assert saved["fetch_service"] == "DeepSeek"
    assert saved["fetch_model"] == "deepseek-flash"
    assert saved["fetch_selenium"] is True


def test_scenario_4_clearing_selenium_restores_the_selectors(isolated_env):
    """GIVEN Selenium is off, WHEN the Models tab renders, THEN the pair shows."""
    _bootstrap()
    slug = "dev_agent"

    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)

    with install_streamlit_mock() as st_mock:
        st_mock.session_state.update(ui_lang="English")
        cfg = {"strong_service": "DeepSeek", "strong_model": "deepseek-flash",
               "fetch_selenium": False,
               "fetch_service": "DeepSeek", "fetch_model": "deepseek-flash"}
        _render_models(st_mock, slug, cfg, {"DeepSeek": DEEPSEEK})

        boxes = _keys(st_mock, "selectbox")
        assert "orch_set_fetch_svc_%s" % slug in boxes
        assert "orch_set_fetch_mdl_%s" % slug in boxes


# ─── Scenario 5: confirmations are painted below the page body ──────────────

def test_scenario_5_save_confirmation_is_painted_below_the_page(isolated_env):
    """GIVEN a queued confirmation, WHEN a page renders, THEN it comes last."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)

    with install_streamlit_mock() as st_mock:
        st_mock.session_state.update(ui_lang="English", current_page="orchestrators")

        import ui.app as app_mod
        from ui.components.flash import flash_success

        rendered = []

        def _fake_page():
            """Stand-in page body: records that it painted its own content."""
            rendered.append("body")
            st_mock.markdown("PAGE BODY")

        flash_success("saved_ok")
        with patch.object(app_mod, "page_orchestrators", side_effect=_fake_page), \
             patch.object(app_mod, "check_dependencies",
                          return_value={"ok": True, "missing": [], "outdated": []}):
            try:
                app_mod.main()
            except StopRerun:
                pass

        # Page body painted first, the confirmation afterwards.
        assert rendered == ["body"]
        success_calls = [
            index for index, (name, _args, _kwargs) in enumerate(st_mock.calls)
            if name == "success"
        ]
        body_calls = [
            index for index, (name, args, _kwargs) in enumerate(st_mock.calls)
            if name == "markdown" and args and args[0] == "PAGE BODY"
        ]
        assert body_calls, "page body was not rendered"
        assert success_calls, "confirmation was not painted"
        assert success_calls[0] > body_calls[0], (
            "the confirmation must be painted after the page body"
        )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
