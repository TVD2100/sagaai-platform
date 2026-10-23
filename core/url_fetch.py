# -*- coding: utf-8 -*-
"""
core.url_fetch - page fetching through a provider's Search/Fetch API.

A provider supports page fetching when its service profile declares a
``fetch_models`` catalog; an orchestrator switches its ``url_fetch`` tool
on by assigning one of those models. The transport lives here so the API
key never leaves the HTTP layer: callers pass a URL and receive the page.

The provider contract is a plain JSON call - ``POST <endpoint>`` with
``{"url": "<page>"}`` and an ``Authorization: Bearer <key>`` header - and
the answer carries ``title`` and ``content`` (page text in Markdown).

No Streamlit imports. Every transport failure is raised as a subclass of
``core.api_errors.APIError`` so callers can render it like any other API
failure.
"""
from __future__ import annotations

import io
import ipaddress
import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from core.api_errors import (
    ApiKeyMissingError,
    NetworkError,
    ProviderHTTPError,
    ProviderResponseError,
    RequestTimeoutError,
    ServiceNotFoundError,
)
from core.api_layer import _VERIFY_TLS, _extract_error_body
from core.config import load_config
from core.services import get_services

#: Hard upper bound of the target URL, matching the provider-side limit.
MAX_FETCH_URL_CHARS = 8192

#: (connect, read) timeout of one page fetch. A single page is much
#: cheaper than a model completion, so the read budget is modest.
FETCH_REQUEST_TIMEOUT = (30, 120)

#: Response fields a provider may use for the page body, by priority.
_CONTENT_FIELDS = ("content", "markdown", "text")

#: Size cap of a directly downloaded file (a PDF or a page), in bytes.
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

#: (connect, read) timeout of a direct download.
DOWNLOAD_TIMEOUT = (30, 300)

#: Browser render budget: page load and script settling phases.
SELENIUM_PAGE_LOAD_TIMEOUT = 30
SELENIUM_SETTLE_SECONDS = 1.5

#: Host names that always resolve to the local machine or a private zone.
_BLOCKED_HOSTS = ("localhost", "localhost.localdomain", "ip6-localhost")

#: Host name suffixes of private, link-local and service-discovery zones.
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home")


class SeleniumUnavailableError(RuntimeError):
    """Selenium or a browser driver is not installed.

    Raised when the Selenium branch of a URL fetch is selected but the
    optional dependency is missing. The message tells the user what to
    install, so the tool can pass it straight to the model.
    """


