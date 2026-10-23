# -*- coding: utf-8 -*-
"""tests/scenarios/test_url_fetch_scenario.py - user-level scenario for the
optional url-fetch model and the url_fetch tool.

Scenarios (given -> when -> then), walking the same public entry points the
platform and the UI use:

  Scenario 1 - an employee is created without a url-fetch model. Its system
               prompt has no url_fetch entry, the functions list does not
               offer it and a direct call is refused with a structured error
               (nothing is downloaded).

  Scenario 2 - the user assigns DeepSeek + deepseek-flash on the Models tab
               (save_orchestrator, the same call the UI makes). The prompt
               block, the functions list and the catalog all gain url_fetch;
               a dispatch through the real dispatcher downloads the page and
               hands it back labelled as untrusted data.

  Scenario 3 - the model disappears (the user clears it, or the provider
               drops it from the catalog): the tool vanishes from the prompt
               and from dispatch again, and the configuration accessor
               reports the capability as off.

  Scenario 4 - the Models tab renders the url-fetch selectors with only
               fetch-capable providers and persists the picked pair.
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

DEEPSEEK = {
    "name": "DeepSeek",
    "config_key": "DEEPSEEK_API_KEY",
    "base_url": "https://api.deepseek.com/responses",
    "fetch_base_url": "https://api.deepseek.com/v1/tools/fetch",
    "fetch_models": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}],
    "models": [{"id": "deepseek-flash"}],
    "temp_min": 0, "temp_max": 2, "temp_step": 0.1,
}
PLAIN = {
    "name": "Plain",
    "config_key": "PLAIN_KEY",
    "base_url": "https://plain.example/v1/chat/completions",
    "models": [{"id": "p1"}],
    "temp_min": 0, "temp_max": 1, "temp_step": 0.1,
}


@pytest.fixture
def isolated_env():
    """Temporary DATA_DIR with ui.* modules reloaded under a Streamlit mock."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_fetch_")
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
    shutil.rmtree(tmp, ignore_errors=True)


SLUG = "fetch_employee"


def _make_employee():
    """Create the employee the scenarios work with."""
    from core.orchestrators import create_orchestrator
    assert create_orchestrator(
        SLUG, "Fetch Employee", "Page-fetch scenarios",
        prompt_text="You are a research assistant.",
        config={"strong_service": "DeepSeek",
                "strong_model": "deepseek-flash"},
    )


def _prompt():
    """System prompt the model actually receives (catalog block included)."""
    from core.orchestrators import build_assistant_dicts
    strong, _ = build_assistant_dicts(SLUG)
    return strong["text"]


def _dispatcher():
    """Real dispatcher bound to the employee, as the chat page builds it."""
    from dev_agent.universal_agent import UniversalDevAgent
    from core.orchestrators import get_fetch_config
    dispatcher = UniversalDevAgent()
    dispatcher.attach_orchestrator(SLUG)
    dispatcher.core._url_fetch_config = get_fetch_config(SLUG)
    return dispatcher


def test_scenario_1_no_model_no_tool(isolated_env):
    """GIVEN no url-fetch model, WHEN the employee works, THEN no tool."""
    _make_employee()

    prompt = _prompt()
    assert "url_fetch" not in prompt

    from core.orchestrator_tools import list_system_tools
    assert "url_fetch" not in [t["name"] for t in list_system_tools(SLUG)]

    dispatcher = _dispatcher()
    with patch("core.url_fetch.requests.post") as post:
        result = dispatcher.dispatch("url_fetch", {"url": "https://example.com"})
    assert result["ok"] is False
    assert "not configured" in result["error"]
    post.assert_not_called()


