# -*- coding: utf-8 -*-
"""
core.personal_memory - the personal memory layer of the installation.

The layer keeps short, durable notes about the installation owner and the
people working on their behalf: working agreements, preferred formats, where
their files live, which projects are active. It exists so that a new dialog
does not start with re-explaining the same things.

Memory is organised as *scopes* - one Markdown file per scope:

    DATA_DIR/personal/user.md                     owner profile (always in prompt)
    DATA_DIR/personal/orchestrators/<slug>.md     per-employee notes
    DATA_DIR/personal/projects/<hash>.md          per-working-folder notes
    DATA_DIR/connectors/<id>/memory.md            per-connection notes (existing)

Design rules encoded here:

* **Personal, not portable.** The scope files live under ``DATA_DIR`` and are
  deliberately outside the orchestrator/assistant/connector entity folders, so
  exporting an entity (zip of its folder) can never carry personal notes along.
* **Connection memory is inherited, not migrated.** ``connector:<id>`` is an
  adapter over the existing ``core.connectors`` API; the file stays where it
  is and its contract does not change.
* **Missing file is normal.** Reading an absent scope yields an empty string -
  the same convention as ``core.connectors.get_connection_memory``.
* **Writes are atomic and versioned.** A temporary file plus ``os.replace``
  guarantees the file is never half-written; the previous version is copied to
  ``personal/.backups/`` with a bounded rotation.
* **Credentials are refused.** A write whose text looks like a token, password
  or private key is rejected instead of stored.

No streamlit imports: the module is used by the agent tools, the UI and tests.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import core.paths
from core.fs import ensure_dir


# ─── Constants ────────────────────────────────────────────────────────────────

PERSONAL_DIR_NAME: str = "personal"

#: Hard limit of a single scope file. Chosen deliberately above the value used
#: for connection memory so an established employee database keeps working,
#: while still keeping the personal layer small enough to stay a summary.
PERSONAL_MEMORY_MAX_BYTES: int = 64 * 1024

#: How many previous versions of one scope file are kept.
PERSONAL_BACKUPS_KEPT: int = 10

#: Owner-card budget: hard cap, floor and the share of the model input ceiling.
PERSONAL_CARD_HARD_CAP: int = 2500
PERSONAL_CARD_MIN_CAP: int = 400
PERSONAL_CARD_RATIO: float = 0.03

SCOPE_USER: str = "user"
SCOPE_ORCHESTRATOR: str = "orchestrator"
SCOPE_PROJECT: str = "project"
SCOPE_CONNECTOR: str = "connector"

VALID_SCOPES: Tuple[str, ...] = (
    SCOPE_USER, SCOPE_ORCHESTRATOR, SCOPE_PROJECT, SCOPE_CONNECTOR,
)
SCOPE_REQUIRES_ID: Tuple[str, ...] = (
    SCOPE_ORCHESTRATOR, SCOPE_PROJECT, SCOPE_CONNECTOR,
)

#: Canonical section order. The order doubles as the truncation priority of the
#: owner card: ``Rules`` is never dropped, the rest go from the bottom up.
SECTION_ORDER: Tuple[str, ...] = ("Rules", "Preferences", "Environment", "Notes")

HEADER_TITLE: str = "# Personal memory"

_LOCK = threading.RLock()

_CONN_ID_RE = re.compile(r"^[a-z0-9_-]+$")
_PROJECT_KEY_RE = re.compile(r"^[0-9a-f]{12}$")

#: Signals strong enough to refuse a write. Deliberately narrow: a false
#: positive blocks a legitimate note, so only unambiguous credential shapes are
#: matched and the caller is told exactly which fragment triggered the refusal.
_SECRET_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key|apikey)"
               r"\b\s*[=:]\s*\S{6,}"),
)


def _looks_like_base64_blob(token: str) -> bool:
    """Return True for a long token that really looks like encoded key material.

    A plain run of one repeated letter (``xxxx...``) is legitimate note text, so
    the shape is checked instead of the length alone: encoded material mixes
    upper and lower case, digits and often ``+/`` characters.
    """
    if len(token) < 40:
        return False
    classes = sum((
        any(ch.islower() for ch in token),
        any(ch.isupper() for ch in token),
        any(ch.isdigit() for ch in token),
        bool(set(token) & set("+/=")),
    ))
    return classes >= 3


# ─── Time helpers ─────────────────────────────────────────────────────────────


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    """Return a filesystem-safe timestamp used for backup and archive names."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