def _is_private_host(host: str) -> bool:
    """Return True when *host* points at a local or private address.

    Guards direct downloads against server-side request forgery: a target
    URL must never make the platform reach the loopback interface, a
    private network, link-local metadata endpoints or a container-local
    name. Only literal addresses and well-known local names are checked -
    no DNS resolution is performed, so the check is deterministic and
    offline.
    """
    value = str(host or "").strip().lower().strip("[]")
    if not value:
        return True
    if value in _BLOCKED_HOSTS or value.endswith(_BLOCKED_SUFFIXES):
        return True
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        # A regular domain name: nothing local about it.
        return False
    return bool(
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def fetch_endpoint(svc: dict) -> str:
    """Return the url-fetch endpoint declared by a service profile.

    ``fetch_base_url`` wins when present. Otherwise the endpoint is derived
    from ``base_url`` by stripping the completion route and appending the
    sibling ``/v1/tools/fetch`` path. Returns '' when the profile carries
    neither field, which means the provider cannot fetch pages.
    """
    if not isinstance(svc, dict):
        return ""
    explicit = str(svc.get("fetch_base_url") or "").strip()
    if explicit:
        return explicit
    base = str(svc.get("base_url") or "").strip().rstrip("/")
    if not base:
        return ""
    for suffix in ("/responses", "/chat/completions", "/messages"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    # A version segment already present in the base URL is not repeated.
    if base.endswith("/v1"):
        return base + "/tools/fetch"
    return base + "/v1/tools/fetch"


def validate_fetch_url(url: str) -> str:
    """Validate a url-fetch target URL and return it stripped.

    Raises:
        ValueError: the URL is empty, longer than
        :data:`MAX_FETCH_URL_CHARS`, contains whitespace or control
        characters, is not http/https, has no host, or embeds a username
        or password. The message is caller-facing and safe to show to the
        model.
    """
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("the URL is empty")
    if len(raw) > MAX_FETCH_URL_CHARS:
        raise ValueError(
            f"the URL is longer than {MAX_FETCH_URL_CHARS} characters"
        )
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in raw):
        raise ValueError(
            "the URL contains whitespace or control characters"
        )
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are supported")
    if not parsed.hostname:
        raise ValueError("the URL has no host")
    if parsed.username or parsed.password:
        raise ValueError("the URL must not embed a username or password")
    if _is_private_host(parsed.hostname or ""):
        raise ValueError(
            "local and private network addresses are not allowed"
        )
    return raw


def _page_from_payload(data) -> tuple:
    """Extract (title, content) from a provider payload.

    Handles the flat answer documented by the provider and a nested
    ``{"data": {...}}`` envelope used by some gateways. Returns
    ("", "") when no page text is present.
    """
    if not isinstance(data, dict):
        return "", ""
    candidates = [data]
    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.append(inner)
    title = ""
    content = ""
    for candidate in candidates:
        if not title:
            title = str(candidate.get("title") or "").strip()
        if not content:
            for field in _CONTENT_FIELDS:
                value = candidate.get(field)
                if isinstance(value, str) and value.strip():
                    content = value
                    break
    return title, content


def fetch_page_content(url: str, *, svc_name: str = "",
                       cfg: Optional[dict] = None,
                       timeout: tuple = FETCH_REQUEST_TIMEOUT) -> dict:
    """Fetch one web page through the provider's url-fetch API.

    Sends ``POST <fetch endpoint>`` with ``{"url": <url>}`` and the
    provider's bearer key, then returns ``{"title": str, "content": str}``
    where *content* is the page text in Markdown.

    Args:
        url: Target page; http/https only.
        svc_name: Provider name from the service registry.
        cfg: Pre-loaded key store; ``load_config()`` is used when omitted.
        timeout: requests timeout tuple (connect, read).

    Raises:
        ValueError: invalid URL or a provider without a fetch endpoint.
        ServiceNotFoundError / ApiKeyMissingError: bad service setup.
        ProviderHTTPError / ProviderResponseError / RequestTimeoutError /
        NetworkError: transport and payload failures.
    """
    clean_url = validate_fetch_url(url)
    name = str(svc_name or "").strip()
    svc = get_services().get(name) if name else None
    if not isinstance(svc, dict):
        raise ServiceNotFoundError(name or "<none>")
    endpoint = fetch_endpoint(svc)
    if not endpoint:
        raise ValueError(
            f"Service '{name}' does not provide a url-fetch endpoint."
        )
    conf = cfg if isinstance(cfg, dict) else load_config()
    key_field = str(svc.get("config_key") or "")
    key = conf.get(key_field, "") if key_field else ""
    if isinstance(key, str):
        key = key.strip()
    if not key:
        raise ApiKeyMissingError(name, field=key_field or "api key")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = requests.post(
            endpoint, headers=headers, json={"url": clean_url},
            timeout=timeout, verify=_VERIFY_TLS,
        )
    except requests.exceptions.Timeout:
        raise RequestTimeoutError(service=name)
    except requests.exceptions.RequestException as exc:
        raise NetworkError(str(exc), service=name)
    if r.status_code != 200:
        raise ProviderHTTPError(
            r.status_code, _extract_error_body(r), service=name,
        )
    try:
        data = r.json()
    except Exception:
        raise ProviderResponseError(
            "failed",
            provider_message="the url-fetch response is not valid JSON",
            service=name,
        )
    title, content = _page_from_payload(data)
    if not content.strip():
        raise ProviderResponseError(
            "failed",
            provider_message=(
                "the url-fetch response contains no page content "
                "(the target site may block automated access)"
            ),
            service=name,
        )
    return {"title": title, "content": content}


# ─── Direct downloads: PDF files and the browser branch ─────────────────────

#: User agent of a direct download; some sites reject the default one.
_DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (compatible; SagaAI/2.9; +https://sagaai.local) "
    "PageFetcher/1.0"
)

#: Extensions that mark a direct file download rather than a page.
_PDF_SUFFIXES = (".pdf",)


def looks_like_pdf_url(url: str) -> bool:
    """Return True when the URL path ends with a PDF extension."""
    parsed = urlparse(str(url or "").strip())
    return str(parsed.path or "").lower().endswith(_PDF_SUFFIXES)


def is_pdf_response(content_type: str) -> bool:
    """Return True when a response content type announces a PDF."""
    return "application/pdf" in str(content_type or "").lower()


def _pdf_page_section(number: int, text: str) -> str:
    """Render one PDF page as a Markdown section with tidy line breaks."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    body = "\n".join(line for line in lines if line)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return f"### Page {number}\n\n{body}".strip()


def extract_pdf_markdown(raw: bytes) -> str:
    """Extract text from a PDF and render it as Markdown.

    Each page becomes a ``### Page N`` section, so a long document keeps a
    navigable structure. ``pdfplumber`` is tried first (it copes better
    with tables and unusual encodings) and ``pypdf`` is the fallback.

    Args:
        raw: PDF file bytes.

    Returns:
        Markdown text; an empty string when the document has no text
        layer at all (for example a scan without OCR).

    Raises:
        RuntimeError: neither PDF library is installed or both failed.
    """
    if not raw:
        return ""
    errors = []
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = (page.extract_text() or "").strip()
                if text:
                    parts.append(_pdf_page_section(i, text))
        return "\n\n".join(parts)
    except ImportError:
        errors.append("pdfplumber is not installed")
    except Exception as exc:
        errors.append(f"pdfplumber: {exc}")
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(raw))
        parts = []
        for i, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(_pdf_page_section(i, text))
        return "\n\n".join(parts)
    except ImportError:
        errors.append("pypdf is not installed")
    except Exception as exc:
        errors.append(f"pypdf: {exc}")
    raise RuntimeError("PDF text extraction failed: " + "; ".join(errors))


def download_bytes(url: str, *, timeout: tuple = DOWNLOAD_TIMEOUT,
                   max_bytes: int = MAX_DOWNLOAD_BYTES) -> dict:
    """Download a URL directly and return its bytes and content type.

    Used for file targets (a PDF behind a link) where the provider fetch
    API is not involved. The size is bounded while streaming, so an
    endless response cannot exhaust memory.

    Args:
        url: Absolute http/https URL; local and private hosts are rejected
            by :func:`validate_fetch_url`.
        timeout: requests timeout tuple (connect, read).
        max_bytes: Upper bound of the accepted body size.

    Returns:
        ``{"content": bytes, "content_type": str, "final_url": str}``.

    Raises:
        ValueError: invalid URL (see :func:`validate_fetch_url`).
        ProviderHTTPError / RequestTimeoutError / NetworkError: transport
        failures, mapped to the platform error contract.
    """
    clean_url = validate_fetch_url(url)
    headers = {
        "User-Agent": _DOWNLOAD_USER_AGENT,
        "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    }
    try:
        with requests.get(clean_url, headers=headers, timeout=timeout,
                          stream=True, verify=_VERIFY_TLS,
                          allow_redirects=True) as r:
            if r.status_code != 200:
                raise ProviderHTTPError(
                    r.status_code, _extract_error_body(r), service="download",
                )
            declared = str(r.headers.get("Content-Length") or "").strip()
            if declared.isdigit() and int(declared) > max_bytes:
                raise ProviderResponseError(
                    "failed",
                    provider_message=(
                        f"the file is larger than the {max_bytes} byte limit"
                    ),
                    service="download",
                )
            chunks = []
            total = 0
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ProviderResponseError(
                        "failed",
                        provider_message=(
                            f"the file exceeds the {max_bytes} byte limit"
                        ),
                        service="download",
                    )
                chunks.append(chunk)
            return {
                "content": b"".join(chunks),
                "content_type": str(r.headers.get("Content-Type") or ""),
                "final_url": str(r.url or clean_url),
            }
    except (ProviderHTTPError, ProviderResponseError):
        raise
    except requests.exceptions.Timeout:
        raise RequestTimeoutError(service="download")
    except requests.exceptions.RequestException as exc:
        raise NetworkError(str(exc), service="download")


def selenium_page_text(url: str,
                       page_load_timeout: int = SELENIUM_PAGE_LOAD_TIMEOUT,
                       settle_seconds: float = SELENIUM_SETTLE_SECONDS) -> dict:
    """Render a page in a real browser and return its visible text.

    Selenium drives a headless Chrome instance: the page is loaded with its
    scripts, the DOM is allowed to settle, and ``document.body.innerText``
    supplies the text a human would see. Markdown formatting is produced
    later by the orchestrator's main model.

    Args:
        url: Absolute http/https URL; local and private hosts are rejected.
        page_load_timeout: Seconds to wait for the load event.
        settle_seconds: Extra pause for late script rendering.

    Returns:
        ``{"title": str, "text": str, "final_url": str}``.

    Raises:
        ValueError: invalid URL.
        SeleniumUnavailableError: the package or the browser driver is
            missing; the message names the install step.
        RuntimeError: the browser could not load the page.
    """
    clean_url = validate_fetch_url(url)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.common.exceptions import WebDriverException
    except ImportError as exc:
        raise SeleniumUnavailableError(
            "Selenium is not installed. Install it with "
            "'pip install selenium' and make sure Google Chrome is "
            "available, then enable the option again."
        ) from exc

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,2200")
    options.add_argument(f"--user-agent={_DOWNLOAD_USER_AGENT}")

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(page_load_timeout)
        try:
            driver.get(clean_url)
        except WebDriverException as exc:
            # A page that never fires the load event still has content;
            # only a request-level failure is fatal.
            message = str(exc).lower()
            if "timeout" not in message and "timed out" not in message:
                raise RuntimeError(f"the browser could not load the page: {exc}")
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        title = str(driver.title or "").strip()
        text = str(driver.execute_script(
            "return document.body ? document.body.innerText : '';"
        ) or "")
        final_url = str(getattr(driver, "current_url", "") or clean_url)
    except SeleniumUnavailableError:
        raise
    except RuntimeError:
        raise
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        if ("chromedriver" in lowered or "executable needs to be in path"
                in lowered or "cannot find chrome" in lowered
                or "chrome not reachable" in lowered):
            raise SeleniumUnavailableError(
                "Chrome or chromedriver is not available for Selenium. "
                "Install Google Chrome (Selenium Manager downloads the "
                "matching driver automatically) and try again. Details: "
                f"{message}"
            ) from exc
        raise RuntimeError(f"the browser render failed: {message}") from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return {"title": title, "text": text, "final_url": final_url}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
