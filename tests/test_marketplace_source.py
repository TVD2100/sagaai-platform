"""
test_marketplace_source.py - catalog transport over a local HTTP channel.

The production catalog is a GitHub repository, but no test touches it: the
source accepts an injected ``urlopen`` and an explicit ``base_url``, so the
same code paths run against an ``http.server`` on loopback. These tests pin
caching (conditional GET), the domain allowlist for the production channel
and the retry behaviour.
"""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    free_port,
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
)


class TestIndex:
    def test_fetches_index(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace.source import MarketplaceSource

        catalog = make_catalog(tmp_path / "catalog", [make_entity()])
        base_url = serve_catalog(catalog)
        source = MarketplaceSource(base_url)

        index = source.fetch_index()
        assert index["schema"] == "sagaai.marketplace.index/1"
        assert len(index["entities"]) == 1
        assert index["entities"][0]["id"] == "demo_skill"

    def test_caches_index_on_disk(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import store
        from core.marketplace.source import MarketplaceSource

        catalog = make_catalog(tmp_path / "catalog", [make_entity()])
        source = MarketplaceSource(serve_catalog(catalog))
        source.fetch_index()

        cached = json.loads(Path(store.catalog_index_cache_path()).read_text(encoding="utf-8"))
        assert cached["index"]["entities"][0]["id"] == "demo_skill"

    def test_serves_cache_when_catalog_is_unreachable(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.source import MarketplaceSource

        catalog = make_catalog(tmp_path / "catalog", [make_entity()])
        base_url = serve_catalog(catalog)
        MarketplaceSource(base_url).fetch_index()

        # A dead port stands in for "catalog is down": the section must keep
        # rendering the last known index instead of failing to open.
        offline = MarketplaceSource(f"http://127.0.0.1:{free_port()}")
        index = offline.fetch_index()
        assert index["entities"][0]["id"] == "demo_skill"

    def test_invalid_json_raises(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace.source import MarketplaceSource, SourceError

        catalog = tmp_path / "broken"
        catalog.mkdir(parents=True, exist_ok=True)
        (catalog / "index.json").write_text("{not json", encoding="utf-8")
        source = MarketplaceSource(serve_catalog(catalog))
        with pytest.raises(SourceError) as excinfo:
            source.fetch_index()
        assert excinfo.value.code == "invalid_index"


class TestConditionalGet:
    def test_304_keeps_the_cached_payload(self, marketplace_data_dir):
        from core.marketplace.source import MarketplaceSource

        calls = []

        class _Response:
            def __init__(self, body: bytes = b"", headers=None):
                self._body = body
                self.headers = headers or {}

            def read(self):
                return self._body

            def close(self):
                return None

        def _urlopen(request, timeout=30):
            calls.append(dict(request.headers))
            if len(calls) == 1:
                return _Response(
                    json.dumps({"entities": [{"id": "first"}]}).encode("utf-8"),
                    {"ETag": "\"abc\""},
                )
            # A real server answers 304 with no body when the tag matches.
            import urllib.error

            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, io.BytesIO(b""))

        source = MarketplaceSource("http://127.0.0.1:1/catalog", urlopen=_urlopen)
        first = source.fetch_index()
        second = source.fetch_index()

        assert first["entities"][0]["id"] == "first"
        assert second == first
        # The second request carried the validator, so a 304 was possible.
        assert calls[1].get("If-none-match") == "\"abc\""

    def test_force_skips_the_validator(self, marketplace_data_dir):
        from core.marketplace.source import MarketplaceSource

        calls = []

        class _Response:
            headers = {}

            def __init__(self, body: bytes):
                self._body = body

            def read(self):
                return self._body

            def close(self):
                return None

        def _urlopen(request, timeout=30):
            calls.append(dict(request.headers))
            return _Response(json.dumps({"entities": []}).encode("utf-8"))

        source = MarketplaceSource("http://127.0.0.1:1/catalog", urlopen=_urlopen)
        source.fetch_index()
        source.fetch_index(force=True)
        assert "If-none-match" not in calls[1]


class TestRetries:
    def test_transient_failure_is_retried(self, marketplace_data_dir, monkeypatch):
        from core.marketplace import source as source_mod

        monkeypatch.setattr(source_mod, "RETRY_DELAY", 0)
        attempts = []

        class _Response:
            headers = {}

            def read(self):
                return b"{}"

            def close(self):
                return None

        def _urlopen(request, timeout=30):
            attempts.append(1)
            if len(attempts) < 3:
                raise TimeoutError("timed out")
            return _Response()

        source = source_mod.MarketplaceSource("http://127.0.0.1:1/catalog", urlopen=_urlopen)
        assert source.fetch_index() == {}
        assert len(attempts) == 3

    def test_gives_up_after_three_attempts(self, marketplace_data_dir, monkeypatch):
        from core.marketplace import source as source_mod

        monkeypatch.setattr(source_mod, "RETRY_DELAY", 0)
        attempts = []

        def _urlopen(request, timeout=30):
            attempts.append(1)
            raise TimeoutError("timed out")

        source = source_mod.MarketplaceSource("http://127.0.0.1:1/catalog", urlopen=_urlopen)
        with pytest.raises(source_mod.SourceError) as excinfo:
            source.fetch_index()
        assert excinfo.value.code == "fetch_failed"
        assert len(attempts) == 3


class TestAllowlist:
    def test_production_channel_rejects_foreign_domains(self, marketplace_data_dir):
        from core.marketplace.source import MarketplaceSource, SourceError

        # No explicit base_url: this is the production channel, and a
        # manifest must not redirect it to an arbitrary host.
        source = MarketplaceSource()
        with pytest.raises(SourceError) as excinfo:
            source.fetch_bytes("https://evil.example.com/package.tar.gz")
        assert excinfo.value.code == "domain_not_allowed"

    def test_production_channel_allows_github(self, marketplace_data_dir):
        from core.marketplace.source import ALLOWED_DOMAINS, MarketplaceSource

        source = MarketplaceSource()
        assert "raw.githubusercontent.com" in ALLOWED_DOMAINS
        try:
            source._check_domain("https://raw.githubusercontent.com/owner/repo/main/index.json")
        except Exception as exc:  # pragma: no cover - guard
            pytest.fail(f"github must be allowed: {exc}")

    def test_loopback_is_always_allowed(self, marketplace_data_dir):
        from core.marketplace.source import MarketplaceSource

        source = MarketplaceSource()
        source._check_domain("http://127.0.0.1:8080/index.json")
        source._check_domain("http://localhost:8080/index.json")

    def test_explicit_base_url_is_trusted(self, marketplace_data_dir):
        from core.marketplace.source import MarketplaceSource

        source = MarketplaceSource("https://mirror.example.com/catalog")
        # The caller chose the channel; only the default production source
        # is constrained by the allowlist.
        source._check_domain("https://mirror.example.com/catalog/index.json")


class TestEntityAndPackage:
    def test_fetch_entity_by_version(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace.source import MarketplaceSource

        catalog = make_catalog(tmp_path / "catalog", [make_entity(version="2.3.4")])
        source = MarketplaceSource(serve_catalog(catalog))
        entity = source.fetch_entity("skill", "demo_skill", "2.3.4")
        assert entity["version"] == "2.3.4"
        assert entity["files"]["SKILL.md"]

    def test_fetch_entity_latest_pointer(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace.source import MarketplaceSource

        catalog = make_catalog(tmp_path / "catalog", [make_entity(version="2.3.4")])
        source = MarketplaceSource(serve_catalog(catalog))
        assert source.fetch_entity("skill", "demo_skill")["version"] == "2.3.4"

    def test_entity_served_from_cache_when_offline(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace.source import MarketplaceSource

        catalog = make_catalog(tmp_path / "catalog", [make_entity()])
        MarketplaceSource(serve_catalog(catalog)).fetch_entity("skill", "demo_skill", "1.0.0")

        offline = MarketplaceSource(f"http://127.0.0.1:{free_port()}")
        entity = offline.fetch_entity("skill", "demo_skill", "1.0.0")
        assert entity["id"] == "demo_skill"
        assert entity["offline"] is True

    def test_missing_package_url_raises(self, marketplace_data_dir):
        from core.marketplace.source import MarketplaceSource, SourceError

        source = MarketplaceSource("http://127.0.0.1:1/catalog")
        with pytest.raises(SourceError) as excinfo:
            source.package_url({"id": "x", "version": "1.0.0"})
        assert excinfo.value.code == "missing_package_url"

    def test_package_is_cached_after_download(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace import store
        from core.marketplace.source import MarketplaceSource

        entity, package_bytes = make_entity()
        catalog = make_catalog(tmp_path / "catalog", [(entity, package_bytes)])
        source = MarketplaceSource(serve_catalog(catalog))

        downloaded = source.fetch_package(entity)
        assert downloaded == package_bytes
        assert store.get_cached_package(entity["package"]["sha256"]) == package_bytes

    def test_second_fetch_uses_the_cache(self, marketplace_data_dir, serve_catalog, tmp_path):
        from core.marketplace.source import MarketplaceSource

        entity, package_bytes = make_entity()
        catalog = make_catalog(tmp_path / "catalog", [(entity, package_bytes)])
        base_url = serve_catalog(catalog)
        MarketplaceSource(base_url).fetch_package(entity)

        # A dead channel proves the second call did not go to the network.
        offline = MarketplaceSource(f"http://127.0.0.1:{free_port()}")
        assert offline.fetch_package(entity) == package_bytes
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
