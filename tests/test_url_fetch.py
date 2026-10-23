# -*- coding: utf-8 -*-
"""tests/test_url_fetch.py - optional url-fetch model and url_fetch tool.

Covers the whole chain of the optional url-fetch capability:
  * the transport (endpoint resolution, URL validation, provider errors);
  * the ToolExecutor tool (configuration gate, sanitization, size cap);
  * the catalog gate (the tool is offered only with an assigned model);
  * the settings UI (only fetch-capable providers, the saved pair).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.api_errors import (
    ApiKeyMissingError,
    NetworkError,
    ProviderHTTPError,
    ProviderResponseError,
    RequestTimeoutError,
)
from core.prompt_guard import MAX_FETCHED_PAGE_CHARS, sanitize_fetched_page
from core.services import get_fetch_models, svc_supports_fetch
from core.url_fetch import (
    MAX_FETCH_URL_CHARS,
    fetch_endpoint,
    fetch_page_content,
    validate_fetch_url,
)

DEEPSEEK = {
    "name": "DeepSeek",
    "config_key": "DEEPSEEK_API_KEY",
    "base_url": "https://api.deepseek.com/responses",
    "fetch_base_url": "https://api.deepseek.com/v1/tools/fetch",
    "fetch_models": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}],
}


def _response(status=200, payload=None, text=""):
    """Build a minimal requests.Response stand-in."""
    r = MagicMock()
    r.status_code = status
    r.text = text
    if payload is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = payload
    return r


# ─── service catalog ────────────────────────────────────────────────────────


def test_fetch_catalog_helpers():
    """Only profiles that declare fetch_models support page fetching."""
    assert [m["id"] for m in get_fetch_models(DEEPSEEK)] == [
        "deepseek-flash", "deepseek-v4-pro",
    ]
    assert get_fetch_models({"models": [{"id": "m"}]}) == []
    assert get_fetch_models(None) == []
    assert svc_supports_fetch(DEEPSEEK) is True
    assert svc_supports_fetch({"models": [{"id": "m"}]}) is False


def test_bundled_deepseek_profile_declares_fetch():
    """The shipped DeepSeek profile carries the Search/Fetch endpoint."""
    root = Path(__file__).resolve().parent.parent
    for rel in ("defaults/services/deepseek.json", "services/deepseek.json"):
        data = json.loads((root / rel).read_text(encoding="utf-8"))
        assert data["fetch_base_url"] == "https://api.deepseek.com/v1/tools/fetch"
        assert [m["id"] for m in data["fetch_models"]] == [
            "deepseek-flash", "deepseek-v4-pro",
        ]


def test_fetch_endpoint_resolution():
    """An explicit endpoint wins; otherwise it is derived once per version."""
    assert fetch_endpoint(DEEPSEEK) == "https://api.deepseek.com/v1/tools/fetch"
    derived = fetch_endpoint({"base_url": "https://api.example.com/responses"})
    assert derived == "https://api.example.com/v1/tools/fetch"
    versioned = fetch_endpoint({"base_url": "https://api.example.com/v1/chat/completions"})
    assert versioned == "https://api.example.com/v1/tools/fetch"
    assert fetch_endpoint({}) == ""
    assert fetch_endpoint(None) == ""


# ─── URL validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "ftp://example.com/page",
    "file:///etc/passwd",
    "https://",
    "https://user:secret@example.com/page",
    "http://example.com/a b",
    "http://example.com/a\x7fb",
    "x" * (MAX_FETCH_URL_CHARS + 1),
])
def test_invalid_urls_are_rejected(bad):
    """Unsafe or malformed addresses never reach the provider."""
    with pytest.raises(ValueError):
        validate_fetch_url(bad)


def test_valid_url_is_stripped():
    """Surrounding whitespace is removed, the address is kept verbatim."""
    assert validate_fetch_url("  https://example.com/a?b=1#c ") == \
        "https://example.com/a?b=1#c"


# ─── transport ──────────────────────────────────────────────────────────────


def test_fetch_posts_url_and_returns_page():
    """The request carries the bearer key and only the target URL."""
    response = _response(200, {"title": "Example", "content": "# Body"})
    with patch("core.url_fetch.requests.post", return_value=response) as post, \
         patch("core.url_fetch.get_services", return_value={"DeepSeek": DEEPSEEK}):
        page = fetch_page_content(
            "https://example.com/article", svc_name="DeepSeek",
            cfg={"DEEPSEEK_API_KEY": "  secret-key  "},
        )

    assert page == {"title": "Example", "content": "# Body"}
    args, kwargs = post.call_args
    assert args[0] == "https://api.deepseek.com/v1/tools/fetch"
    assert kwargs["json"] == {"url": "https://example.com/article"}
    assert kwargs["headers"]["Authorization"] == "Bearer secret-key"
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_fetch_accepts_nested_envelope():
    """Gateways that wrap the answer in a data envelope still work."""
    response = _response(200, {"data": {"title": "T", "markdown": "M"}})
    with patch("core.url_fetch.requests.post", return_value=response), \
         patch("core.url_fetch.get_services", return_value={"DeepSeek": DEEPSEEK}):
        page = fetch_page_content(
            "https://example.com", svc_name="DeepSeek",
            cfg={"DEEPSEEK_API_KEY": "k"},
        )
    assert page == {"title": "T", "content": "M"}


def test_fetch_requires_api_key():
    """A provider without a key fails before any HTTP call."""
    with patch("core.url_fetch.get_services", return_value={"DeepSeek": DEEPSEEK}), \
         patch("core.url_fetch.requests.post") as post:
        with pytest.raises(ApiKeyMissingError):
            fetch_page_content("https://example.com", svc_name="DeepSeek", cfg={})
    post.assert_not_called()


def test_fetch_rejects_unknown_service():
    """An unknown provider name is reported, not silently ignored."""
    with patch("core.url_fetch.get_services", return_value={}):
        with pytest.raises(Exception):
            fetch_page_content("https://example.com", svc_name="Nope", cfg={})


def test_fetch_maps_transport_failures():
    """Timeouts, network errors and HTTP statuses use the APIError contract."""
    import requests as _requests

    with patch("core.url_fetch.get_services", return_value={"DeepSeek": DEEPSEEK}):
        with patch("core.url_fetch.requests.post",
                   side_effect=_requests.exceptions.Timeout()):
            with pytest.raises(RequestTimeoutError):
                fetch_page_content("https://example.com", svc_name="DeepSeek",
                                   cfg={"DEEPSEEK_API_KEY": "k"})
        with patch("core.url_fetch.requests.post",
                   side_effect=_requests.exceptions.ConnectionError("boom")):
            with pytest.raises(NetworkError):
                fetch_page_content("https://example.com", svc_name="DeepSeek",
                                   cfg={"DEEPSEEK_API_KEY": "k"})
        with patch("core.url_fetch.requests.post",
                   return_value=_response(403, {"error": "blocked"})):
            with pytest.raises(ProviderHTTPError):
                fetch_page_content("https://example.com", svc_name="DeepSeek",
                                   cfg={"DEEPSEEK_API_KEY": "k"})
        with patch("core.url_fetch.requests.post", return_value=_response(200, None)):
            with pytest.raises(ProviderResponseError):
                fetch_page_content("https://example.com", svc_name="DeepSeek",
                                   cfg={"DEEPSEEK_API_KEY": "k"})
        with patch("core.url_fetch.requests.post",
                   return_value=_response(200, {"title": "T"})):
            with pytest.raises(ProviderResponseError):
                fetch_page_content("https://example.com", svc_name="DeepSeek",
                                   cfg={"DEEPSEEK_API_KEY": "k"})


def test_sanitize_fetched_page_labels_and_caps():
    """Page text is labelled untrusted and truncated to the cap."""
    labelled = sanitize_fetched_page("  # Title\n\n\n\nBody  ")
    assert labelled.startswith("[DATA_FROM_URL_FETCH]")
    assert "Body" in labelled
    assert "\n\n\n" not in labelled
    long_text = sanitize_fetched_page("a" * (MAX_FETCHED_PAGE_CHARS + 500))
    assert "[truncated]" in long_text
    assert len(long_text) < MAX_FETCHED_PAGE_CHARS + 200
    assert sanitize_fetched_page("") == ""


# ─── tool ───────────────────────────────────────────────────────────────────


def _executor(config=None):
    from dev_agent.tool_executor import ToolExecutor
    ex = ToolExecutor()
    ex._url_fetch_config = config
    return ex


def test_tool_requires_fetch_model():
    """Without an assigned model the tool refuses before touching the net."""
    ex = _executor(None)
    with patch("core.url_fetch.requests.post") as post:
        result = ex.url_fetch("https://example.com")
    assert result["ok"] is False
    assert "not configured" in result["error"]
    post.assert_not_called()


def test_tool_validates_url_before_fetching():
    """A malformed address produces a fixable error with a suggestion."""
    ex = _executor({"service": "DeepSeek", "model": "deepseek-flash"})
    result = ex.url_fetch("file:///etc/passwd")
    assert result["ok"] is False
    assert "Invalid page URL" in result["error"]
    assert "suggestion" in result


def test_tool_returns_labelled_page():
    """A successful fetch returns the labelled body with title and URL."""
    ex = _executor({"service": "DeepSeek", "model": "deepseek-flash"})
    with patch("core.url_fetch.fetch_page_content",
               return_value={"title": "Doc", "content": "Page body"}), \
         patch("core.url_fetch.get_services", return_value={"DeepSeek": DEEPSEEK}):
        result = ex.url_fetch("https://example.com/doc")
    assert result["ok"] is True
    assert result["title"] == "Doc"
    assert result["url"] == "https://example.com/doc"
    assert result["text"].startswith("Title: Doc\nURL: https://example.com/doc")
    assert "[DATA_FROM_URL_FETCH]" in result["text"]


def test_tool_reports_provider_error():
    """Provider failures keep the actionable prefix in the error message."""
    ex = _executor({"service": "DeepSeek", "model": "deepseek-flash"})
    with patch("core.url_fetch.fetch_page_content",
               side_effect=ProviderHTTPError(429, "slow down", service="DeepSeek")):
        result = ex.url_fetch("https://example.com")
    assert result["ok"] is False
    assert result["error"].startswith("Page fetch failed:")


def test_tool_runs_a_pdf_url_through_the_local_extractor():
    """A .pdf link downloads the file and renders its text locally."""
    ex = _executor({"service": "DeepSeek", "model": "deepseek-flash"})
    with patch("core.url_fetch.download_bytes",
               return_value={"content": b"%PDF-1.4",
                             "content_type": "application/pdf",
                             "final_url": "https://example.com/a.pdf"}) as dl, \
         patch("core.url_fetch.extract_pdf_markdown",
               return_value="### Page 1\n\nContract text") as extract, \
         patch("core.url_fetch.fetch_page_content") as provider:
        result = ex.url_fetch("https://example.com/a.pdf")
    dl.assert_called_once()
    extract.assert_called_once_with(b"%PDF-1.4")
    provider.assert_not_called()
    assert result["ok"] is True
    assert "Source: PDF" in result["text"]
    assert "Contract text" in result["text"]


def test_tool_uses_selenium_and_the_model_for_markdown():
    """With Selenium on, the browser text goes through the main model."""
    ex = _executor({"selenium": True, "llm_service": "DeepSeek",
                    "llm_model": "deepseek-v4-pro"})
    page = {"title": "Docs", "text": "Heading\nBody",
            "final_url": "https://example.com/docs"}
    with patch("core.url_fetch.selenium_page_text", return_value=page), \
         patch("dev_agent.tool_executor.send_request",
               return_value="# Heading\n\nBody") as llm:
        result = ex.url_fetch("https://example.com/docs")
    llm.assert_called_once()
    assert "# Heading" in result["text"]
    assert "Source: Selenium browser render" in result["text"]


def test_tool_keeps_plain_text_when_the_model_fails():
    """A failed Markdown conversion must not lose the page text."""
    ex = _executor({"selenium": True, "llm_service": "DeepSeek",
                    "llm_model": "deepseek-v4-pro"})
    page = {"title": "Docs", "text": "Raw visible text", "final_url": ""}
    with patch("core.url_fetch.selenium_page_text", return_value=page), \
         patch("dev_agent.tool_executor.send_request",
               side_effect=RuntimeError("provider down")):
        result = ex.url_fetch("https://example.com/docs")
    assert result["ok"] is True
    assert "Raw visible text" in result["text"]


def test_tool_reports_missing_selenium_dependency():
    """A missing Selenium install surfaces as a fixable error."""
    from core.url_fetch import SeleniumUnavailableError
    ex = _executor({"selenium": True})
    with patch("core.url_fetch.selenium_page_text",
               side_effect=SeleniumUnavailableError("pip install selenium")):
        result = ex.url_fetch("https://example.com/docs")
    assert result["ok"] is False
    assert "pip install selenium" in result["error"]


def test_tool_works_with_selenium_alone():
    """Selenium alone enables the tool, without a provider fetch pair."""
    ex = _executor({"selenium": True})
    page = {"title": "", "text": "Body", "final_url": ""}
    with patch("core.url_fetch.selenium_page_text", return_value=page), \
         patch("dev_agent.tool_executor.send_request", return_value="Body"):
        result = ex.url_fetch("https://example.com/x")
    assert result["ok"] is True


def test_tool_runs_the_provider_branch_when_selenium_is_off():
    """Without Selenium the provider API stays the transport."""
    ex = _executor({"service": "DeepSeek", "model": "deepseek-flash",
                    "selenium": False})
    with patch("core.url_fetch.fetch_page_content",
               return_value={"title": "Doc", "content": "Body"}), \
         patch("core.url_fetch.selenium_page_text") as browser:
        result = ex.url_fetch("https://example.com/doc")
    browser.assert_not_called()
    assert "Source: provider fetch API" in result["text"]


def test_tool_arguments_are_restricted():
    """Only the url argument is accepted; extras are rejected structurally."""
    ex = _executor({"service": "DeepSeek", "model": "deepseek-flash"})
    result = ex.dispatch("url_fetch", {"url": "https://example.com", "service": "x"})
    assert result["ok"] is False
    assert result["unknown_args"] == ["service"]


# ─── configuration and catalog gate ─────────────────────────────────────────


def test_get_fetch_config_resolves_only_usable_pairs():
    """A configured pair is valid only while the catalog lists the model."""
    from core.orchestrators import get_fetch_config

    services = {"DeepSeek": DEEPSEEK}
    cases = [
        ({}, {"service": "", "model": "", "configured": False}),
        ({"fetch_service": "DeepSeek"},
         {"service": "", "model": "", "configured": False}),
        ({"fetch_service": "DeepSeek", "fetch_model": "gone"},
         {"service": "", "model": "", "configured": False}),
        ({"fetch_service": "DeepSeek", "fetch_model": "deepseek-flash"},
         {"service": "DeepSeek", "model": "deepseek-flash", "configured": True}),
    ]
    for config, expected in cases:
        with patch("core.orchestrators.get_orchestrator",
                   return_value={"config": config, "prompt_text": ""}), \
             patch("core.orchestrators._get_services", return_value=services):
            got = get_fetch_config("probe")
            assert (got["service"], got["model"], got["configured"]) == (
                expected["service"], expected["model"], expected["configured"],
            )
            assert got["selenium"] is False


def test_catalog_offers_url_fetch_only_with_a_model():
    """The tool appears in the catalog, prompt block and tool list on demand."""
    from core.orchestrator_tools import (
        build_tool_catalog, list_system_tools, render_available_tools_block,
    )

    with patch("core.orchestrators.get_orchestrator",
               return_value={"config": {}, "prompt_text": ""}):
        assert "url_fetch" not in [t["name"] for t in build_tool_catalog(
            "probe", include_connections=False)]
        assert "url_fetch" not in render_available_tools_block("probe")
        assert "url_fetch" not in [t["name"] for t in list_system_tools("probe")]

    enabled = {"config": {"fetch_service": "DeepSeek",
                           "fetch_model": "deepseek-flash"},
               "prompt_text": ""}
    with patch("core.orchestrators.get_orchestrator", return_value=enabled), \
         patch("core.orchestrators._get_services", return_value={"DeepSeek": DEEPSEEK}):
        assert "url_fetch" in [t["name"] for t in build_tool_catalog(
            "probe", include_connections=False)]
        assert "`url_fetch`" in render_available_tools_block("probe")
        assert "url_fetch" in [t["name"] for t in list_system_tools("probe")]


# ─── settings UI ────────────────────────────────────────────────────────────


def test_catalog_offers_url_fetch_with_selenium_alone():
    """Selenium alone opens the gate; a provider pair is not required."""
    from core.orchestrator_tools import (
        build_tool_catalog, is_conditional_tool_enabled, list_system_tools,
        render_available_tools_block,
    )

    with patch("core.orchestrators.get_orchestrator",
               return_value={"config": {}, "prompt_text": ""}):
        assert is_conditional_tool_enabled("url_fetch", "probe") is False

    selenium_only = {"config": {"fetch_selenium": True}, "prompt_text": ""}
    with patch("core.orchestrators.get_orchestrator",
               return_value=selenium_only):
        assert is_conditional_tool_enabled("url_fetch", "probe") is True
        assert "url_fetch" in [t["name"] for t in build_tool_catalog(
            "probe", include_connections=False)]
        assert "`url_fetch`" in render_available_tools_block("probe")
        assert "url_fetch" in [t["name"] for t in list_system_tools("probe")]


def test_models_tab_offers_only_fetch_capable_services():
    """The url-fetch selectors list only providers with a fetch catalog."""
    from tests._st_mock import install_streamlit_mock, StopRerun

    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st_mock:
        st_mock.session_state.update(ui_lang="English")
        import ui.pages.orchestrator as orch_mod
        from ui.pages.orchestrator import _render_models_settings

        orch_mod.st = st_mock
        with patch.object(orch_mod, "t", lambda key, *a, **k: key):
            services = {
                "DeepSeek": dict(DEEPSEEK, models=[{"id": "deepseek-flash"}],
                                 temp_min=0, temp_max=2, temp_step=0.1),
                "Plain": {"models": [{"id": "m1"}], "temp_min": 0,
                          "temp_max": 1, "temp_step": 0.1},
            }
            orch = {"config": {"strong_service": "DeepSeek",
                               "strong_model": "deepseek-flash"},
                    "prompt_text": ""}
            with patch.object(orch_mod, "get_orchestrator", return_value=orch), \
                 patch.object(orch_mod, "get_services", return_value=services), \
                 patch.object(orch_mod, "save_orchestrator", return_value=True) as save:
                try:
                    _render_models_settings("myorch", "English")
                except StopRerun:
                    pass

                boxes = {kwargs.get("key"): kwargs
                         for name, _, kwargs in st_mock.calls
                         if name == "selectbox"}
                assert boxes["orch_set_fetch_svc_myorch"]["options"] == [
                    "orch_model_unset_option", "DeepSeek"]
                assert "orch_set_fetch_mdl_myorch" not in boxes

                # Assign the pair and save it into the config.
                st_mock._selectbox_returns["orch_set_fetch_svc_myorch"] = "DeepSeek"
                st_mock.click("orch_save_models_myorch")
                try:
                    _render_models_settings("myorch", "English")
                except StopRerun:
                    pass
                boxes = {kwargs.get("key"): kwargs
                         for name, _, kwargs in st_mock.calls
                         if name == "selectbox"}
                assert boxes["orch_set_fetch_mdl_myorch"]["options"] == [
                    "deepseek-flash", "deepseek-v4-pro"]

        saved_cfg = save.call_args.kwargs["config"]
        assert saved_cfg["fetch_service"] == "DeepSeek"
        assert saved_cfg["fetch_model"] == "deepseek-flash"


def test_dispatcher_receives_fetch_config():
    """The orchestrator page hands the resolved fetch pair to the executor."""
    from tests._st_mock import install_streamlit_mock

    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st_mock:
        import ui.pages.orchestrator as orch_mod

        expected = {"service": "DeepSeek", "model": "deepseek-flash",
                    "configured": True}
        with patch.object(orch_mod, "get_fetch_config", return_value=expected), \
             patch.object(orch_mod, "get_web_search_config", return_value={}), \
             patch.object(orch_mod, "get_orchestrator",
                          return_value={"config": {}, "prompt_text": ""}), \
             patch.object(orch_mod.UniversalDevAgent, "attach_orchestrator"), \
             patch.object(orch_mod.UniversalDevAgent, "set_thread_id"):
            dispatcher = orch_mod._make_dispatcher("dispatch_probe")
        assert dispatcher.core._url_fetch_config == expected
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
