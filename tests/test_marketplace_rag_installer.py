"""
test_marketplace_rag_installer.py - installing a ``rag`` entity.

A RAG base is a folder with a manifest, the source documents and a SQLite
index. These tests drive the real installer against a temporary DATA_DIR and
cover what is easy to get wrong: the index is checked before it replaces a
live base, a distribution that cannot work locally is refused with a code,
the sources mode builds the index on the spot, an update lands in the folder
the base already occupies, and a failure restores the previous base.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import (  # noqa: E402
    make_catalog,
    make_entity,
    marketplace_data_dir,  # noqa: F401 - fixture
    serve_catalog,
    sha256_hex,
)


#: Chunk texts of a small but realistic prebuilt index.
CHUNKS = [
    "Документ: Yandex Cloud. Продукт: Object Storage. Заголовок: Бакеты",
    "Документ: Yandex Cloud. Продукт: Object Storage. Заголовок: Политики доступа",
    "Документ: Yandex Cloud. Продукт: Managed Kubernetes. Заголовок: Кластеры",
]


def _make_index(path: Path, texts=None) -> Path:
    """Build an index database with *texts* and return its path."""
    from core import rag_bm25, rag_index

    rag_index.create_index_db(
        str(path), dimension=256, provider="YandexAI", embedding_model="text-search-doc"
    )
    for position, text in enumerate(texts or CHUNKS):
        rag_index.add_chunk(str(path), text, source="doc.md", chunk_index=position)
    rag_bm25.rebuild_index(str(path))
    return path


def _distribution(index_path: Path, **overrides) -> dict:
    """Return a ``prebuilt_index`` distribution block for *index_path*."""
    payload = index_path.read_bytes()
    block = {
        "mode": "prebuilt_index",
        "chunks": len(CHUNKS),
        "sources_count": 1,
        "index_sha256": sha256_hex(payload),
        "index_bytes": len(payload),
        "index_schema": 3,
        "embedding_model": "text-search-doc",
        "dimensions": 256,
    }
    block.update(overrides)
    return block


def _rag_entity(
    tmp_path,
    serve_catalog,
    *,
    entity_id="demo_rag",
    version="1.0.0",
    files=None,
    manifest=None,
    distribution=None,
    index_texts=None,
):
    """Serve a catalog holding one RAG base and return ``(entity, base_url)``.

    When the distribution ships a prebuilt index, the index is built first,
    hashed and copied into the served catalog folder, so the installer
    downloads exactly the bytes the manifest declares.
    """
    content = dict(files or {})
    declared_manifest = {
        "name": {"en": "Demo base", "ru": "Демо-база"},
        "description": {"en": "A base used by the tests", "ru": "База для тестов"},
        "provider": "YandexAI",
        "embedding_model": "text-search-doc",
        "chunk_size": 1800,
        "chunk_overlap": 0,
        "search_mode": "bm25",
        "rag_slots": [],
    }
    declared_manifest.update(manifest or {})
    extra = {"manifest": declared_manifest}

    index_path = None
    if distribution is not None:
        built = dict(distribution)
        if built.get("mode") == "prebuilt_index":
            index_path = _make_index(
                tmp_path / f"{entity_id}-{version}.db", index_texts
            )
            # A real catalog manifest declares the format of its index; the
            # installer refuses an index whose format it cannot describe.
            built.setdefault("index_schema", 3)
            built.setdefault("dimensions", 256)
            built.setdefault(
                "embedding_model", declared_manifest.get("embedding_model")
            )
            built.setdefault("chunks", len(index_texts or CHUNKS))
            built["index_sha256"] = sha256_hex(index_path.read_bytes())
            built["index_bytes"] = index_path.stat().st_size
            built["index_url"] = f"rag/{entity_id}/{version}/index.db"
        extra["distribution"] = built

    entity, package = make_entity(
        entity_id=entity_id,
        version=version,
        type_="rag",
        files=content,
        extra=extra,
    )
    root = make_catalog(tmp_path / "catalog", [(entity, package)])
    if index_path is not None:
        target = root / extra["distribution"]["index_url"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(index_path.read_bytes())
    return entity, serve_catalog(root)


def _source(base_url: str):
    """Return a catalog source bound to *base_url*."""
    from core.marketplace.source import MarketplaceSource

    return MarketplaceSource(base_url)


def _base_folder(slug: str) -> Path:
    """Return the folder of a base."""
    from core import rag

    return Path(rag.base_dir(slug))


def _served_root(tmp_path: Path, entity_id: str, version: str) -> Path:
    """Return the catalog folder of one served version."""
    return tmp_path / "catalog" / "rag" / entity_id / version


class TestPrebuiltIndex:
    def test_installs_the_folder_and_the_registry_row(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core import rag, rag_bm25
        from core.marketplace import registry
        from core.marketplace.rag_installer import install_rag

        entity, base_url = _rag_entity(
            tmp_path, serve_catalog, distribution={"mode": "prebuilt_index"}
        )
        report = install_rag(entity, channel="main", source=_source(base_url))

        assert report["ok"] is True
        assert report["runtime_slug"] == "demo_rag"
        assert report["distribution"] == "prebuilt_index"
        folder = _base_folder("demo_rag")
        assert (folder / "manifest.json").is_file()
        assert (folder / "index.db").is_file()
        assert rag_bm25.is_ready(str(folder / "index.db"))

        manifest = rag.get_base("demo_rag", with_stats=False)
        assert manifest["status"] == "ready"
        assert manifest["name"] == "Демо-база"
        assert manifest["marketplace"]["version"] == "1.0.0"
        assert manifest["search_mode"] == "bm25"

        row = registry.get_installed("rag", "demo_rag")
        assert row is not None
        assert row["index_sha256"] == entity["distribution"]["index_sha256"]
        assert row["version"] == "1.0.0"

    def test_second_install_of_the_same_version_is_a_noop(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.rag_installer import install_rag

        entity, base_url = _rag_entity(
            tmp_path, serve_catalog, distribution={"mode": "prebuilt_index"}
        )
        first = install_rag(entity, channel="main", source=_source(base_url))
        second = install_rag(entity, channel="main", source=_source(base_url))

        assert first["noop"] is False
        assert second["noop"] is True
        assert second["runtime_slug"] == first["runtime_slug"]

    def test_taken_slug_gets_a_suffix(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core.marketplace.rag_installer import install_rag

        folder = _base_folder("demo_rag")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text("{}", encoding="utf-8")

        entity, base_url = _rag_entity(
            tmp_path, serve_catalog, distribution={"mode": "prebuilt_index"}
        )
        report = install_rag(entity, channel="main", source=_source(base_url))

        assert report["runtime_slug"] == "demo_rag_2"

    def test_a_near_black_index_file_is_refused_while_staged(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        """A file that is not a usable index never reaches the live folder.

        The transport check only proves the bytes are what the manifest
        announced; a manifest can announce garbage. Reading the index is
        the check that keeps an unrunnable base off the disk.
        """
        from core.marketplace.installer import InstallError
        from core.marketplace.rag_installer import install_rag

        entity, base_url = _rag_entity(
            tmp_path, serve_catalog, distribution={"mode": "prebuilt_index"}
        )
        broken = b"this is not a database"
        (_served_root(tmp_path, "demo_rag", "1.0.0") / "index.db").write_bytes(broken)
        entity["distribution"].update(
            {
                "index_sha256": sha256_hex(broken),
                "index_bytes": len(broken),
                "chunks": 0,
            }
        )

        with pytest.raises(InstallError) as excinfo:
            install_rag(entity, channel="main", source=_source(base_url))

        assert excinfo.value.code in ("invalid_index", "empty_index")
        assert not _base_folder("demo_rag").exists()


class TestSourcesMode:
    def test_builds_the_index_from_the_source_files(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core import rag, rag_index
        from core.marketplace.rag_installer import install_rag

        entity, base_url = _rag_entity(
            tmp_path,
            serve_catalog,
            files={"files/doc.md": "# Бакеты\n\nПолитика доступа к бакету.\n"},
            distribution={"mode": "sources_only"},
        )
        report = install_rag(entity, channel="main", source=_source(base_url))

        assert report["ok"] is True
        # A locally built index is still recorded by its hash: it makes the
        # installed state reproducible even though no catalog bytes produced it.
        assert report["index_sha256"] and len(report["index_sha256"]) == 64
        folder = _base_folder("demo_rag")
        assert (folder / "files" / "doc.md").is_file()
        index_path = folder / "index.db"
        assert index_path.is_file()
        assert rag_index.count_chunks(str(index_path)) > 0
        assert rag.get_base("demo_rag", with_stats=False)["status"] == "ready"

    def test_an_update_keeps_the_folder_and_drops_vanished_files(
        self, marketplace_data_dir, serve_catalog, tmp_path
    ):
        from core import rag
        from core.marketplace import registry
        from core.marketplace.rag_installer import install_rag

        entity_v1, url_v1 = _rag_entity(
            tmp_path / "v1",
            serve_catalog,
            files={
                "files/a.md": "# А\n\nПервый документ базы.\n",
                "files/b.md": "# Б\n\nВторой документ базы.\n",
            },
            distribution={"mode": "sources_only"},
        )
        install_rag(entity_v1, channel="main", source=_source(url_v1))
        folder = _base_folder("demo_rag")
        base_id = rag.get_base("demo_rag", with_stats=False)["id"]

        entity_v2, url_v2 = _rag_entity(
            tmp_path / "v2",
            serve_catalog,
            version="1.1.0",
            files={"files/b.md": "# Б\n\nВторой документ базы, обновлённый.\n"},
            distribution={"mode": "sources_only"},
        )
        report = install_rag(entity_v2, channel="main", source=_source(url_v2))

        assert report["ok"] is True
        assert report["runtime_slug"] == "demo_rag"
        assert not (folder / "files" / "a.md").exists()
        assert (folder / "files" / "b.md").is_file()
        manifest = rag.get_base("demo_rag", with_stats=False)
        assert manifest["id"] == base_id
        assert manifest["marketplace"]["version"] == "1.1.0"
        assert registry.get_installed("rag", "demo_rag")["version"] == "1.1.0"

    def test_a_failed_write_restores_the_previous_base(
        self, marketplace_data_dir, serve_catalog, tmp_path, monkeypatch
    ):
        """A run that dies after copying leaves the old base intact.

        The registry write is the last step of a run; failing it must undo
        the files the run already replaced and remove the ones it added,
        so the base keeps working at its previous version.
        """
        from core.marketplace import registry
        from core.marketplace.installer import InstallError
        from core.marketplace.rag_installer import install_rag

        entity_v1, url_v1 = _rag_entity(
            tmp_path / "v1",
            serve_catalog,
            files={
                "files/a.md": "# А\n\nПервый документ базы.\n",
                "files/b.md": "# Б\n\nВторой документ базы.\n",
            },
            distribution={"mode": "sources_only"},
        )
        install_rag(entity_v1, channel="main", source=_source(url_v1))
        folder = _base_folder("demo_rag")
        old_b = (folder / "files" / "b.md").read_text(encoding="utf-8")

        entity_v2, url_v2 = _rag_entity(
            tmp_path / "v2",
            serve_catalog,
            version="1.1.0",
            files={
                "files/b.md": "# Б\n\nСовсем другой текст.\n",
                "files/c.md": "# В\n\nНовый документ.\n",
            },
            distribution={"mode": "sources_only"},
        )

        def _boom(_record):
            raise RuntimeError("registry is unavailable")

        monkeypatch.setattr(registry, "upsert_installed", _boom)
        with pytest.raises(InstallError) as excinfo:
            install_rag(entity_v2, channel="main", source=_source(url_v2))
        assert excinfo.value.code == "install_failed"

        assert (folder / "files" / "b.md").read_text(encoding="utf-8") == old_b
        assert (folder / "files" / "a.md").is_file()
        assert not (folder / "files" / "c.md").exists()
        assert registry.get_installed("rag", "demo_rag")["version"] == "1.0.0"


class TestDistributionChecks:
    def test_index_without_a_hash_is_refused(self, marketplace_data_dir, tmp_path):
        from core.marketplace.installer import InstallError
        from core.marketplace.rag_installer import check_distribution

        entity = {
            "id": "demo_rag",
            "distribution": {
                "mode": "prebuilt_index",
                "index_url": "rag/demo_rag/1.0.0/index.db",
            },
        }
        with pytest.raises(InstallError) as excinfo:
            check_distribution(entity)
        assert excinfo.value.code == "missing_index_hash"

    def test_index_without_a_schema_is_refused(self, marketplace_data_dir, tmp_path):
        from core.marketplace.installer import InstallError
        from core.marketplace.rag_installer import check_distribution

        block = _distribution(_make_index(tmp_path / "probe.db"))  # type: ignore[arg-type]
        block.pop("index_schema")
        block["index_url"] = "rag/demo_rag/1.0.0/index.db"
        with pytest.raises(InstallError) as excinfo:
            check_distribution({"id": "demo_rag", "distribution": block})
        assert excinfo.value.code == "missing_index_schema"

    def test_index_of_another_schema_is_refused(self, marketplace_data_dir, tmp_path):
        from core.marketplace.installer import InstallError
        from core.marketplace.rag_installer import check_distribution

        block = _distribution(_make_index(tmp_path / "probe.db"), index_schema=2)
        block["index_url"] = "rag/demo_rag/1.0.0/index.db"
        with pytest.raises(InstallError) as excinfo:
            check_distribution({"id": "demo_rag", "distribution": block})
        assert excinfo.value.code == "index_schema_mismatch"

    def test_index_of_another_dimension_is_refused(self, marketplace_data_dir, tmp_path):
        from core.marketplace.installer import InstallError
        from core.marketplace.rag_installer import check_distribution

        block = _distribution(_make_index(tmp_path / "probe.db"), dimensions=1024)
        block["index_url"] = "rag/demo_rag/1.0.0/index.db"
        with pytest.raises(InstallError) as excinfo:
            check_distribution({"id": "demo_rag", "distribution": block})
        assert excinfo.value.code == "index_dimension_mismatch"

    def test_unknown_mode_is_refused(self, marketplace_data_dir):
        from core.marketplace.installer import InstallError
        from core.marketplace.rag_installer import check_distribution

        with pytest.raises(InstallError) as excinfo:
            check_distribution({"id": "demo_rag", "distribution": {"mode": "mystery"}})
        assert excinfo.value.code == "invalid_distribution"

    def test_vector_base_without_its_model_is_refused(self, marketplace_data_dir, tmp_path):
        from core.marketplace.installer import InstallError
        from core.marketplace.rag_installer import check_distribution

        block = _distribution(_make_index(tmp_path / "probe.db"), embedding_model="")
        block["index_url"] = "rag/demo_rag/1.0.0/index.db"
        entity = {
            "id": "demo_rag",
            "manifest": {"search_mode": "vector"},
            "distribution": block,
        }
        with pytest.raises(InstallError) as excinfo:
            check_distribution(entity)
        assert excinfo.value.code == "missing_embedding_model"


def test_the_installer_dispatch_reaches_the_rag_branch(
    marketplace_data_dir, serve_catalog, tmp_path
):
    """The page calls one entry point; it must route a base to its installer."""
    from core.marketplace import registry
    from core.marketplace.installer import install_entity

    entity, base_url = _rag_entity(
        tmp_path, serve_catalog, distribution={"mode": "prebuilt_index"}
    )
    report = install_entity(entity, channel="main", source=_source(base_url))

    assert report["ok"] is True
    assert report["type"] == "rag"
    assert registry.get_installed("rag", "demo_rag") is not None
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
