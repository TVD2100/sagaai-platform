# -*- coding: utf-8 -*-
"""
dev_agent.loop.results -- the tool-result size policy and its storage form.

A single tool_result must never blow up the model context window (incident: a
1.65M-character result caused an HTTP 400 context overflow). Three tiers are
applied by the serialized ``{"tool_result": ...}`` document size:

* up to ``TOOL_RESULT_INLINE_LIMIT`` - the result passes through unchanged;
* above it - the full document is SPILLED to
  ``<PROJECT_ROOT>/.dev_agent/tool_results/`` BEFORE its first send and
  replaced by a bounded head+tail preview carrying ``spill_path`` (readable
  back with the read_file tool). The model never sees the untruncated
  version, so from the cache viewpoint the replacement is append-only;
* above ``MAX_TOOL_RESULT_CHARS`` AND the spill failed - an explicit ok=False
  error carrying the measured size, so even then the payload never reaches
  the model.

A second policy keeps the persisted form bounded: hidden tool_results go into
 the thread database, and storing a giant payload bloats the database and
breaks the context on reload. Small documents are kept byte-verbatim, so the
stored form matches the wire form; large ones are reduced to their scalar
fields with a ``bulk_sizes`` map.

Two knobs are read through the facade module on purpose: ``_TOOL_RESULT_SPILL_KEEP``
and ``_spill_tool_result_doc``. Tests replace them on the public loop module,
and reading the attribute at call time is what makes that replacement reach
the code here. Everything else is a plain module constant.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_TOOL_RESULT_PREFIX = '{"tool_result"'

#: Hard emergency fallback: when the spill cannot be written, results up to
#: this size pass through unchanged (the storage layer still compacts them),
#: anything larger is replaced by an explicit ok=False error.
MAX_TOOL_RESULT_CHARS: int = 200_000

#: Threshold above which a tool_result is spilled to disk and replaced by a
#: bounded preview.
_TOOL_RESULT_INLINE_LIMIT: int = 35_000

#: Head/tail budget (characters) of a spilled-result preview. If the preview
#: still exceeds ``_TOOL_RESULT_INLINE_LIMIT`` after bounding, both sides
#: shrink deterministically by ``_TOOL_RESULT_PREVIEW_SHRINK`` until it fits.
_TOOL_RESULT_PREVIEW_SIDE: int = 8_000
_TOOL_RESULT_PREVIEW_SHRINK: float = 0.85

#: Rotation: keep only this many newest spill files in .dev_agent/tool_results.
_TOOL_RESULT_SPILL_KEEP: int = 100

#: Storage tier: results up to this many characters are stored verbatim;
#: larger ones are compacted. ``_TOOL_RESULT_STORAGE_FIELD_LIMIT`` bounds one
#: scalar string and ``_TOOL_RESULT_STORAGE_FALLBACK_LIMIT`` bounds raw text
#: that could not be parsed at all.
_TOOL_RESULT_STORAGE_KEEP_LIMIT: int = 50_000
_TOOL_RESULT_STORAGE_FIELD_LIMIT: int = 400
_TOOL_RESULT_STORAGE_FALLBACK_LIMIT: int = 10_000

#: Keys that typically carry large payloads and must not be persisted in full.
_TOOL_RESULT_BULK_KEYS = frozenset({
    "content", "entries", "files", "dirs", "results", "blocks", "messages",
    "new_text", "verified_text", "diff", "edits", "details", "suggestions",
    "occurrences", "events", "data", "chunks", "documents", "items",
})


def _facade_attr(name: str, default: Any) -> Any:
    """Return an attribute of the loop facade, or *default* when it is gone.

    The facade re-exports the constants and helpers of this module, and the
    tests of the size policy replace them there. Reading the attribute at
    call time keeps that replacement effective without making the caller
    reach into the facade.
    """
    try:
        from dev_agent import agent_loop
    except Exception:  # pragma: no cover - the facade is always importable here
        return default
    return getattr(agent_loop, name, default)


def tool_results_dir() -> Path:
    """Return the spill directory for oversized tool results (not created).

    Mirrors ``dev_agent.task_state``: runtime artifacts live under
    ``<PROJECT_ROOT>/.dev_agent/``. PROJECT_ROOT is read lazily from
    dev_agent.config so workspace switches and test sandboxes are honoured.
    """
    try:
        from dev_agent import config as _cfg
        root = Path(_cfg.PROJECT_ROOT)
    except Exception:
        root = Path(os.getcwd())
    return root / ".dev_agent" / "tool_results"


def rotate_tool_result_spills(directory: Path) -> None:
    """Best-effort rotation: keep only the newest spill files."""
    keep = int(_facade_attr("_TOOL_RESULT_SPILL_KEEP", _TOOL_RESULT_SPILL_KEEP))
    try:
        files = [p for p in directory.glob("tool_result_*.json") if p.is_file()]
        if len(files) <= keep:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            try:
                stale.unlink()
            except Exception:
                pass
    except Exception:
        pass


def spill_tool_result_doc(doc: str, tool_name: str = "") -> Optional[str]:
    """Write the full tool_result document to disk; return its relative path.

    The file lands in ``<PROJECT_ROOT>/.dev_agent/tool_results/`` as a plain
    JSON document the model can read back with ``read_file``. Runtime
    artifacts are written directly (not through the source-writer guards):
    the directory is DevAgent's own hidden runtime area, never project
    source. Returns None when the file cannot be written, so the caller
    falls back to the hard size cap.
    """
    directory = tool_results_dir()
    directory.mkdir(parents=True, exist_ok=True)
    safe_tool = re.sub(r"[^A-Za-z0-9_.-]", "_", str(tool_name or "tool"))[:40]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"tool_result_{safe_tool}_{stamp}_{uuid.uuid4().hex[:8]}.json"
    target = directory / name
    target.write_text(doc, encoding="utf-8")
    rotate_tool_result_spills(directory)
    try:
        from dev_agent import config as _cfg
        return target.relative_to(Path(_cfg.PROJECT_ROOT)).as_posix()
    except Exception:
        return str(target)


def compact_result_fields(tr: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a tool-result dict to scalar fields plus a ``bulk_sizes`` map.

    List values are replaced by their element count, dicts and known bulk
    fields by their serialized size, oversized scalar strings by a truncated
    head plus ``<key>_full_len``. Shared by the storage summary and by the
    spilled-result preview so both carry the same control fields.
    """
    if not isinstance(tr, dict):
        return {}
    compact: Dict[str, Any] = {}
    sizes: Dict[str, Any] = {}
    for key, value in tr.items():
        if isinstance(value, (list, tuple)):
            sizes[key] = len(value)
            continue
        if isinstance(value, dict) or key in _TOOL_RESULT_BULK_KEYS:
            try:
                sizes[key] = len(json.dumps(value, ensure_ascii=False))
            except Exception:
                sizes[key] = len(str(value))
            continue
        if isinstance(value, str) and len(value) > _TOOL_RESULT_STORAGE_FIELD_LIMIT:
            compact[key] = value[:_TOOL_RESULT_STORAGE_FIELD_LIMIT]
            compact[key + "_full_len"] = len(value)
            continue
        compact[key] = value
    if sizes:
        compact["bulk_sizes"] = sizes
    return compact


