"""
Updates across entity types (P5, P6).

The updater was written when only skills were installable: it looked the
registry row up as ``skill`` and pushed every update through the three-path
resolver, which knows nothing about a base or an agent. A base could be
found by the check and then never updated. These tests pin the type-aware
lookup, the installer-backed update path and the hashing area of a base.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    add_catalog_version,
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
    sha256_hex,
)


#: Chunks of the first published index.
CHUNKS_V1 = [
    "Документ: Yandex Cloud. Продукт: Object Storage. Заголовок: Бакеты",
    "Документ: Yandex Cloud. Продукт: Object Storage. Заголовок: Политики",
]

#: Chunks of the second published index - one more document than v1.
CHUNKS_V2 = CHUNKS_V1 + [
    "Документ: Yandex Cloud. Продукт: Managed Kubernetes. Заголовок: Кластеры",
]


# ─── Helpers ──────────────────────────────────────────────────────────────

def _settings(monkeypatch, **overrides):
    """Patch the marketplace settings for one test."""
    from core.marketplace import config as marketplace_config

    values = {
        "enabled": True,
        "base_url": "",
        "allow_code": True,
        "require_signature": False,
        "channel": "main",
    }
    values.update(overrides)
    monkeypatch.setattr(marketplace_config, "marketplace_settings", lambda: dict(values))
    return values


def _source(base_url):
    """Return a catalog transport bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


def _make_index(path: Path, texts) -> Path:
    """Build an index database holding *texts* and return its path."""
    from core import rag_bm25, rag_index

    rag_index.create_index_db(
        str(path), dimension=256, provider="YandexAI", embedding_model="text-search-doc"
    )
    for position, text in enumerate(texts):
        rag_index.add_chunk(str(path), text, source="doc.md", chunk_index=position)
    rag_bm25.rebuild_index(str(path))
    return path


def _base_version(tmp_path: Path, version: str, *, texts=None, files=None):
    """Return ``(entity, package, index_bytes)`` for one base version.

    ``texts`` publishes a prebuilt index, ``files`` publishes sources; a
    version may carry both. The index bytes are returned so the test can
    place them in the served catalog at the URL the manifest declares.
    """
    content = dict(files or {})
    extra = {
        "manifest": {
            "name": {"en": "Demo base", "ru": "Демо-база"},
            "description": {"en": "A base used by the tests", "ru": "База для тестов"},
            "search_mode": "bm25",
        }
    }
    index_bytes = b""
    if texts:
        index_path = _make_index(tmp_path / f"index-{version}.db", texts)
        index_bytes = index_path.read_bytes()
        extra["distribution"] = {
            "mode": "sources_plus_index" if content else "prebuilt_index",
            "index_url": f"rag/demo_rag/{version}/index.db",
            "index_sha256": sha256_hex(index_bytes),
            "index_bytes": len(index_bytes),
            "index_schema": 3,
            "dimensions": 256,
            "embedding_model": "text-search-doc",
            "chunks": len(texts),
            "sources_count": len(texts),
        }
    else:
        extra["distribution"] = {"mode": "sources_only"}

    entity, package = make_entity(
        entity_id="demo_rag",
        version=version,
        type_="rag",
        files=content,
        extra=extra,
    )
    return entity, package, index_bytes


