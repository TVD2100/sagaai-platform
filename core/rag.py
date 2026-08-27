"""
core.rag - RAG knowledge base management (folder-based CRUD).

Each base lives in ``DATA_DIR/rag_bases/<slug>/``:

  manifest.json                   -- metadata (see RagBaseDict)
  files/                          -- uploaded source documents
  index.db                        -- local SQLite index (core.rag_index)

All data is stored locally; provider APIs are used only for embedding
vectorization (BYOK). The optional ``rag_slots`` list controls which
assistants/orchestrators may reference the base (anyone when empty).

The module also exposes chunk-level helpers used by the Storage UI and by
skills: list_chunks, get_chunk, update_chunk, delete_chunk.

No streamlit imports; errors raise ValueError with a user-facing message.
"""
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone

from core.fs import ensure_dir
from core.paths import RAG_BASES_DIR


VALID_STATUS = ("draft", "indexing", "ready", "error")
VALID_TYPES = ("rag",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9_\-\.]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "base"


def _manifest_path(slug: str) -> str:
    if not re.fullmatch(r"[a-z0-9_\-\.]+", slug) or slug in (".", ".."):
        raise ValueError("Invalid base slug")
    return os.path.join(RAG_BASES_DIR, slug, "manifest.json")


def base_dir(slug: str) -> str:
    """Return the folder of a base (raises on invalid slug)."""
    path = _manifest_path(slug)
    return os.path.dirname(path)


def files_dir(slug: str) -> str:
    """Return the files folder of a base."""
    return os.path.join(base_dir(slug), "files")


def index_db_path(slug: str) -> str:
    """Return the SQLite index path of a base."""
    return os.path.join(base_dir(slug), "index.db")


def _ensure_files_dir(slug: str) -> str:
    d = files_dir(slug)
    ensure_dir(d)
    return d


def list_bases() -> list:
    """Return manifest dicts for all bases in RAG_BASES_DIR."""
    result = []
    try:
        names = sorted(os.listdir(RAG_BASES_DIR))
    except FileNotFoundError:
        return result
    for name in names:
        path = os.path.join(RAG_BASES_DIR, name, "manifest.json")
        if not os.path.isfile(path):
            continue
        try:
            data = json_load(path)
        except Exception:
            continue
        if data.get("slug"):
            result.append(_with_index_stats(data))
    return result


def get_base(slug: str) -> dict:
    """Return manifest for *slug*; {} if missing."""
    path = _manifest_path(slug)
    if not os.path.isfile(path):
        return {}
    try:
        data = json_load(path)
    except Exception:
        return {}
    return _with_index_stats(data)


def _with_index_stats(data: dict) -> dict:
    """Attach lightweight index stats to a manifest dict."""
    try:
        from core.rag_index import index_stats
        out = dict(data)
        out.setdefault("index_stats", index_stats(index_db_path(data.get("slug", ""))))
        return out
    except Exception:
        return dict(data)


def _validate_create(name: str, description: str, provider: str,
                     embedding_model: str, chunk_size: int,
                     chunk_overlap: int) -> str:
    if not name.strip():
        raise ValueError("Base name cannot be empty")
    if not provider.strip():
        raise ValueError("Provider cannot be empty")
    if chunk_size < 20:
        raise ValueError("Chunk size is too small (min 20)")
    if chunk_overlap < 0:
        raise ValueError("Chunk overlap cannot be negative")
    if chunk_overlap >= chunk_size:
        raise ValueError("Chunk overlap must be smaller than chunk size")
    return _slugify(name)


def create_base(name: str, description: str = "", provider: str = "yandex",
                embedding_model: str = "text-search-doc",
                chunk_size: int = 1500, chunk_overlap: int = 150,
                type_: str = "rag", rag_slots=None) -> dict:
    """Create a folder + manifest + index for a new base.

    Returns the created manifest (with index stats). Rag *rag_slots* is an
    optional list of assistant/orchestrator identifiers allowed to reference
    the base; empty means "available to everyone".
    """
    if type_ not in VALID_TYPES:
        raise ValueError(f"Unsupported storage type: {type_}")
    slug = _validate_create(name, description, provider, embedding_model,
                            chunk_size, chunk_overlap)
    path = _manifest_path(slug)
    if os.path.exists(path):
        raise ValueError("A base with this slug already exists")
    slots = []
    for s in (rag_slots or []):
        clean = str(s or "").strip().lower()
        if clean and clean not in slots:
            slots.append(clean)
    now = _now()
    manifest = {
        "id": str(uuid.uuid4()),
        "slug": slug,
        "name": name.strip(),
        "description": description.strip(),
        "type": type_,
        "provider": provider.strip(),
        "embedding_model": embedding_model.strip(),
        "chunk_size": int(chunk_size),
        "chunk_overlap": int(chunk_overlap),
        "status": "draft",
        "created_at": now,
        "updated_at": now,
        "rag_slots": slots,
    }
    _save_manifest(slug, manifest)
    _ensure_files_dir(slug)
    from core.rag_index import create_index_db
    create_index_db(index_db_path(slug), dimension=256,
                    provider=manifest["provider"],
                    embedding_model=manifest["embedding_model"])
    return get_base(slug)


