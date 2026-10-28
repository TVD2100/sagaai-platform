"""
core.marketplace.source - catalog transport.

The catalog is a plain folder tree in a GitHub repository:

    index.json
    skills/<id>/<version>/entity.json
    skills/<id>/<version>/package.tar.gz

Metadata is fetched raw by tag, packages come from GitHub Releases
(D-24). The transport is ``urllib.request`` - the same choice
``core/updater.py`` makes - so no new dependency is introduced.

``MarketplaceSource`` accepts an explicit ``base_url`` and an injected
``urlopen``: tests point it at a local ``http.server`` and never touch the
network. The domain allowlist applies only to the production URL - a
``base_url`` given explicitly (local server, ``file://``) is trusted.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional

from core.marketplace import store


DEFAULT_BASE_URL = (
    "https://raw.githubusercontent.com/TVD2100/sagaai-marketplace/main"
)
ALLOWED_DOMAINS = (
    "raw.githubusercontent.com",
    "github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
)

# Catalog folders are plural (``skills/``, ``agents/``) while entity types
# are singular; the mapping lives here so the transport and the fixtures
# cannot drift apart.
TYPE_FOLDERS = {
    "agent": "agents",
    "skill": "skills",
    "rag": "rag",
    "mcp": "mcp",
}
REQUEST_TIMEOUT = 30
RETRY_COUNT = 3
RETRY_DELAY = 0.5


def type_folder(type_: str) -> str:
    """Return the catalog folder name of an entity type."""
    return TYPE_FOLDERS.get(str(type_ or "").strip(), str(type_ or "").strip())


class SourceError(store.MarketplaceError):
    """Raised when the catalog cannot be reached or returns garbage."""


def _domain_of(url: str) -> str:
    """Return the lowercase host of an absolute URL."""
    parsed = urllib.parse.urlparse(url)
    return (parsed.hostname or "").lower()


def _is_local(url: str) -> bool:
    """True for ``file://`` URLs and loopback hosts (test channels)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        return True
    host = (parsed.hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def _mark_stale(payload: dict) -> dict:
    """Return a copy of a cached index marked as stale.

    The caller served the index because the catalog could not be reached;
    the flag lets the showcase tell the user the data may be out of date
    instead of silently presenting an old catalog as current.
    """
    marked = dict(payload)
    marked["stale"] = True
    return marked


class MarketplaceSource:
    """Fetches the catalog index, entity manifests and packages.

    Parameters:
        base_url: catalog root. Defaults to the production repository.
        urlopen: injectable opener, used by tests to avoid real sockets.

    The index is cached under ``.marketplace/cache/catalog-index.json``
    together with its ``ETag`` / ``Last-Modified`` so a repeat request can
    be answered with ``304 Not Modified`` and an offline start still shows
    the last known catalog.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        urlopen: Optional[Callable] = None,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._urlopen = urlopen or urllib.request.urlopen
        self._enforce_allowlist = base_url is None

    # ── URL helpers ───────────────────────────────────────────────────

    def _join(self, *parts: str) -> str:
        """Join parts onto ``base_url`` with forward slashes."""
        tail = "/".join(str(p).strip("/") for p in parts if str(p).strip("/"))
        return f"{self.base_url}/{tail}" if tail else self.base_url

    def _check_domain(self, url: str) -> None:
        """Reject a URL outside the allowlist when the source is remote.

        An explicitly provided ``base_url`` disables the check: the caller
        already chose the channel (a local fixture server, a mirror), and
        the allowlist exists to constrain the *default* production source
        from being redirected elsewhere by a manifest.
        """
        if not self._enforce_allowlist or _is_local(url):
            return
        domain = _domain_of(url)
        if domain not in ALLOWED_DOMAINS:
            raise SourceError(
                "domain_not_allowed",
                f"Catalog URL outside the allowlist: {domain or url}",
                {"url": url, "allowed": list(ALLOWED_DOMAINS)},
            )

    # ── Low-level GET ─────────────────────────────────────────────────

    def _open(self, url: str, headers: Optional[Dict[str, str]] = None):
        """Run one request with retries and return the response object.

        ``HTTPError`` carrying 304 is returned as-is so the caller can keep
        its cached copy; 404 is not retried. Any other transport failure is
        retried ``RETRY_COUNT`` times with a short pause.
        """
        self._check_domain(url)
        request = urllib.request.Request(url, headers=headers or {})
        last_error: Optional[Exception] = None
        for attempt in range(RETRY_COUNT):
            try:
                return self._urlopen(request, timeout=REQUEST_TIMEOUT)
            except urllib.error.HTTPError as exc:
                if exc.code in (304, 404):
                    raise
                last_error = exc
            except Exception as exc:  # timeout, DNS, connection reset
                last_error = exc
            if attempt < RETRY_COUNT - 1:
                time.sleep(RETRY_DELAY)
        raise SourceError(
            "fetch_failed",
            f"Catalog request failed: {last_error}",
            {"url": url},
        )

    def fetch_bytes(self, url: str) -> bytes:
        """Download *url* and return the raw bytes.

        A relative URL is resolved against ``base_url``; an absolute one is
        checked against the allowlist before use.
        """
        absolute = url if "://" in url else self._join(url)
        try:
            response = self._open(absolute)
        except urllib.error.HTTPError as exc:
            raise SourceError(
                "http_error",
                f"Catalog returned HTTP {exc.code} for {absolute}",
                {"url": absolute, "status": exc.code},
            ) from exc
        try:
            return response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    # ── Index ─────────────────────────────────────────────────────────

    def _load_cached_index(self) -> dict:
        """Return the cached index payload (metadata included)."""
        data = store.read_json(store.catalog_index_cache_path(), default={})
        return data if isinstance(data, dict) else {}

    def fetch_index(self, force: bool = False) -> dict:
        """Return the catalog index, using a conditional GET.

        ``force=True`` sends no validators. Otherwise the cached ``ETag`` /
        ``Last-Modified`` go out, and a ``304`` keeps the cached payload.
        A transport failure falls back to the cache so the section still
        renders offline with a staleness marker.
        """
        cache = self._load_cached_index()
        cached_index = cache.get("index") if isinstance(cache.get("index"), dict) else None
        headers: Dict[str, str] = {}
        if not force and cached_index is not None:
            if cache.get("etag"):
                headers["If-None-Match"] = str(cache["etag"])
            if cache.get("last_modified"):
                headers["If-Modified-Since"] = str(cache["last_modified"])

        url = self._join("index.json")
        try:
            response = self._open(url, headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached_index is not None:
                return cached_index
            if cached_index is not None:
                return _mark_stale(cached_index)
            raise SourceError(
                "http_error",
                f"Catalog index request failed with HTTP {exc.code}",
                {"url": url, "status": exc.code},
            ) from exc
        except SourceError:
            if cached_index is not None:
                return _mark_stale(cached_index)
            raise

        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceError(
                "invalid_index",
                f"Catalog index is not valid JSON: {exc}",
                {"url": url},
            ) from exc
        if not isinstance(payload, dict):
            raise SourceError(
                "invalid_index",
                "Catalog index must be a JSON object",
                {"url": url},
            )

        etag = None
        last_modified = None
        get_header = getattr(response, "headers", None)
        if get_header is not None:
            etag = get_header.get("ETag")
            last_modified = get_header.get("Last-Modified")
        store.atomic_write_json(
            store.catalog_index_cache_path(),
            {
                "index": payload,
                "etag": etag,
                "last_modified": last_modified,
                "fetched_at": store.isoformat_utc(),
            },
        )
        return payload

    # ── Entity manifests ──────────────────────────────────────────────

    def _entity_cache_path(self, type_: str, id_: str, version: str) -> str:
        """Return the cache path of one entity manifest."""
        safe_id = id_.replace("/", "__")
        return os.path.join(store.manifest_cache_dir(), f"{type_}__{safe_id}__{version}.json")

    def fetch_entity(
        self,
        type_: str,
        id_: str,
        version: Optional[str] = None,
    ) -> dict:
        """Return the ``entity.json`` of one catalog entity.

        With *version* the path is ``<type>/<id>/<version>/entity.json``,
        without it the ``latest`` pointer of the same folder. The manifest
        is cached per ``(type, id, version)`` so an offline render can show
        the card of an already-seen entity.
        """
        tag = version or "latest"
        cache_path = self._entity_cache_path(type_, id_, tag)
        try:
            raw = self.fetch_bytes(f"{type_folder(type_)}/{id_}/{tag}/entity.json")
        except SourceError:
            cached = store.read_json(cache_path, default=None)
            if isinstance(cached, dict):
                cached.setdefault("offline", True)
                return cached
            raise
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceError(
                "invalid_entity",
                f"Entity manifest is not valid JSON: {exc}",
                {"type": type_, "id": id_, "version": tag},
            ) from exc
        if not isinstance(payload, dict):
            raise SourceError(
                "invalid_entity",
                "Entity manifest must be a JSON object",
                {"type": type_, "id": id_, "version": tag},
            )
        try:
            store.atomic_write_json(cache_path, payload)
        except OSError:
            pass
        return payload

    def package_url(self, entity: dict) -> str:
        """Return the package URL declared by an entity manifest.

        ``package.url`` may be absolute (a GitHub Release asset) or relative
        to the catalog root. Missing declarations are an error: a package
        without a URL cannot be verified.
        """
        package = entity.get("package") if isinstance(entity.get("package"), dict) else {}
        url = str(package.get("url") or entity.get("package_url") or "").strip()
        if not url:
            raise SourceError(
                "missing_package_url",
                "Entity manifest declares no package URL",
                {"id": entity.get("id"), "version": entity.get("version")},
            )
        return url if "://" in url or _is_local(url) else self._join(url)

    def fetch_package(self, entity: dict) -> bytes:
        """Download the package of an entity, preferring the local cache.

        The package is identified by its ``package.sha256``; a cached copy
        that matches is returned without any network request.
        """
        package = entity.get("package") if isinstance(entity.get("package"), dict) else {}
        sha = str(package.get("sha256") or "").strip()
        if sha:
            cached = store.get_cached_package(sha)
            if cached is not None:
                return cached
        data = self.fetch_bytes(self.package_url(entity))
        if sha:
            store.cache_package(sha, data)
        return data

    def fetch_bundle(self, bundle_id: str) -> dict:
        """Return the ``bundles/<id>.json`` record of a curated set.

        A bundle pins exact versions of several entities so one install
        action reproduces the whole set. It is optional in the catalog: a
        catalog without ``bundles/`` simply answers with an error the caller
        renders as "this set is unavailable", never a crash.
        """
        safe_id = str(bundle_id or "").strip().replace("/", "__")
        if not safe_id:
            raise SourceError("invalid_bundle", "Bundle id is empty", {})
        cache_path = os.path.join(
            store.manifest_cache_dir(), f"bundle__{safe_id}.json"
        )
        try:
            raw = self.fetch_bytes(f"bundles/{safe_id}.json")
        except SourceError:
            cached = store.read_json(cache_path, default=None)
            if isinstance(cached, dict):
                cached.setdefault("offline", True)
                return cached
            raise
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceError(
                "invalid_bundle",
                f"Bundle is not valid JSON: {exc}",
                {"id": safe_id},
            ) from exc
        if not isinstance(payload, dict):
            raise SourceError(
                "invalid_bundle",
                "Bundle must be a JSON object",
                {"id": safe_id},
            )
        try:
            store.atomic_write_json(cache_path, payload)
        except OSError:
            pass
        return payload



def default_source(base_url: Optional[str] = None) -> MarketplaceSource:
    """Return a source bound to the configured or production catalog.

    ``marketplace.base_url`` from the config wins; an empty value keeps the
    production repository. Tests pass their own ``base_url`` instead of
    relying on this helper.
    """
    if base_url:
        return MarketplaceSource(base_url)
    try:
        from core.marketplace import config as marketplace_config

        configured = str(marketplace_config.marketplace_settings().get("base_url") or "").strip()
    except Exception:
        configured = ""
    return MarketplaceSource(configured or None)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
