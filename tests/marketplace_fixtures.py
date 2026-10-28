"""
marketplace_fixtures - shared builders for marketplace tests.

Every marketplace test needs the same three things: a temporary DATA_DIR,
a catalog on disk served over a local HTTP server, and a skill package with
a matching manifest. Building them here keeps the individual test modules
focused on behaviour instead of plumbing, and guarantees that the installer
and the conflict resolver are exercised against exactly the same entity
shape.

Nothing in this module talks to the public network: ``make_catalog`` writes
a folder tree and ``serve_catalog`` starts an ``http.server`` on a loopback
port, the same technique ``tests/test_updater.py`` uses.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import socket
import sys
import tarfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Data directory isolation ─────────────────────────────────────────────

@pytest.fixture
def marketplace_data_dir(tmp_path):
    """Point every data path at a temporary folder for one test.

    The platform resolves ``DATA_DIR`` through ``core.paths``, and several
    modules cache derived paths at import time, so the fixture reloads
    ``core.paths`` and ``core.rag`` and resets the DB engines - exactly the
    pattern ``tests/test_default_imports.py`` established. Without the reset
    the first test would leave a live SQLAlchemy engine pointing at the real
    ``sagaai.db`` and later tests would write into the developer's data.
    """
    import importlib

    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = str(tmp_path)

    import core.paths as paths_mod

    attrs = ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
             "SYSTEM_PROMPTS_DIR", "RAG_BASES_DIR", "PERSONAL_DIR")
    old_attrs = {attr: getattr(paths_mod, attr, None) for attr in attrs}

    paths_mod.DATA_DIR = str(tmp_path)
    paths_mod.DB_PATH = os.path.join(str(tmp_path), "sagaai.db")
    paths_mod.DEVAGENT_DB_PATH = os.path.join(str(tmp_path), "devagent.db")
    paths_mod.HISTORY_DIR = os.path.join(str(tmp_path), "history")
    paths_mod.SYSTEM_PROMPTS_DIR = os.path.join(str(tmp_path), "system_prompts")
    paths_mod.RAG_BASES_DIR = os.path.join(str(tmp_path), "rag_bases")
    paths_mod.PERSONAL_DIR = os.path.join(str(tmp_path), "personal")

    import core.rag as rag_mod
    importlib.reload(rag_mod)

    import storage.db as db_mod
    db_mod.DB_PATH = paths_mod.DB_PATH
    db_mod.DEVAGENT_DB_PATH = paths_mod.DEVAGENT_DB_PATH

    from storage.db import reset_engine, reset_devagent_engine
    reset_engine()
    reset_devagent_engine()

    yield tmp_path

    reset_engine()
    reset_devagent_engine()

    if old_env is not None:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    for attr, value in old_attrs.items():
        if value is not None:
            setattr(paths_mod, attr, value)
    importlib.reload(rag_mod)


# ─── Entity and package builders ──────────────────────────────────────────

def sha256_hex(data: bytes) -> str:
    """Return the hex sha256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def default_files(slug: str = "demo_skill") -> Dict[str, str]:
    """Return the file contents of a small but realistic skill.

    Includes ``scripts/run.py`` so ``allow_code`` has something to gate and
    ``references/notes.md`` so a package has more than one file.
    """
    return {
        "SKILL.md": f"# {slug}\n\nInstructions for the demo skill.\n",
        "references/notes.md": "Reference material.\n",
        "scripts/run.py": "print('demo')\n",
    }