def test_scenario_2_assigning_model_switches_the_tool_on(isolated_env):
    """GIVEN no model, WHEN DeepSeek/deepseek-flash is saved, THEN the tool."""
    _make_employee()
    from core.orchestrators import get_fetch_config, save_orchestrator
    from core.orchestrator_tools import list_system_tools

    with patch("core.orchestrators._get_services",
               return_value={"DeepSeek": DEEPSEEK, "Plain": PLAIN}):
        assert save_orchestrator(
            SLUG, config={"strong_service": "DeepSeek",
                          "strong_model": "deepseek-flash",
                          "fetch_service": "DeepSeek",
                          "fetch_model": "deepseek-flash"},
        )

        _fetch_cfg = get_fetch_config(SLUG)
        assert _fetch_cfg["service"] == "DeepSeek"
        assert _fetch_cfg["model"] == "deepseek-flash"
        assert _fetch_cfg["configured"] is True
        assert _fetch_cfg["selenium"] is False
        assert "`url_fetch`" in _prompt()
        assert "url_fetch" in [t["name"] for t in list_system_tools(SLUG)]

        dispatcher = _dispatcher()
        page = {"title": "Docs", "content": "# Heading\n\nBody text."}
        with patch("core.url_fetch.fetch_page_content", return_value=page) as fetch:
            result = dispatcher.dispatch(
                "url_fetch", {"url": "https://example.com/docs"},
            )
        fetch.assert_called_once()

    assert result["ok"] is True
    assert result["title"] == "Docs"
    assert result["text"].startswith(
        "Title: Docs\nURL: https://example.com/docs")
    assert "[DATA_FROM_URL_FETCH]" in result["text"]
    assert "Body text." in result["text"]


def test_scenario_3_clearing_the_model_switches_the_tool_off(isolated_env):
    """GIVEN a working tool, WHEN the model is cleared, THEN it is gone."""
    _make_employee()
    from core.orchestrators import get_fetch_config, save_orchestrator

    services = {"DeepSeek": DEEPSEEK}
    with patch("core.orchestrators._get_services", return_value=services):
        assert save_orchestrator(
            SLUG, config={"fetch_service": "DeepSeek",
                          "fetch_model": "deepseek-flash"},
        )
        assert get_fetch_config(SLUG)["configured"] is True
        assert "`url_fetch`" in _prompt()

        # The user clears the pair on the Models tab.
        assert save_orchestrator(
            SLUG, config={"fetch_service": "", "fetch_model": ""},
        )
        assert get_fetch_config(SLUG)["configured"] is False
        assert "url_fetch" not in _prompt()

        # A model that the provider no longer lists is treated the same way.
        assert save_orchestrator(
            SLUG, config={"fetch_service": "DeepSeek",
                          "fetch_model": "removed-model"},
        )
        assert get_fetch_config(SLUG)["configured"] is False
        assert "url_fetch" not in _prompt()

    dispatcher = _dispatcher()
    with patch("core.url_fetch.requests.post") as post:
        result = dispatcher.dispatch("url_fetch", {"url": "https://example.com"})
    assert result["ok"] is False
    assert "not configured" in result["error"]
    post.assert_not_called()


def test_scenario_4_models_tab_lists_only_fetch_capable_providers(isolated_env):
    """GIVEN two providers, WHEN the Models tab renders, THEN only one."""
    _make_employee()
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)

    with install_streamlit_mock() as st_mock:
        st_mock.session_state.update(ui_lang="English")
        import ui.pages.orchestrator as orch_mod
        from core.orchestrators import get_orchestrator

        orch_mod.st = st_mock
        services = {"DeepSeek": DEEPSEEK, "Plain": PLAIN}
        with patch.object(orch_mod, "t", lambda key, *a, **k: key), \
             patch.object(orch_mod, "get_services", return_value=services), \
             patch.object(orch_mod, "get_orchestrator",
                          side_effect=get_orchestrator), \
             patch.object(orch_mod, "save_orchestrator",
                          wraps=orch_mod.save_orchestrator) as save:
            try:
                orch_mod._render_models_settings(SLUG, "English")
            except StopRerun:
                pass

            boxes = {kwargs.get("key"): kwargs
                     for name, _, kwargs in st_mock.calls
                     if name == "selectbox"}
            assert boxes["orch_set_fetch_svc_%s" % SLUG]["options"] == [
                "orch_model_unset_option", "DeepSeek"]

            st_mock._selectbox_returns["orch_set_fetch_svc_%s" % SLUG] = "DeepSeek"
            st_mock.click("orch_save_models_%s" % SLUG)
            try:
                orch_mod._render_models_settings(SLUG, "English")
            except StopRerun:
                pass

            boxes = {kwargs.get("key"): kwargs
                     for name, _, kwargs in st_mock.calls
                     if name == "selectbox"}
            assert boxes["orch_set_fetch_mdl_%s" % SLUG]["options"] == [
                "deepseek-flash", "deepseek-v4-pro"]

    saved = save.call_args.kwargs["config"]
    assert saved["fetch_service"] == "DeepSeek"
    assert saved["fetch_model"] == "deepseek-flash"

    # The stored pair now switches the tool on for the employee.
    from core.orchestrators import get_fetch_config
    with patch("core.orchestrators._get_services", return_value=services):
        assert get_fetch_config(SLUG)["configured"] is True