def build_tool_result_preview(result: Any, doc: str, size: int,
                              spill_path: str) -> Dict[str, Any]:
    """Build a bounded head+tail preview of a spilled tool result.

    Control fields of *result* (scalars + ``bulk_sizes``) are copied, *doc*
    contributes the ordered head and tail of the preview budget. The total
    serialized preview is kept within ``_TOOL_RESULT_INLINE_LIMIT`` so the
    sent (wire) document and the stored document can stay byte-identical.
    """
    head_budget = _TOOL_RESULT_PREVIEW_SIDE
    tail_budget = _TOOL_RESULT_PREVIEW_SIDE
    preview: Dict[str, Any] = {}
    while True:
        preview = compact_result_fields(result if isinstance(result, dict) else {})
        preview["truncated"] = True
        preview["spill_path"] = spill_path
        preview["spill_size"] = size
        preview["head"] = doc[:head_budget]
        preview["tail"] = doc[-tail_budget:] if tail_budget else ""
        preview["note"] = (
            "Result too large for the model context: the full payload is "
            "spilled to spill_path and readable with read_file "
            "(use offset/limit). head+tail are the ordered beginning and end."
        )
        try:
            wire_len = len(json.dumps({"tool_result": preview}, ensure_ascii=False))
        except Exception:
            return preview
        if wire_len <= _TOOL_RESULT_INLINE_LIMIT:
            return preview
        if head_budget <= 200 and tail_budget <= 200:
            return preview
        head_budget = max(100, int(head_budget * _TOOL_RESULT_PREVIEW_SHRINK))
        tail_budget = max(100, int(tail_budget * _TOOL_RESULT_PREVIEW_SHRINK))


