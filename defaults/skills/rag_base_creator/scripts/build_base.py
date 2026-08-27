#!/usr/bin/env python3
"""build_base.py - RAG Base Creator skill helper.

Builds a SagaAI RAG knowledge base from local text files with custom
chunking rules:

  * ``index.md`` files (and any ``--exclude`` names) are skipped;
  * files <= ``chunk_size`` characters become ONE chunk;
  * larger files are split into self-contained parts via
    ``core.rag_chunker``;
  * every chunk is prefixed with a self-describing header so a reader
    immediately understands what document the piece belongs to, which
    product section it covers, and which part of the whole file it is:

        Документ: docs/ai-studio/api-ref/authentication_in_yandex_ai_studio_api.md
        Продукт: AI Studio
        Заголовок: Authentication in Yandex AI Studio API
        Раздел: <H2 → H3 trail when the file was split>
        Фрагмент: 1/1 — документ целиком  |  Фрагмент: 2/5

Chunks are embedded offline with the YandexAI embeddings API, then inserted
into the base's local SQLite index. ``--dry-run`` only prints a JSON plan and
touches nothing; ``--limit N`` limits processed files (useful for trials).

Usage:

    python build_base.py --install-root <ROOT> --source <DIR_OR_FILE> \\
        --name "My KB" [--slug my_kb] [--exclude index.md] \\
        [--chunk-size 1800] [--chunk-overlap 0] [--limit N]

Options:
    --install-root ROOT    SagaAI install root (contains core/).
    --source PATH          Source directory or file (repeatable).
    --name NAME            Base display name.
    --description TEXT     Optional description.
    --slug SLUG            Optional stable slug.
    --chunk-size N         Max chunk length in chars (default 1800).
    --chunk-overlap N      Overlap in chars (default 0).
    --exclude NAME         File name(s) to skip (repeatable).
    --limit N              Process only the first N matched files.
    --slots S              Rag_slots identifier (repeatable).
    --dry-run              Only plan; print JSON and exit 0.
    --no-embed             Insert chunks without embeddings.

Exit code 0 on success, 1 on error.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections import OrderedDict


# Hard dependency list, checked at startup so the agent can report a
# meaningful error when the script is run from outside the install.
_HARD_DEPS = ("core.rag", "core.rag_index", "core.rag_chunker",
              "core.rag_embeddings", "core.rag_search")

# Default source extensions.
_DEFAULT_EXTS = (".md", ".txt")

# Default chunk size: Yandex text-search embeddings accept up to ~1024
# tokens (roughly 2000 Russian characters); 1800 leaves a safe margin.
_DEFAULT_CHUNK_SIZE = 1800

# Maps the first path component under docs/ to a human-readable product
# name used in chunk headers. Unknown components pass through as-is.
_PRODUCT_ALIASES = {
    "ai-studio": "AI Studio",
    "speechkit": "SpeechKit",
    "speechsense": "SpeechSense",
    "search-api": "Search API",
    "vision": "Vision",
    "translate": "Translate",
    "speechkit-hybrid": "SpeechKit Hybrid",
}

# Headings H1-H3 used to attach a "Раздел" trail to every split part.
_HEADING_RE = re.compile(r"(?m)^(#{1,3})\s+(.+?)\s*$")

# Fallback pseudo embeddings for --no-embed runs and tests. Pure cosine
# queries without vectors are irrelevant, so keeping everything numerically
# distinct is enough to exercise the full pipeline without YandexAI.
_DEFAULT_DIM = 256


def _fallback_vector(text: str, dim: int = _DEFAULT_DIM) -> list:
    """Return a deterministic pseudo-embedding for *text*."""
    rnd = random.Random(hash(text.lower().strip()) & 0xFFFFFFFF)
    vec = [rnd.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


def _check_deps(install_root: str) -> None:
    """Verify that *install_root* points at a SagaAI install."""
    if not install_root:
        sys.exit("ERROR: --install-root is required (SagaAI install root)")
    if not os.path.isdir(install_root):
        sys.exit(f"ERROR: install root not found: {install_root}")
    if not os.path.isdir(os.path.join(install_root, "core")):
        sys.exit(f"ERROR: not a SagaAI install root (missing core/): {install_root}")
    sys.path.insert(0, install_root)
    try:
        import core.paths  # noqa: F401
    except Exception as e:
        sys.exit(f"ERROR: cannot import core.paths from {install_root}: {e}")


def _collect_files(sources, excludes=(), limit=None, extensions=_DEFAULT_EXTS):
    """Walk *sources* and return an ordered dict {rel_path: abs_path}.

    Files whose name is in *excludes* are dropped; only *extensions* are
    kept. *limit* (if set) truncates the collection to the first N files by
    relative-path order.
    """
    found = OrderedDict()
    exclude_set = {e.strip().lower() for e in excludes}
    for source in sources:
        source = source.strip()
        if not source:
            continue
        if os.path.isfile(source):
            base = os.path.abspath(os.path.dirname(source))
            _add_file(source, base, exclude_set, extensions, found)
            continue
        base = os.path.abspath(source)
        if not os.path.isdir(base):
            print(json.dumps({"error": f"source missing: {source}"}),
                  file=sys.stderr)
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                full = os.path.join(root, name)
                _add_file(full, base, exclude_set, extensions, found)
    items = list(found.items())
    if limit is not None:
        items = items[: int(limit)]
    return OrderedDict(items)


def _add_file(full, base, exclude_set, extensions, found):
    """Add *full* to *found* when it matches include/exclude filters."""
    if not os.path.isfile(full):
        return
    name = os.path.basename(full)
    if name.lower() in exclude_set:
        return
    ext = os.path.splitext(name)[1].lower()
    if ext not in extensions:
        return
    rel = os.path.relpath(full, base)
    if rel not in found:
        found[rel] = full


def _read_text(path: str) -> str:
    """Read a file as UTF-8 text with a binary-safe fallback."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _product_of(rel_path: str) -> str:
    """Return the human-readable product name for a relative path."""
    rel_path = str(rel_path or "").replace("\\", "/")
    parts = [p for p in rel_path.split("/") if p]
    if parts and parts[0] == "docs" and len(parts) >= 2:
        return _PRODUCT_ALIASES.get(parts[1], parts[1])
    return "YaAgentAI"