def test_scenario_5_selenium_alone_switches_the_tool_on(isolated_env):
    """GIVEN no fetch model, WHEN Selenium is ticked, THEN the tool works."""
    _make_employee()
    from core.orchestrators import get_fetch_config, save_orchestrator
    from core.orchestrator_tools import list_system_tools

    with patch("core.orchestrators._get_services", return_value={}):
        from core.orchestrators import get_orchestrator
        merged = dict(get_orchestrator(SLUG).get("config") or {})
        merged["fetch_selenium"] = True
        assert save_orchestrator(SLUG, config=merged)
        cfg = get_fetch_config(SLUG)
        assert cfg["configured"] is True
        assert cfg["selenium"] is True
        assert cfg["service"] == "" and cfg["model"] == ""
        assert cfg["llm_service"] == "DeepSeek"
        assert "`url_fetch`" in _prompt()
        assert "url_fetch" in [t["name"] for t in list_system_tools(SLUG)]

    dispatcher = _dispatcher()
    rendered = {"title": "Rendered", "text": "Visible body",
                "final_url": "https://example.com/app"}
    with patch("core.url_fetch.selenium_page_text", return_value=rendered) as run, \
         patch("dev_agent.tool_executor.send_request",
               return_value="# Rendered\n\nVisible body") as llm:
        result = dispatcher.dispatch(
            "url_fetch", {"url": "https://example.com/app"},
        )
    run.assert_called_once_with("https://example.com/app")
    llm.assert_called_once()
    assert result["ok"] is True
    assert "Source: Selenium browser render" in result["text"]
    assert "[DATA_FROM_URL_FETCH]" in result["text"]


def test_scenario_6_pdf_link_is_downloaded_and_extracted(isolated_env):
    """GIVEN a PDF link, WHEN fetched, THEN text becomes Markdown pages."""
    _make_employee()
    from core.orchestrators import save_orchestrator

    with patch("core.orchestrators._get_services", return_value={}):
        from core.orchestrators import get_orchestrator
        merged = dict(get_orchestrator(SLUG).get("config") or {})
        merged["fetch_selenium"] = True
        assert save_orchestrator(SLUG, config=merged)

    dispatcher = _dispatcher()
    payload = {"content": b"%PDF-1.4 body",
               "content_type": "application/pdf",
               "final_url": "https://example.com/report.pdf"}
    with patch("core.url_fetch.download_bytes", return_value=payload) as dl, \
         patch("core.url_fetch.extract_pdf_markdown",
               return_value="### Page 1\n\nAnnual report") as extract, \
         patch("core.url_fetch.selenium_page_text") as browser:
        result = dispatcher.dispatch(
            "url_fetch", {"url": "https://example.com/report.pdf"},
        )
    dl.assert_called_once_with("https://example.com/report.pdf")
    extract.assert_called_once_with(b"%PDF-1.4 body")
    browser.assert_not_called()
    assert result["ok"] is True
    assert "Source: PDF" in result["text"]
    assert "Annual report" in result["text"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