# ─── Root and paths ───────────────────────────────────────────────────────────


def personal_dir() -> str:
    """Return the personal-layer root.

    ``core.paths.DATA_DIR`` is read at call time so tests (and alternate
    deployments) can point it at a temporary directory.
    """
    return os.path.join(core.paths.DATA_DIR, PERSONAL_DIR_NAME)


def ensure_personal_dir() -> str:
    """Create the personal-layer root when missing and return its path."""
    return ensure_dir(personal_dir())


def project_key(path: str) -> str:
    """Return the stable 12-char key of a working folder.

    The key is derived from the normalised absolute path: the same folder opens
    under one key regardless of how the path was written, and the name stays
    filesystem-safe for cyrillic paths and long names. The readable path is
    stored inside the file header and in ``index.json``.
    """
    raw = (path or "").strip()
    if not raw:
        return ""
    try:
        norm = str(Path(raw).expanduser().resolve())
    except (OSError, RuntimeError):
        norm = os.path.abspath(os.path.expanduser(raw))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


# ─── Scope parsing and normalisation ──────────────────────────────────────────


def normalize_scope_id(kind: str, raw: str = "") -> str:
    """Return a safe scope id, or '' when the input cannot be normalised.

    Orchestrator slugs go through the same normaliser as their folders;
    connection ids keep the connector id format; project ids accept either an
    absolute/relative path (hashed) or an already computed 12-char key.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if kind == SCOPE_ORCHESTRATOR:
        from core.orchestrator_folders import safe_orchestrator_slug
        return safe_orchestrator_slug(text)
    if kind == SCOPE_PROJECT:
        if _PROJECT_KEY_RE.fullmatch(text):
            return text
        return project_key(text)
    if kind == SCOPE_CONNECTOR:
        return text if _CONN_ID_RE.fullmatch(text) else ""
    return ""


def parse_scope(raw: str) -> Tuple[str, str]:
    """Split ``'kind:key'`` into ``(kind, key)``.

    A bare ``'user'`` is accepted; a scope that requires an id but carries an
    empty or unusable one raises ``ValueError`` instead of silently falling back
    to a placeholder bucket.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Memory scope is empty")
    if ":" in text:
        kind, _, key = text.partition(":")
        kind = kind.strip().lower()
        key = key.strip()
    else:
        kind, key = text.lower(), ""
    if kind not in VALID_SCOPES:
        raise ValueError(f"Unknown memory scope: '{raw}'")
    if kind in SCOPE_REQUIRES_ID:
        key = normalize_scope_id(kind, key)
        if not key:
            raise ValueError(f"Memory scope '{kind}' requires a usable id")
    else:
        key = ""
    return kind, key


def format_scope(kind: str, key: str = "") -> str:
    """Return the canonical scope string."""
    return f"{kind}:{key}" if key else kind


def memory_path(scope: str) -> str:
    """Return the absolute path of a scope file (it may not exist yet)."""
    kind, key = parse_scope(scope)
    root = personal_dir()
    if kind == SCOPE_USER:
        return os.path.join(root, "user.md")
    if kind == SCOPE_ORCHESTRATOR:
        return os.path.join(root, "orchestrators", f"{key}.md")
    if kind == SCOPE_PROJECT:
        return os.path.join(root, "projects", f"{key}.md")
    if kind == SCOPE_CONNECTOR:
        return os.path.join(core.paths.DATA_DIR, "connectors", key, "memory.md")
    raise ValueError(f"Unknown memory scope: '{scope}'")