def _headings(text: str) -> list:
    """Return [(level, title, offset)] for H1-H3 headings in *text*."""
    return [
        (len(m.group(1)), m.group(2).strip(), m.start())
        for m in _HEADING_RE.finditer(text)
    ]


def _trail_at(heads: list, h1: str, offset: int) -> str:
    """Return the nearest H2 → H3 trail active at *offset*.

    ``""`` when there is no useful subsection context.
    """
    cur = {1: None, 2: None, 3: None}
    for level, title, start in heads:
        if start > offset:
            break
        cur[level] = title
        if level == 1:
            cur[2] = None
            cur[3] = None
        elif level == 2:
            cur[3] = None
    chain = [cur[2], cur[3]]
    return " → ".join(t for t in chain if t and t != h1)


def _split_units(text: str, size: int) -> list:
    """Split *text* into paragraph/sentence/word units of at most *size*.

    Uses ``core.rag_chunker`` when available and falls back to a local
    paragraph splitter. Returns a list of strings.
    """
    try:
        from core.rag_chunker import _split_units as _chunker_units
        units = _chunker_units(text, size)
        if units:
            return units
    except Exception:
        pass
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _group_units(text: str, size: int) -> list:
    """Group paragraph-level units into (start_offset, part_text) buckets.

    The bucketing is greedy over the source text in order, so offsets stay
    monotonic and every part fits comfortably within *size* characters.
    """
    units = []
    pos = 0
    for para in re.split(r"\n\s*\n+", text):
        para = para.strip()
        if not para:
            pos += 2
            continue
        start = text.find(para, pos)
        if start < 0:
            start = pos
        pos = start + len(para)
        for unit in _split_units(para, size):
            units.append((start, unit))
    groups, buf, chunk_start, chars = [], [], 0, 0
    for off, unit in units:
        candidate = chars + len(unit) + (1 if buf else 0)
        if buf and candidate > size:
            groups.append((chunk_start, "\n".join(buf)))
            buf, chars, chunk_start = [unit], len(unit), off
        else:
            if not buf:
                chunk_start = off
            buf.append(unit)
            chars = candidate
    if buf:
        groups.append((chunk_start, "\n".join(buf)))
    return groups


def build_chunks(text: str, rel_path: str, chunk_size: int,
                 chunk_overlap: int) -> list:
    """Split file *text* into self-describing chunks.

    Every chunk starts with a header block:

        Документ: <relative path>
        Продукт: <product name>
        Заголовок: <first H1 or file name>
        [Раздел: <H2 → H3 trail>]        # only for split files
        Фрагмент: <N/M> [— документ целиком]

    Files that fit within *chunk_size* (including the header) become one
    chunk. Larger files are split at paragraph boundaries into parts of
    about *chunk_size* characters minus the header, each tagged with the
    section trail of its source position.
    """
    text = str(text or "").strip()
    if not text:
        return []
    heads = _headings(text)
    h1 = next((t for lv, t, _ in heads if lv == 1), os.path.basename(rel_path))
    fixed = (
        f"Документ: {rel_path}\n"
        f"Продукт: {_product_of(rel_path)}\n"
        f"Заголовок: {h1}\n"
    )
    if len(fixed) + len(text) + 40 <= chunk_size:
        return [fixed + f"Фрагмент: 1/1 — документ целиком\n\n{text}"]
    budget = max(200, chunk_size - len(fixed) - 90)
    groups = _group_units(text, budget)
    total = len(groups)
    chunks = []
    for idx, (start_off, part) in enumerate(groups, start=1):
        trail = _trail_at(heads, h1, start_off)
        trail_line = f"Раздел: {trail}\n" if trail else ""
        chunks.append(fixed + trail_line + f"Фрагмент: {idx}/{total}\n\n{part}")
    return chunks