def make_package_tar(files: Dict[str, str]) -> bytes:
    """Build a tar.gz archive from ``{relative path: text}`` and return bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for rel_path, content in sorted(files.items()):
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def make_entity(
    entity_id: str = "demo_skill",
    version: str = "1.0.0",
    files: Optional[Dict[str, str]] = None,
    *,
    type_: str = "skill",
    name: Optional[str] = None,
    package_url: Optional[str] = None,
    license_spdx: str = "PolyForm-Noncommercial-1.0.0",
    license_acceptance_required: bool = False,
    requires_tools: Optional[List[str]] = None,
    executes_code: bool = True,
    platform: Optional[dict] = None,
    runtime_slug: Optional[str] = None,
    extra: Optional[dict] = None,
) -> Tuple[dict, bytes]:
    """Build an entity manifest and its package, returning ``(entity, bytes)``.

    The manifest follows the catalog schema: ``schema``, ``id``, ``version``,
    ``name``/``description`` keyed by language, ``files`` with per-file
    sha256, ``package`` with url/sha256/size and optional ``license`` and
    ``platform`` blocks. The returned bytes are the tar.gz the manifest
    describes, so ``make_catalog`` can write both without rebuilding the
    package and drifting from its hash.
    """
    content = files if files is not None else default_files(entity_id)
    package_bytes = make_package_tar(content)
    package_sha = sha256_hex(package_bytes)
    language_name = name or entity_id.replace("_", " ").title()
    from core.marketplace.source import type_folder
    entity = {
        "schema": "sagaai.marketplace.entity/1",
        "type": type_,
        "id": entity_id,
        "version": version,
        "name": {"en": language_name, "ru": f"Демонстрационный навык {entity_id}" if type_ == "skill" else language_name, "zh-CN": language_name},
        "description": {
            "en": f"Demo skill {entity_id}",
            "ru": f"Демонстрационный навык {entity_id}",
            "zh-CN": f"Demo skill {entity_id}",
        },
        "license": {
            "spdx": license_spdx,
            "acceptance_required": bool(license_acceptance_required),
        },
        "permissions": {"executes_code": bool(executes_code)},
        "requires_tools": list(requires_tools or []),
        "files": {path: sha256_hex(text.encode("utf-8")) for path, text in content.items()},
        "package": {
            "sha256": package_sha,
            "size": len(package_bytes),
            "url": package_url or f"{type_folder(type_)}/{entity_id}/{version}/package.tar.gz",
        },
        "source_url": f"{type_folder(type_)}/{entity_id}/{version}",
    }
    if runtime_slug:
        entity["runtime_slug"] = runtime_slug
    if platform:
        entity["platform"] = platform
    if extra:
        entity.update(extra)
    return entity, package_bytes


def make_catalog(
    root: Path,
    entities: Optional[List[Tuple[dict, bytes]]] = None,
    bundles: Optional[List[dict]] = None,
) -> Path:
    """Write a catalog tree under *root* and return it.

    Layout matches the production repository: ``index.json`` plus
    ``skills/<id>/<version>/{entity.json,package.tar.gz}``. The index lists
    every entity so a test can exercise both the index and the per-entity
    manifest path.
    """
    root.mkdir(parents=True, exist_ok=True)
    from core.marketplace.source import type_folder

    entries = []
    for entity, package_bytes in entities or [make_entity()]:
        type_ = str(entity.get("type") or "skill")
        entity_id = str(entity["id"])
        version = str(entity["version"])
        folder = root / type_folder(type_) / entity_id / version
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "entity.json").write_text(
            json.dumps(entity, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (folder / "package.tar.gz").write_bytes(package_bytes)
        latest = root / type_folder(type_) / entity_id / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        (latest / "entity.json").write_text(
            json.dumps(entity, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        entries.append({
            "type": type_,
            "id": entity_id,
            "version": version,
            "agent_kind": (
                str(entity.get("agent_kind") or "orchestrator") if type_ == "agent" else ""
            ),
            "name": entity.get("name"),
            "description": entity.get("description"),
            "path": f"{type_folder(type_)}/{entity_id}/{version}",
        })
    index = {
        "schema": "sagaai.marketplace.index/1",
        "updated_at": "2026-10-27T10:00:00Z",
        "entities": entries,
    }
    if bundles:
        write_bundles(root, bundles, index=index)
    (root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def write_bundles(
    root: Path,
    bundles: List[dict],
    *,
    index: Optional[dict] = None,
) -> Path:
    """Write ``bundles/<id>.json`` records and list them in the index.

    Called by :func:`make_catalog` for a catalog built in one go, and
    directly by a test that adds a bundle to a catalog it already served.
    """
    folder = root / "bundles"
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for record in bundles:
        bundle_id = str(record.get("id") or "").strip()
        if not bundle_id:
            raise ValueError("a bundle needs an id")
        payload = {"schema": "sagaai.marketplace.bundle/1"}
        payload.update(record)
        (folder / f"{bundle_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        entries.append({
            "id": bundle_id,
            "name": str(record.get("name") or bundle_id),
            "path": f"bundles/{bundle_id}.json",
        })
    persist = index is None
    if persist:
        index_path = root / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
    index["bundles"] = entries
    if persist:
        (root / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return root


def add_catalog_version(root: Path, entity: dict, package_bytes: bytes) -> Path:
    """Add one more version of an entity to an existing catalog.

    Updates the versioned folder, the ``latest`` pointer and the index list
    so ``check_entity_updates`` sees a genuine newer release.
    """
    type_ = str(entity.get("type") or "skill")
    entity_id = str(entity["id"])
    version = str(entity["version"])
    from core.marketplace.source import type_folder

    folder = root / type_folder(type_) / entity_id / version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "entity.json").write_text(
        json.dumps(entity, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "package.tar.gz").write_bytes(package_bytes)
    latest = root / type_folder(type_) / entity_id / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "entity.json").write_text(
        json.dumps(entity, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    index_path = root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = [e for e in index.get("entities") or []
               if not (e.get("type") == type_ and e.get("id") == entity_id)]
    entries.append({
        "type": type_,
        "id": entity_id,
        "version": version,
        "agent_kind": (
            str(entity.get("agent_kind") or "orchestrator") if type_ == "agent" else ""
        ),
        "name": entity.get("name"),
        "description": entity.get("description"),
        "path": f"{type_folder(type_)}/{entity_id}/{version}",
    })
    index["entities"] = entries
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


# ─── Local HTTP channel ───────────────────────────────────────────────────

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """``SimpleHTTPRequestHandler`` without the per-request log line."""

    def log_message(self, format, *args):  # noqa: A002 - signature from stdlib
        return


@pytest.fixture
def serve_catalog(tmp_path):
    """Return a factory that serves a directory over loopback and its URL.

    The server runs in a daemon thread on an ephemeral port and is stopped
    on teardown, so tests never leave a listening socket behind. Requests
    go to ``127.0.0.1`` only, which the source treats as a local channel and
    exempts from the domain allowlist.
    """
    servers = []

    def _start(directory: Path) -> str:
        handler = lambda *args, **kwargs: _QuietHandler(  # noqa: E731
            *args, directory=str(directory), **kwargs
        )
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append((httpd, thread))
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start

    for httpd, thread in servers:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def free_port() -> int:
    """Return a port that is free right now (used for negative tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ─── Misc helpers ─────────────────────────────────────────────────────────

def read_json_file(path) -> dict:
    """Read a JSON file and return its content as a dict."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def make_installed_row(
    entity_id: str,
    *,
    type_: str = "skill",
    version: str = "1.0.0",
    runtime_slug: Optional[str] = None,
    user_modified: bool = False,
    skipped_code_files: Optional[List[str]] = None,
) -> dict:
    """Register an installed entity directly in the marketplace registry.

    Some tests need an installation state without paying for a full install
    run - for example the showcase, which only reads the registry. The row
    is written through the public ``upsert_installed`` so it is shaped by
    the same coercion as a real install.
    """
    from core.marketplace import registry

    slug = runtime_slug or entity_id
    record = {
        "type": type_,
        "id": entity_id,
        "runtime_slug": slug,
        "version": version,
        "channel": "main",
        "source_url": f"{type_}/{entity_id}/{version}",
        "package_sha256": "0" * 64,
        "tree_sha256": "0" * 64,
        "files_json": {},
        "skipped_code_files_json": list(skipped_code_files or []),
        "installed_at": "2026-10-27T10:00:00Z",
        "updated_at": "2026-10-27T10:00:00Z",
        "installed_by": "marketplace",
        "install_id": "20261027T100000Z-aaaa",
        "user_modified": bool(user_modified),
        "user_modified_files_json": [],
    }
    registry.upsert_installed(record)
    return record


def list_files_under(root) -> Dict[str, bytes]:
    """Return ``{relative path: bytes}`` for every file under *root*."""
    result: Dict[str, bytes] = {}
    base = Path(root)
    if not base.is_dir():
        return result
    for path in sorted(base.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(base)).replace(os.sep, "/")] = path.read_bytes()
    return result