def update_base(slug: str, updates: dict) -> dict:
    """Update writable manifest fields and return the new manifest."""
    path = _manifest_path(slug)
    if not os.path.isfile(path):
        raise ValueError("Base does not exist")
    data = json_load(path)
    updateable = ("name", "description", "chunk_size", "chunk_overlap", "rag_slots")
    for key in updateable:
        if key not in updates:
            continue
        value = updates[key]
        if key in ("chunk_size", "chunk_overlap"):
            data[key] = int(value)
        elif key == "rag_slots":
            slots = []
            for s in (value or []):
                clean = str(s or "").strip().lower()
                if clean and clean not in slots:
                    slots.append(clean)
            data[key] = slots
        else:
            data[key] = str(value or "").strip()
    if data.get("chunk_size", 0) < 20:
        raise ValueError("Chunk size is too small")
    if data.get("chunk_overlap", 0) >= data.get("chunk_size", 1):
        raise ValueError("Chunk overlap must be smaller than chunk size")
    data["updated_at"] = _now()
    _save_manifest(slug, data)
    return get_base(slug)


def set_status(slug: str, status: str) -> dict:
    """Update the processing status and updated_at."""
    if status not in VALID_STATUS:
        raise ValueError(f"Invalid status: {status}")
    path = _manifest_path(slug)
    if not os.path.isfile(path):
        raise ValueError("Base does not exist")
    data = json_load(path)
    data["status"] = status
    data["updated_at"] = _now()
    _save_manifest(slug, data)
    return get_base(slug)


_DEFAULTS_MARKER_FILE = os.path.join(RAG_BASES_DIR, ".defaults_removed.json")