# ─── Access control ───────────────────────────────────────────────────────────


def check_access(scope: str, caller_slug: str = "", write: bool = False) -> None:
    """Raise ``PermissionError`` when *caller_slug* may not touch *scope*.

    Policy (enforced here rather than in the tool dispatcher, so a Python
    wrapper cannot bypass it):

    * an empty *caller_slug* means an internal caller (UI, migration) - only
      connection memory stays write-protected for it, because that file is
      managed exclusively through the connector tools;
    * an employee may read/write ``user`` and its own ``orchestrator:<slug>``,
      may read/write ``project:*`` of the active folder, and may read only the
      ``connector:*`` scopes enabled for it.
    """
    kind, key = parse_scope(scope)
    if not caller_slug:
        # Internal callers (UI, migration, prompt assembly) are trusted: they
        # reach this layer directly, not through the agent tool dispatcher.
        # Keeping the agent away from connection memory is the dispatcher's
        # allowlist job, not this function's.
        return

    own = normalize_scope_id(SCOPE_ORCHESTRATOR, caller_slug)
    if kind in (SCOPE_USER, SCOPE_PROJECT):
        return
    if kind == SCOPE_ORCHESTRATOR:
        if key == own:
            return
        raise PermissionError(
            f"Memory scope '{format_scope(kind, key)}' belongs to another employee"
        )
    if kind == SCOPE_CONNECTOR:
        if write:
            raise PermissionError(
                "Connection memory is written through the connector tools"
            )
        enabled = _enabled_connection_ids(own)
        if enabled is None or key in enabled:
            return
        raise PermissionError(
            f"Connection '{key}' is not enabled for this employee"
        )


def _enabled_connection_ids(slug: str) -> Optional[set]:
    """Best-effort set of connection ids enabled for *slug* (None = unknown)."""
    if not slug:
        return None
    try:
        from core.orchestrators import get_enabled_connections
        return set(get_enabled_connections(slug) or [])
    except Exception:
        return None


# ─── Credential screen ────────────────────────────────────────────────────────


def detect_secret(text: str) -> str:
    """Return a short reason when *text* looks like it holds a credential.

    Empty string means the text passed the screen. The matched fragment is
    truncated on purpose: the error message must never echo the secret.
    """
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(text or "")
        if match:
            head = match.group(0)[:14]
            return f"text looks like a credential ('{head}...')"
    for token in re.findall(r"[A-Za-z0-9+/]{40,}={0,2}", text or ""):
        if _looks_like_base64_blob(token):
            return f"text looks like a credential ('{token[:14]}...')"
    return ""


# ─── Document structure ───────────────────────────────────────────────────────


def _split_document(text: str) -> Tuple[Dict[str, str], str]:
    """Split a scope file into its header map and body.

    Text without a header is treated as a bare body, so callers may write plain
    Markdown and still get a well-formed file.
    """
    lines = (text or "").splitlines()
    if not lines or not lines[0].startswith(HEADER_TITLE):
        return {}, text or ""
    meta: Dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            break
        if line.startswith("- "):
            name, _, value = line[2:].partition(":")
            meta[name.strip()] = value.strip()
            index += 1
        else:
            break
    return meta, "\n".join(lines[index:])


def _render_document(kind: str, key: str, body: str, updated_by: str,
                     revision: int) -> str:
    """Build the full scope file text from a body and header fields."""
    header = [
        f"{HEADER_TITLE}: {format_scope(kind, key)}",
        f"- scope: {kind}",
        f"- id: {key or '-'}",
        f"- updated: {_now()}",
        f"- updated_by: {updated_by or 'agent'}",
        f"- revision: {int(revision)}",
        "",
    ]
    cleaned = (body or "").strip("\n")
    return "\n".join(header) + (cleaned + "\n" if cleaned else "")