def _place_index(root: Path, entity: dict, index_bytes: bytes) -> None:
    """Write the index of *entity* into the catalog folder it declares."""
    if not index_bytes:
        return
    target = root / entity["distribution"]["index_url"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(index_bytes)


def _publish_two_versions(tmp_path: Path, serve_catalog, *, mode="prebuilt"):
    """Serve versions 1.0.0 and 1.1.0 of one base.

    Returns ``(root, url, first_entity, second_entity)``: the entities are
    the ones actually served, so an install in a test declares exactly the
    bytes the catalog holds.

    ``mode`` chooses what the two versions ship: ``prebuilt`` publishes an
    index only, ``sources`` publishes source documents only.
    """
    if mode == "sources":
        files_v1 = {"files/doc.md": "# Бакеты\n\nПолитика доступа.\n"}
        files_v2 = {
            "files/doc.md": "# Бакеты\n\nПолитика доступа к бакету.\n",
            "files/extra.md": "# Кластеры\n\nВерсии Kubernetes.\n",
        }
        first, package_v1, index_v1 = _base_version(tmp_path, "1.0.0", files=files_v1)
        second, package_v2, index_v2 = _base_version(tmp_path, "1.1.0", files=files_v2)
    else:
        first, package_v1, index_v1 = _base_version(tmp_path, "1.0.0", texts=CHUNKS_V1)
        second, package_v2, index_v2 = _base_version(tmp_path, "1.1.0", texts=CHUNKS_V2)

    root = make_catalog(tmp_path / "catalog", [(first, package_v1)])
    _place_index(root, first, index_v1)
    add_catalog_version(root, second, package_v2)
    _place_index(root, second, index_v2)
    return root, serve_catalog(root), first, second


def _install(base_url: str, entity: dict):
    """Install one base through the real installer."""
    from core.marketplace.rag_installer import install_rag

    return install_rag(entity, channel="main", source=_source(base_url))


# ─── Discovery ────────────────────────────────────────────────────────────

def test_a_base_update_is_reported_in_the_rag_section(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    first, _package, index_v1 = _base_version(tmp_path, "1.0.0", texts=CHUNKS_V1)
    root = make_catalog(tmp_path / "catalog", [(first, _package)])
    _place_index(root, first, index_v1)
    base_url = serve_catalog(root)
    _install(base_url, first)

    second, package_v2, index_v2 = _base_version(tmp_path, "1.1.0", texts=CHUNKS_V2)
    add_catalog_version(root, second, package_v2)
    _place_index(root, second, index_v2)

    from core.marketplace import updates

    report = updates.check_entity_updates(source=_source(base_url))

    assert report["checked"] == 1
    assert [item["id"] for item in report["rag"]] == ["demo_rag"]
    item = report["rag"][0]
    assert item["action"] == "update"
    assert item["type"] == "rag"
    assert item["installed_version"] == "1.0.0"
    assert item["version"] == "1.1.0"


# ─── Applying ─────────────────────────────────────────────────────────────

def test_applying_a_base_update_replaces_the_index_in_the_same_folder(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    root, base_url, first, _second = _publish_two_versions(tmp_path, serve_catalog)
    installed = _install(base_url, first)
    folder = Path(_installed_folder(installed["runtime_slug"]))
    old_index = (folder / "index.db").read_bytes()

    from core.marketplace import registry, updates

    report = updates.apply_entity_updates(["demo_rag"], "keep_mine", source=_source(base_url))

    assert report["ok"] is True
    assert len(report["applied"]) == 1
    assert report["applied"][0]["version"] == "1.1.0"
    # An update keeps the folder: assistants and threads point at the slug.
    assert report["applied"][0]["install_id"]
    assert _installed_folder("demo_rag") == str(folder)
    assert (folder / "index.db").read_bytes() != old_index
    row = registry.get_installed("rag", "demo_rag")
    assert row["version"] == "1.1.0"


def test_a_qualified_reference_chooses_the_type(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    first, _package, index_v1 = _base_version(tmp_path, "1.0.0", texts=CHUNKS_V1)
    root = make_catalog(tmp_path / "catalog", [(first, _package)])
    _place_index(root, first, index_v1)
    base_url = serve_catalog(root)
    _install(base_url, first)

    from core.marketplace import updates

    assert updates._resolve_row("demo_rag")[0] == "rag"
    assert updates._resolve_row("rag:demo_rag")[0] == "rag"
    assert updates._resolve_row("skill:demo_rag")[2] is None
    assert updates._resolve_row("nowhere")[2] is None


def test_an_edited_base_is_not_overwritten_under_keep_mine(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    """A base is replaced as a whole, so the per-file keep policy skips it."""
    _settings(monkeypatch)
    _root, base_url, first, _second = _publish_two_versions(
        tmp_path, serve_catalog, mode="sources"
    )
    installed = _install(base_url, first)
    folder = Path(_installed_folder(installed["runtime_slug"]))
    edited = folder / "files" / "doc.md"
    edited.write_text("# мои заметки\n", encoding="utf-8")

    from core.marketplace import updates

    report = updates.apply_entity_updates(["demo_rag"], "keep_mine", source=_source(base_url))

    assert report["applied"] == []
    assert report["skipped"][0]["reason"] == "user_modified"
    assert edited.read_text(encoding="utf-8") == "# мои заметки\n"


def test_take_catalog_replaces_an_edited_base(
    marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
):
    _settings(monkeypatch)
    _root, base_url, first, _second = _publish_two_versions(
        tmp_path, serve_catalog, mode="sources"
    )
    installed = _install(base_url, first)
    folder = Path(_installed_folder(installed["runtime_slug"]))
    (folder / "files" / "doc.md").write_text("# мои заметки\n", encoding="utf-8")

    from core.marketplace import registry, updates

    report = updates.apply_entity_updates(["demo_rag"], "take_catalog", source=_source(base_url))

    assert report["ok"] is True
    assert report["applied"][0]["version"] == "1.1.0"
    assert (folder / "files" / "doc.md").read_text(encoding="utf-8").startswith("# Бакеты")
    assert registry.get_installed("rag", "demo_rag")["version"] == "1.1.0"


# ─── Hashing area ─────────────────────────────────────────────────────────

def test_the_generated_manifest_is_outside_the_hashing_area(
    marketplace_data_dir, serve_catalog, tmp_path
):
    """``manifest.json`` and ``index.db`` are produced, not installed.

    Both are written by the platform: the manifest carries ``updated_at``,
    the index is rewritten by the first search. Hashing them would report a
    modification of a base nobody touched.
    """
    first, _package, index_v1 = _base_version(tmp_path, "1.0.0", texts=CHUNKS_V1)
    root = make_catalog(tmp_path / "catalog", [(first, _package)])
    _place_index(root, first, index_v1)
    base_url = serve_catalog(root)
    _install(base_url, first)

    from core.marketplace import conflicts, registry, updates

    row = registry.get_installed("rag", "demo_rag")
    assert "manifest.json" not in row["files_json"]
    assert "index.db" not in row["files_json"]

    state = conflicts.detect_local_modifications("rag", "demo_rag")
    assert state["modified"] is False
    assert updates.verify_installed("rag")["clean"] == 1


def _installed_folder(slug: str) -> str:
    """Return the folder of an installed base."""
    from core import rag

    return rag.base_dir(slug)