def apply_tool_result_cap(result: Dict[str, Any], tool_name: str = "") -> Dict[str, Any]:
    """Apply the tool-result size policy; return a context-safe result.

    Three outcomes by the serialized ``{"tool_result": ...}`` document size:

    * up to ``_TOOL_RESULT_INLINE_LIMIT`` - returned unchanged;
    * above the inline limit - the full document is spilled to
      ``<PROJECT_ROOT>/.dev_agent/tool_results/`` and the result is replaced
      by a bounded head+tail preview carrying ``spill_path`` (readable back
      with the read_file tool). The replacement happens BEFORE the first
      send, so no untruncated payload ever enters the model context;
    * above ``MAX_TOOL_RESULT_CHARS`` AND the spill write failed - replaced
      by an explicit error dict (``result_too_large=True`` +
      ``result_size``) so even then the payload never reaches the model.

    Parameters
    ----------
    result : dict
        Raw tool result about to be packaged as ``{"tool_result": result}``.
    tool_name : str
        Tool the result came from (used only in the spill file name).
    """
    try:
        doc = json.dumps({"tool_result": result}, ensure_ascii=False)
    except Exception:
        doc = ""
    size = len(doc) if doc else len(str(result))
    if size <= _TOOL_RESULT_INLINE_LIMIT:
        return result
    spill_path: Optional[str] = None
    if doc:
        spill = _facade_attr("_spill_tool_result_doc", spill_tool_result_doc)
        try:
            spill_path = spill(doc, tool_name)
        except Exception:
            spill_path = None
    if spill_path:
        return build_tool_result_preview(result, doc, size, spill_path)
    if size <= MAX_TOOL_RESULT_CHARS:
        return result
    return {
        "ok": False,
        "error": (
            "Tool result is too large for the model context "
            f"({size} characters; limit {MAX_TOOL_RESULT_CHARS}). "
            "The payload was NOT passed to the model. Re-run the tool with "
            "narrower arguments (offset/limit, max_depth, max_results, "
            "subdir filters) instead of repeating the same call."
        ),
        "result_too_large": True,
        "result_size": size,
    }


def summarize_single_tool_result_doc(content: str) -> str:
    """Compact ONE tool_result document (a single line of wire content).

    Documents up to ``_TOOL_RESULT_STORAGE_KEEP_LIMIT`` characters are
    returned byte-verbatim, keeping the wire == stored invariant for small
    results and for every spilled-result preview (bounded by
    ``_TOOL_RESULT_INLINE_LIMIT``). Larger documents are parsed and reduced
    via ``compact_result_fields``; unparseable oversized input is truncated
    raw to ``_TOOL_RESULT_STORAGE_FALLBACK_LIMIT``.
    """
    if len(content) <= _TOOL_RESULT_STORAGE_KEEP_LIMIT:
        return content
    try:
        data = json.loads(content)
    except Exception:
        return content[:_TOOL_RESULT_STORAGE_FALLBACK_LIMIT]
    tr = data.get("tool_result")
    if not isinstance(tr, dict):
        return content[:_TOOL_RESULT_STORAGE_FALLBACK_LIMIT]
    compact = compact_result_fields(tr)
    return json.dumps({"tool_result": compact}, ensure_ascii=False)


def summarize_tool_result_for_storage(content: str) -> str:
    """Return a compact, DB-safe form of hidden tool_result message content.

    The loop packs one step's results as newline-joined single-line JSON
    documents (``{"tool_result": ...}``), so the input may hold several
    documents. Each document is processed separately by
    ``summarize_single_tool_result_doc``: small documents (<= 50k chars)
    are stored byte-verbatim, larger ones are reduced to their scalar
    fields with a ``bulk_sizes`` map (emergency path for results the spill
    could not handle), and unparseable oversized ones are truncated raw to
    the fallback limit.

    Parameters
    ----------
    content : str
        The raw persisted message content (a ``{"tool_result": ...}`` JSON
        payload, possibly several newline-joined documents).

    Returns
    -------
    str
        Either the original content (small / not a tool_result) or the
        compacted form keeping the same document structure.
    """
    if not isinstance(content, str) or not content.startswith(_TOOL_RESULT_PREFIX):
        return content
    if len(content) <= _TOOL_RESULT_STORAGE_KEEP_LIMIT:
        return content
    if "\n" not in content:
        return summarize_single_tool_result_doc(content)
    return "\n".join(
        summarize_single_tool_result_doc(part) for part in content.split("\n")
    )


# ── Backward-compatible private aliases ─────────────────────────────────────
# The facade re-exports these names; tests of the size policy import and
# monkeypatch them there, so the underscore spelling stays part of the
# contract even though the definitions above read better without it.
_tool_results_dir = tool_results_dir
_rotate_tool_result_spills = rotate_tool_result_spills
_spill_tool_result_doc = spill_tool_result_doc
_compact_result_fields = compact_result_fields
_build_tool_result_preview = build_tool_result_preview
_apply_tool_result_cap = apply_tool_result_cap
_summarize_single_tool_result_doc = summarize_single_tool_result_doc


__all__ = [
    "MAX_TOOL_RESULT_CHARS",
    "apply_tool_result_cap",
    "build_tool_result_preview",
    "compact_result_fields",
    "rotate_tool_result_spills",
    "spill_tool_result_doc",
    "summarize_tool_result_for_storage",
    "tool_results_dir",
]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
