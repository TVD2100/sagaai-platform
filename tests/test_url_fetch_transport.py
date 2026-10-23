# -*- coding: utf-8 -*-
"""tests/test_url_fetch_transport.py - PDF download, Selenium and SSRF.

Covers the transport helpers that back the three branches of the
``url_fetch`` tool: a direct PDF download rendered to Markdown, a browser
render whose text is converted to Markdown by the main model, and the
server-side request forgery guard shared by both.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.api_errors import ProviderHTTPError, ProviderResponseError
from core.url_fetch import (
    MAX_DOWNLOAD_BYTES,
    SeleniumUnavailableError,
    download_bytes,
    extract_pdf_markdown,
    is_pdf_response,
    looks_like_pdf_url,
    selenium_page_text,
    validate_fetch_url,
)


# ─── URL classification and the SSRF guard ─────────────────────────────────


def test_pdf_url_is_detected_by_path_extension():
    """A .pdf path is a file, a page path and query are not."""
    assert looks_like_pdf_url("https://example.com/reports/2026.PDF") is True
    assert looks_like_pdf_url("https://example.com/a/b.pdf?token=1") is True
    assert looks_like_pdf_url("https://example.com/a/b.html") is False
    assert looks_like_pdf_url("https://example.com/a/pdf") is False


def test_pdf_response_is_detected_by_content_type():
    """The content type decides for a redirect ending on a PDF."""
    assert is_pdf_response("application/pdf") is True
    assert is_pdf_response("application/pdf; charset=binary") is True
    assert is_pdf_response("text/html; charset=utf-8") is False
    assert is_pdf_response("") is False


@pytest.mark.parametrize("url", [
    "http://localhost/admin",
    "http://127.0.0.1:8000/secret",
    "http://127.1.2.3/",
    "http://10.0.0.1/internal",
    "http://192.168.1.10/router",
    "http://172.16.0.5/",
    "http://169.254.169.254/latest/meta-data/",
    "http://printer.local/status",
    "http://service.internal/",
    "http://[::1]/admin",
    "http://[fe80::1]/",
    "http://0.0.0.0/",
])
def test_private_targets_are_rejected(url):
    """Local and private addresses never pass URL validation."""
    with pytest.raises(ValueError, match="private network"):
        validate_fetch_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/page",
    "https://8.8.8.8/resolve",
    "http://docs.python.org/3/library/index.html",
])
def test_public_targets_pass_validation(url):
    """Public hosts and addresses stay allowed."""
    assert validate_fetch_url(url) == url


# ─── PDF rendering ─────────────────────────────────────────────────────────


def _fake_pdf(reply):
    """Build a requests.get stand-in streaming *reply* by chunks."""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "application/pdf",
                    "Content-Length": str(len(reply))}
    resp.url = "https://example.com/report.pdf"
    resp.iter_content.return_value = [reply[i:i + 8]
                                      for i in range(0, len(reply), 8)]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_download_returns_bytes_and_content_type():
    """A direct download returns the body and the announced type."""
    payload = b"%PDF-1.4 small body"
    with patch("core.url_fetch.requests.get",
               return_value=_fake_pdf(payload)) as get:
        got = download_bytes("https://example.com/report.pdf")
    get.assert_called_once()
    assert got["content"] == payload
    assert got["content_type"] == "application/pdf"
    assert got["final_url"] == "https://example.com/report.pdf"


def test_download_rejects_oversized_body_while_streaming():
    """The streaming cap fires even without a Content-Length header."""
    resp = _fake_pdf(b"x" * 10)
    resp.headers = {"Content-Type": "application/pdf"}
    with patch("core.url_fetch.requests.get", return_value=resp):
        with pytest.raises(ProviderResponseError):
            download_bytes("https://example.com/big.pdf", max_bytes=4)


def test_download_maps_http_errors_to_the_api_contract():
    """A non-200 answer is a provider HTTP error, not a raw requests one."""
    resp = _fake_pdf(b"body")
    resp.status_code = 404
    resp.text = "not found"
    with patch("core.url_fetch.requests.get", return_value=resp):
        with pytest.raises(ProviderHTTPError):
            download_bytes("https://example.com/missing.pdf")


def test_pdf_text_is_rendered_as_markdown_sections():
    """Pages become '### Page N' sections without blank-line noise."""
    page = MagicMock()
    page.extract_text.return_value = "Heading\n\n\n\nBody line\n"
    pdf = MagicMock()
    pdf.pages = [page, page]
    pdf.__enter__ = MagicMock(return_value=pdf)
    pdf.__exit__ = MagicMock(return_value=False)

    module = MagicMock()
    module.open.return_value = pdf
    with patch.dict(sys.modules, {"pdfplumber": module}):
        got = extract_pdf_markdown(b"%PDF-1.4")

    assert got.startswith("### Page 1")
    assert "### Page 2" in got
    assert "Heading\nBody line" in got


def test_scanned_pdf_returns_empty_text():
    """A PDF without a text layer produces no Markdown."""
    page = MagicMock()
    page.extract_text.return_value = ""
    pdf = MagicMock()
    pdf.pages = [page]
    pdf.__enter__ = MagicMock(return_value=pdf)
    pdf.__exit__ = MagicMock(return_value=False)

    module = MagicMock()
    module.open.return_value = pdf
    with patch.dict(sys.modules, {"pdfplumber": module}):
        assert extract_pdf_markdown(b"%PDF-1.4") == ""


# ─── Selenium branch ───────────────────────────────────────────────────────


def _fake_selenium_modules(title="Docs", text="Visible body"):
    """Build real module objects standing in for the selenium package tree.

    Plain ``MagicMock`` wrappers do not work here: ``from selenium import
    webdriver`` would receive an auto-created child mock instead of the
    prepared stand-in, so every level is an explicit module object whose
    attributes are set by hand.

    Returns:
        ``(modules, driver)`` where *modules* maps a dotted module name to
        its stand-in and *driver* is the browser mock.
    """
    driver = MagicMock()
    driver.title = title
    driver.current_url = "https://example.com/docs"
    driver.execute_script.return_value = text

    screen = types.ModuleType("selenium.webdriver.chrome.options")
    screen.Options = MagicMock(return_value=MagicMock())

    chrome = types.ModuleType("selenium.webdriver.chrome")
    chrome.options = screen

    webdriver = types.ModuleType("selenium.webdriver")
    webdriver.chrome = chrome
    webdriver.Chrome = MagicMock(return_value=driver)

    errors = types.ModuleType("selenium.common.exceptions")
    errors.WebDriverException = RuntimeError

    common = types.ModuleType("selenium.common")
    common.exceptions = errors

    package = types.ModuleType("selenium")
    package.webdriver = webdriver
    package.common = common

    modules = {
        "selenium": package,
        "selenium.webdriver": webdriver,
        "selenium.webdriver.chrome": chrome,
        "selenium.webdriver.chrome.options": screen,
        "selenium.common": common,
        "selenium.common.exceptions": errors,
    }
    return modules, driver


def test_selenium_returns_visible_text_and_closes_the_browser():
    """The browser is driven headless, its text is read, and it quits."""
    modules, driver = _fake_selenium_modules()
    with patch.dict(sys.modules, modules):
        got = selenium_page_text("https://example.com/docs",
                                 settle_seconds=0)

    assert got["text"] == "Visible body"
    assert got["title"] == "Docs"
    driver.get.assert_called_once_with("https://example.com/docs")
    driver.quit.assert_called_once()


def test_selenium_missing_package_raises_a_clear_error():
    """Without the package the user gets an install hint, not ImportError."""
    with patch.dict(sys.modules, {"selenium": None}):
        with pytest.raises(SeleniumUnavailableError, match="pip install"):
            selenium_page_text("https://example.com/docs")


def test_selenium_rejects_private_targets_before_starting_a_browser():
    """The SSRF guard runs before any driver is created."""
    modules, driver = _fake_selenium_modules()
    chrome_factory = modules["selenium.webdriver"].Chrome
    with patch.dict(sys.modules, modules):
        with pytest.raises(ValueError, match="private network"):
            selenium_page_text("http://127.0.0.1:8501/")
    chrome_factory.assert_not_called()


def test_download_limit_is_25_megabytes():
    """The shared size limit stays at 25 MB."""
    assert MAX_DOWNLOAD_BYTES == 25 * 1024 * 1024