def parse_sections(text: str) -> Dict[str, List[str]]:
    """Group a body by its ``## Section`` headings, preserving order."""
    sections: Dict[str, List[str]] = {}
    current = ""
    for line in (text or "").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    return sections


# ─── Atomic write, backups, archive ───────────────────────────────────────────


def _atomic_write(path: str, text: str) -> None:
    """Write *text* to *path* atomically (temporary file plus rename)."""
    ensure_dir(os.path.dirname(path) or ".")
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _service_dir(kind: str, scope: str) -> str:
    """Return a service folder (backups/archive) for a scope."""
    return os.path.join(personal_dir(), kind, scope.replace(":", "__"))


def _rotate_backups(folder: str) -> None:
    """Keep only the newest ``PERSONAL_BACKUPS_KEPT`` files in *folder*."""
    try:
        files = sorted(f for f in os.listdir(folder) if f.endswith(".md"))
    except OSError:
        return
    for stale in files[:-PERSONAL_BACKUPS_KEPT]:
        try:
            os.remove(os.path.join(folder, stale))
        except OSError:
            pass


def _backup_file(scope: str) -> str:
    """Copy the current scope file into ``.backups`` and return the copy path."""
    path = memory_path(scope)
    if not os.path.exists(path):
        return ""
    folder = _service_dir(".backups", scope)
    ensure_dir(folder)
    destination = os.path.join(folder, f"v{_stamp()}.md")
    try:
        shutil.copy2(path, destination)
    except OSError:
        return ""
    _rotate_backups(folder)
    return destination


# ─── Public read / write / delete ─────────────────────────────────────────────