def _load_removed_defaults() -> list:
    """Return the list of removed default base markers."""
    try:
        with open(_DEFAULTS_MARKER_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _save_removed_defaults(markers: list) -> None:
    """Persist removed default base markers."""
    ensure_dir(RAG_BASES_DIR)
    with open(_DEFAULTS_MARKER_FILE, "w", encoding="utf-8") as f:
        json.dump(markers, f, ensure_ascii=False)


def _load_manifest_raw(slug: str) -> dict:
    """Read a base manifest WITHOUT index stats (for delete detection)."""
    path = _manifest_path(slug)
    if not os.path.isfile(path):
        return {}
    return json_load(path)


def delete_base(slug: str) -> bool:
    """Delete the base folder entirely.

    When the base was imported from the bundled defaults, its slug is
    recorded in the removed-defaults list so the default import does not
    resurrect a base the user deleted.
    """
    path = _manifest_path(slug)
    if not os.path.isfile(path):
        return False
    manifest = _load_manifest_raw(slug)
    source = str(manifest.get("source") or "")
    shutil.rmtree(base_dir(slug), ignore_errors=True)
    if source.startswith("defaults/"):
        markers = [m for m in _load_removed_defaults() if m != source]
        if source not in markers:
            markers.append(source)
        _save_removed_defaults(markers)
    return True


def add_file(slug: str, filename: str, content: bytes) -> dict:
    """Store an uploaded source file inside the base's files/ folder."""
    d = _ensure_files_dir(slug)
    safe_name = os.path.basename(filename or "").strip() or "file.txt"
    target = os.path.join(d, safe_name)
    with open(target, "wb") as f:
        f.write(content)
    return {"path": safe_name, "size": len(content)}


def remove_file(slug: str, filename: str) -> bool:
    """Remove a stored source file."""
    d = files_dir(slug)
    target = os.path.join(d, os.path.basename(filename or ""))
    if os.path.isfile(target):
        os.remove(target)
        return True
    return False


def list_files(slug: str) -> list:
    """Return ``[{path, size}]`` for stored source files."""
    d = files_dir(slug)
    if not os.path.isdir(d):
        return []
    result = []
    for name in sorted(os.listdir(d)):
        full = os.path.join(d, name)
        if os.path.isfile(full):
            result.append({"path": name, "size": os.path.getsize(full)})
    return result


def read_file_contents(slug: str, filename: str) -> str:
    """Read a stored source file as UTF-8 text."""
    d = files_dir(slug)
    target = os.path.join(d, os.path.basename(filename or ""))
    if not os.path.isfile(target):
        raise ValueError("File does not exist")
    try:
        with open(target, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(target, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def allowed_for_slot(slug: str, slot: str) -> bool:
    """True when base *slug* may be used by *slot* (assistant/orchestrator)."""
    data = get_base(slug)
    if not data:
        return False
    slots = data.get("rag_slots") or []
    return not slots or slot in slots


def base_has_credentials(slug: str) -> bool:
    """True when the base's embedding provider has API credentials configured.

    Reads the provider's ``config_key`` (and ``config_key2`` when declared)
    from the platform config. A missing key means the base cannot be
    vectorized and is therefore inactive; the UI shows a hint to connect the
    provider's API keys.
    """
    data = get_base(slug)
    provider = str(data.get("provider") or "").strip()
    if not provider:
        return False
    try:
        from core.services import get_services
        from core.config import load_config
        svc = get_services().get(provider, {})
        if not svc:
            return False
        cfg = load_config()
        ck1 = str(svc.get("config_key") or "").strip()
        if not ck1:
            return False
        val1 = cfg.get(ck1, "")
        if isinstance(val1, str):
            val1 = val1.strip()
        if not val1:
            return False
        ck2 = str(svc.get("config_key2") or "").strip()
        if ck2 and svc.get("require_folder_id", True):
            val2 = cfg.get(ck2, "")
            if isinstance(val2, str):
                val2 = val2.strip()
            if not val2:
                return False
        return True
    except Exception:
        return False


def list_chunks(slug: str, query: str = "", limit: int = 20,
                offset: int = 0) -> dict:
    """Return a page of chunks of base *slug* (optionally filtered by text).

    When *query* is non-empty the chunks are filtered by a case-insensitive
    substring match over their text. Returns ``{"total", "chunks"}`` where
    each chunk dict is ``{"chunk_id", "text", "source", "chunk_index",
    "created_at", "has_embedding"}``.
    """
    from core.rag_index import list_chunks as _list, search_chunks_text
    db = index_db_path(slug)
    if (query or "").strip():
        return search_chunks_text(db, query, limit=limit, offset=offset)
    return _list(db, limit=limit, offset=offset)


def get_chunk(slug: str, chunk_id: int) -> dict:
    """Return one chunk of base *slug* or {} when missing."""
    from core.rag_index import get_chunk as _get
    return _get(index_db_path(slug), chunk_id)


def update_chunk(slug: str, chunk_id: int, text: str,
                 reembed: bool = False) -> dict:
    """Update the text of one chunk; optionally re-embed it.

    The old embedding is always invalidated. When *reembed* is True the new
    text is embedded through the base's provider; on success the fresh
    vector is attached and the result contains ``"reembedded": True``. When
    re-embedding fails the chunk keeps its new text without a vector and the
    result contains ``"reembedded": False, "warning": <message>``.

    Returns ``{"ok": bool, "chunk": dict, "reembedded": bool,
    "warning": str}``.
    """
    from core.rag_index import (
        update_chunk_text, add_embedding, get_chunk as _get,
    )
    db = index_db_path(slug)
    if not update_chunk_text(db, chunk_id, text):
        return {"ok": False, "chunk": {}, "reembedded": False, "warning": ""}
    outcome = {"ok": True, "chunk": _get(db, chunk_id),
               "reembedded": False, "warning": ""}
    if not reembed:
        outcome["warning"] = "Chunk text saved; embedding was reset."
        return outcome
    base = get_base(slug)
    model = base.get("embedding_model") or "text-search-doc"
    try:
        from core.rag_embeddings import embed_text
        vector = embed_text(str(text), model=model)
    except Exception as e:
        outcome["warning"] = f"Re-embedding failed: {e}"
        return outcome
    if vector:
        if add_embedding(db, chunk_id, vector):
            outcome["reembedded"] = True
            outcome["chunk"] = _get(db, chunk_id)
            return outcome
    outcome["warning"] = "Re-embedding failed; chunk kept without a vector."
    return outcome


def delete_chunk(slug: str, chunk_id: int) -> bool:
    """Delete one chunk (and its embedding) of base *slug*."""
    from core.rag_index import delete_chunk as _del
    return _del(index_db_path(slug), chunk_id)


def list_bases_with_activity() -> list:
    """Return all bases with an extra ``active`` flag (credentials present)."""
    out = []
    for b in list_bases():
        b = dict(b)
        b["active"] = base_has_credentials(str(b.get("slug") or ""))
        out.append(b)
    return out


def _save_manifest(slug: str, data: dict) -> None:
    path = _manifest_path(slug)
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def json_load(path: str) -> dict:
    """Parse a JSON file into a dict ({} on any error)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