def _embed_chunk(text: str, model: str, no_embed: bool):
    """Return the embedding vector for *text* (or a fallback vector)."""
    if no_embed:
        return _fallback_vector(text)
    from core.rag_embeddings import embed_text
    return embed_text(text, model=model)


def run(args) -> dict:
    """Execute the build per parsed *args* and return a result dict."""
    from core import rag
    from core.rag_index import add_chunk

    ext_tuple = tuple(args.extensions) if args.extensions else _DEFAULT_EXTS
    # index.md is always excluded; user exclusions are merged with it.
    exclude_values = list(dict.fromkeys(["index.md"] + list(args.exclude)))
    files = _collect_files(args.source, excludes=exclude_values,
                           limit=args.limit, extensions=ext_tuple)
    plan = {
        "sources": list(args.source),
        "chunk_size": args.chunk_size,
        "chunk_overlap": args.chunk_overlap,
        "excludes": exclude_values,
        "limit": args.limit,
        "files_total": len(files),
        "files": [],
    }
    total_chunks = 0
    for rel, full in files.items():
        text = _read_text(full)
        chunks = build_chunks(text, rel, args.chunk_size, args.chunk_overlap)
        plan["files"].append({
            "path": rel,
            "chars": len(text),
            "chunks": len(chunks),
        })
        total_chunks += len(chunks)
    plan["chunks_total"] = total_chunks

    if args.dry_run:
        return {"dry_run": True, "plan": plan}

    slots = []
    for s in (args.slots or []):
        clean = str(s or "").strip().lower()
        if clean and clean not in slots:
            slots.append(clean)

    existing = rag.get_base(args.slug) if args.slug else {}
    if existing and not args.replace:
        return {
            "ok": False,
            "error": f"base already exists: {args.slug}",
            "hint": "choose a different --slug or pass --replace",
        }
    if existing and args.replace:
        rag.delete_base(args.slug)

    base = rag.create_base(
        name=args.name,
        description=args.description or "",
        provider="YandexAI",
        embedding_model="text-search-doc",
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        rag_slots=slots,
    )
    slug = base["slug"]
    db = rag.index_db_path(slug)

    inserted = 0
    warnings = []
    for rel, full in files.items():
        text = _read_text(full)
        chunks = build_chunks(text, rel, args.chunk_size, args.chunk_overlap)
        for idx, chunk in enumerate(chunks):
            try:
                vector = _embed_chunk(chunk, "text-search-doc", args.no_embed)
            except Exception as e:
                warnings.append(f"{rel}[{idx}]: embedding failed: {e}")
                vector = []
            add_chunk(db, chunk, source=rel, chunk_index=idx,
                      vector=vector or None)
            inserted += 1

    if inserted == 0:
        rag.set_status(slug, "error")
        return {"ok": False, "slug": slug, "status": "error",
                "chunks": 0, "warnings": warnings}
    rag.set_status(slug, "ready")
    result = {"ok": True, "slug": slug, "status": "ready",
              "chunks": inserted, "warnings": warnings}
    try:
        from core.rag_search import search_base
        hits = search_base(slug, "main features", top_k=3)
        result["sample_hits"] = [
            {"source": h.get("source"), "score": round(float(h.get("score", 0)), 3)}
            for h in hits
        ]
    except Exception as e:
        result["sample_hits_error"] = str(e)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a SagaAI RAG knowledge base from local text files."
    )
    parser.add_argument("--install-root", required=True,
                        help="SagaAI install root (contains core/)")
    parser.add_argument("--source", action="append", required=True,
                        help="Source directory or file (repeatable)")
    parser.add_argument("--name", required=True, help="Base display name")
    parser.add_argument("--description", default="", help="Base description")
    parser.add_argument("--slug", default="", help="Stable base slug")
    parser.add_argument("--chunk-size", type=int, default=_DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=0)
    parser.add_argument("--exclude", action="append", default=["index.md"],
                        help="File names to skip (repeatable; merged with the default index.md)")
    parser.add_argument("--extensions", action="append", default=[],
                        help="Include extra file extensions (repeatable)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N matched files")
    parser.add_argument("--slots", action="append", default=[],
                        help="rag_slots identifier (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only plan; create nothing and exit 0")
    parser.add_argument("--no-embed", action="store_true",
                        help="Insert chunks without embeddings (draft-ish trial)")
    parser.add_argument("--replace", action="store_true",
                        help="Replace the base when the slug already exists")
    args = parser.parse_args(argv)

    # Validate early; print JSON, no stack traces.
    if args.chunk_size < 20:
        sys.exit("ERROR: --chunk-size must be >= 20")
    if args.chunk_overlap < 0:
        sys.exit("ERROR: --chunk-overlap cannot be negative")
    if args.chunk_overlap >= args.chunk_size:
        sys.exit("ERROR: --chunk-overlap must be smaller than --chunk-size")

    _check_deps(args.install_root)

    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok", True) else 1)


if __name__ == "__main__":
    main()