def memory_exists(scope: str) -> bool:
    """Return True when the scope file exists and is not empty."""
    kind, key = parse_scope(scope)
    if kind == SCOPE_CONNECTOR:
        try:
            from core.connectors import get_connection_memory
            return bool(get_connection_memory(key).strip())
        except Exception:
            return False
    path = memory_path(scope)
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def read_memory(scope: str, max_chars: int = 0,
                caller_slug: str = "") -> Dict[str, Any]:
    """Return the scope file as a structured result.

    A missing file is not an error: ``exists`` is False and ``content`` is
    empty. When *max_chars* is positive the returned text is cut to that many
    characters and ``truncated`` becomes True; ``bytes`` always reports the
    full stored size, so the caller can see how much was left out.

    ``caller_slug`` applies the same access policy as writes; an empty value
    marks an internal (trusted) caller such as the prompt assembly.
    """
    kind, key = parse_scope(scope)
    check_access(scope, caller_slug=caller_slug, write=False)
    text = ""
    body = ""
    updated_at = ""
    updated_by = ""
    revision = 0

    if kind == SCOPE_CONNECTOR:
        try:
            from core.connectors import get_connection_memory
            text = get_connection_memory(key) or ""
        except Exception:
            text = ""
        body = text
        path = memory_path(scope)
        try:
            updated_at = datetime.fromtimestamp(
                os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds")
        except OSError:
            updated_at = ""
    else:
        path = memory_path(scope)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (FileNotFoundError, OSError):
            text = ""
        meta, _body = _split_document(text)
        body = _body.strip("\n")
        updated_at = meta.get("updated", "")
        updated_by = meta.get("updated_by", "")
        try:
            revision = int(meta.get("revision", "0"))
        except (TypeError, ValueError):
            revision = 0

    size = len(text.encode("utf-8"))
    truncated = False
    if max_chars and max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return {
        "ok": True,
        "scope": format_scope(kind, key),
        "kind": kind,
        "scope_id": key,
        "exists": bool(size),
        "content": text,
        "body": body,
        "bytes": size,
        "max_bytes": PERSONAL_MEMORY_MAX_BYTES,
        "truncated": truncated,
        "updated_at": updated_at,
        "updated_by": updated_by,
        "revision": revision,
    }


def write_memory(scope: str, content: str = "", updated_by: str = "agent",
                 caller_slug: str = "") -> Dict[str, Any]:
    """Replace the scope file with *content*.

    The pipeline is: validate the scope and permissions -> screen the text for
    credentials -> check the size limit -> back up the current version -> write
    atomically -> bump the revision in the header.

    Raises:
        ValueError: unknown scope, credential-like text, or an oversized file.
        PermissionError: the caller may not write this scope.
    """
    kind, key = parse_scope(scope)
    check_access(scope, caller_slug=caller_slug, write=True)

    data = str(content if content is not None else "")
    reason = detect_secret(data)
    if reason:
        raise ValueError(f"Refusing to store memory: {reason}")

    if kind == SCOPE_CONNECTOR:
        from core.connectors import set_connection_memory
        ok = set_connection_memory(key, data)
        return {
            "ok": bool(ok),
            "scope": format_scope(kind, key),
            "kind": kind,
            "scope_id": key,
            "bytes": len(data.encode("utf-8")),
            "max_bytes": PERSONAL_MEMORY_MAX_BYTES,
            "saved": bool(ok),
            "revision": 0,
        }

    with _LOCK:
        previous = read_memory(scope)
        old_meta, _old_body = _split_document(previous["content"])
        try:
            revision = int(old_meta.get("revision", "0")) + 1
        except (TypeError, ValueError):
            revision = 1

        _new_meta, body = _split_document(data)
        document = _render_document(kind, key, body, updated_by, revision)
        encoded = document.encode("utf-8")
        if len(encoded) > PERSONAL_MEMORY_MAX_BYTES:
            raise ValueError(
                f"Memory is too large: {len(encoded)} bytes "
                f"(max {PERSONAL_MEMORY_MAX_BYTES})"
            )

        if previous["exists"]:
            _backup_file(scope)
        ensure_personal_dir()
        _atomic_write(memory_path(scope), document)

    return {
        "ok": True,
        "scope": format_scope(kind, key),
        "kind": kind,
        "scope_id": key,
        "bytes": len(encoded),
        "max_bytes": PERSONAL_MEMORY_MAX_BYTES,
        "saved": True,
        "revision": revision,
    }


def delete_memory(scope: str, archived_by: str = "agent",
                  caller_slug: str = "") -> bool:
    """Move a scope file into ``personal/.archive`` and report whether it existed."""
    kind, key = parse_scope(scope)
    check_access(scope, caller_slug=caller_slug, write=True)
    if kind == SCOPE_CONNECTOR:
        raise PermissionError(
            "Connection memory is managed through the connector tools"
        )
    path = memory_path(scope)
    if not os.path.exists(path):
        return False
    with _LOCK:
        folder = _service_dir(".archive", scope)
        ensure_dir(folder)
        destination = os.path.join(folder, f"{_stamp()}__{archived_by}.md")
        try:
            shutil.move(path, destination)
        except OSError:
            return False
    return True


# ─── Registry and metadata ────────────────────────────────────────────────────


def memory_meta(scope: str) -> Dict[str, Any]:
    """Return size and freshness metadata without exposing the text."""
    result = read_memory(scope, max_chars=1)
    return {
        "scope": result["scope"],
        "kind": result["kind"],
        "scope_id": result["scope_id"],
        "exists": result["exists"],
        "bytes": result["bytes"],
        "max_bytes": result["max_bytes"],
        "updated_at": result["updated_at"],
        "updated_by": result["updated_by"],
        "revision": result["revision"],
    }


def list_memory_scopes() -> List[Dict[str, Any]]:
    """Return every non-empty scope with its metadata, in a stable order."""
    found: List[Dict[str, Any]] = []
    candidates: List[str] = [SCOPE_USER]

    root = personal_dir()
    for kind, sub in ((SCOPE_ORCHESTRATOR, "orchestrators"),
                      (SCOPE_PROJECT, "projects")):
        folder = os.path.join(root, sub)
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            names = []
        for name in names:
            if name.endswith(".md"):
                candidates.append(format_scope(kind, name[:-3]))

    try:
        from core.connectors import list_connections
        for item in list_connections():
            conn_id = str(item.get("id") or "")
            if conn_id:
                candidates.append(format_scope(SCOPE_CONNECTOR, conn_id))
    except Exception:
        pass

    for scope in candidates:
        try:
            meta = memory_meta(scope)
        except (ValueError, PermissionError):
            continue
        if meta["exists"]:
            found.append(meta)
    return found


# ─── Owner card for the prompt ────────────────────────────────────────────────


def card_cap(input_ceiling_tokens: int = 0) -> int:
    """Return the owner-card character budget for a model input ceiling.

    ``min(HARD_CAP, max(MIN_CAP, RATIO * ceiling))`` - small windows shrink the
    card proportionally, large ones always get the full budget. A zero or
    unknown ceiling yields the hard cap.
    """
    try:
        ceiling = int(input_ceiling_tokens or 0)
    except (TypeError, ValueError):
        ceiling = 0
    if ceiling <= 0:
        return PERSONAL_CARD_HARD_CAP
    adaptive = int(PERSONAL_CARD_RATIO * ceiling)
    return min(PERSONAL_CARD_HARD_CAP, max(PERSONAL_CARD_MIN_CAP, adaptive))


def render_owner_card(max_chars: int = PERSONAL_CARD_HARD_CAP) -> str:
    """Render the owner card that is always injected into the system prompt.

    Only ``## Rules``, ``## Preferences``, ``## Environment`` and ``## Notes``
    sections are rendered, in that order. ``Rules`` is never dropped (it is the
    reason the card exists); the remaining sections are added while the budget
    allows, so truncation always removes the most easily rediscovered content
    first. An empty or missing file yields '' and the caller adds nothing.
    """
    text = read_memory(SCOPE_USER)["content"]
    if not text.strip():
        return ""
    _meta, body = _split_document(text)
    sections = parse_sections(body)
    if not any(line.strip() for lines in sections.values() for line in lines):
        return ""

    cap = max(PERSONAL_CARD_MIN_CAP, int(max_chars or PERSONAL_CARD_HARD_CAP))
    parts: List[str] = []
    used = 0
    for name in SECTION_ORDER:
        lines = [line.rstrip() for line in sections.get(name, []) if line.strip()]
        if not lines:
            continue
        block = f"## {name}\n" + "\n".join(lines)
        if name == "Rules" or used + len(block) + 2 <= cap:
            parts.append(block)
            used += len(block) + 2
            continue
        # The section does not fit whole: keep as many of its lines as the
        # remaining budget allows, so a long section degrades instead of
        # disappearing from the card entirely.
        remaining = cap - used
        header = f"## {name}\n"
        if remaining < len(header) + 8:
            break
        kept: List[str] = []
        for line in lines:
            if len(header + "\n".join(kept + [line])) + 2 > remaining:
                break
            kept.append(line)
        if kept:
            parts.append(header + "\n".join(kept))
        break

    if not parts:
        return ""
    rendered = "\n\n".join(parts)
    if len(rendered) > cap:
        cut = rendered[:cap]
        if "\n" in cut:
            cut = cut.rsplit("\n", 1)[0]
        rendered = cut
    return rendered


# ─── Index (derived cache) ────────────────────────────────────────────────────


def rebuild_index() -> Dict[str, Any]:
    """Rebuild ``personal/index.json`` from the scope files and return it.

    The index is a derived cache: the scope files stay the source of truth, so a
    stale or missing index is always recoverable by calling this function.
    """
    from core.fs import write_json_file

    scopes: Dict[str, Any] = {}
    for meta in list_memory_scopes():
        entry = dict(meta)
        entry["path"] = os.path.relpath(memory_path(meta["scope"]), personal_dir())
        scopes[meta["scope"]] = entry
    index = {"version": 1, "updated": _now(), "scopes": scopes}
    ensure_personal_dir()
    write_json_file(os.path.join(personal_dir(), "index.json"), index)
    return index
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
