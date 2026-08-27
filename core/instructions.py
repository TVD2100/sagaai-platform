"""core.instructions - filesystem-backed CRUD for global instructions.

Global instructions are stored as markdown files with front-matter under
DATA_DIR/orchestrators/global_instructions/<id>.md:

    ---
    id: github_connector
    name: GitHub Connector
    description: How to use GitHub connection tools...
    ---

    <prompt body>

Same public API as the old DB-backed storage, so existing callers work."""
from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, List, Optional

from core.defaults import parse_front_matter
from core.fs import ensure_dir


_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _root() -> str:
    """Return the global instructions root dir (re-reads DATA_DIR)."""
    import core.paths
    return os.path.join(core.paths.DATA_DIR, "orchestrators", "global_instructions")


def _safe_filename(instruction_id: str) -> str:
    """Return a filesystem-safe file name for an instruction id."""
    safe = _SAFE_RE.sub("_", (instruction_id or "").strip())
    return safe or "instruction"


def _instruction_path(instruction_id: str) -> str:
    return os.path.join(_root(), _safe_filename(instruction_id) + ".md")


def _read_instruction_file(instruction_id: str) -> Optional[Dict[str, Any]]:
    """Read one instruction file and parse its front-matter."""
    path = _instruction_path(instruction_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    default_id = _safe_filename(instruction_id)
    meta, body = parse_front_matter(raw, default_id=default_id)
    iid = meta.get("id") or default_id
    return {
        "id": iid,
        "name": meta.get("name") or iid,
        "description": meta.get("description", ""),
        "prompt_text": body,
    }


def _write_instruction_file(instruction_id: str, name: str,
                            description: str, prompt_text: str) -> bool:
    """Write one instruction as a markdown file with front-matter."""
    ensure_dir(_root())
    clean_name = (name or instruction_id).strip().replace(chr(10), " ")
    clean_desc = (description or "").replace(chr(10), " ").strip()
    header = [
        "---",
        "id: " + instruction_id,
        "name: " + clean_name,
        "description: " + clean_desc,
        "---",
        "",
    ]
    content = "\n".join(header) + (prompt_text or "")
    try:
        with open(_instruction_path(instruction_id), "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception:
        return False


def list_instructions() -> List[Dict[str, Any]]:
    """Return metadata for all global instructions (without prompt_text)."""
    root = _root()
    if not os.path.isdir(root):
        return []
    result: List[Dict[str, Any]] = []
    for fname in sorted(os.listdir(root)):
        if not fname.endswith(".md"):
            continue
        inst = _read_instruction_file(fname[:-3])
        if inst is None:
            continue
        result.append({
            "id": inst["id"],
            "name": inst["name"],
            "description": inst["description"],
        })
    return result


def get_instruction(instruction_id: str) -> Optional[Dict[str, Any]]:
    """Return one instruction incl. prompt_text (as 'text'), or None."""
    inst = _read_instruction_file(instruction_id)
    if inst is None:
        return None
    return {
        "id": inst["id"],
        "name": inst["name"],
        "description": inst["description"],
        "text": inst["prompt_text"],
    }


def get_instruction_prompt(instruction_id: str) -> str:
    """Return only the prompt_text, or an empty string."""
    inst = _read_instruction_file(instruction_id)
    return inst["prompt_text"] if inst else ""

def _connector_service_for_instruction(iid: str) -> Optional[str]:
    """Return the connector service for a global instruction id, or None.

    Instructions whose id ends with ``_connector`` and whose prefix matches a
    known connector service (e.g. ``github_connector``) are available to an
    orchestrator only when a connection of that service is enabled.
    """
    if not (iid or "").endswith("_connector"):
        return None
    prefix = iid[: -len("_connector")]
    try:
        from core.connectors import CONNECTOR_SERVICES
        if prefix in CONNECTOR_SERVICES:
            return prefix
    except Exception:
        pass
    return None


def list_instructions_for(orchestrator_slug: str) -> List[Dict[str, Any]]:
    """Return global instructions available to the given orchestrator.

    Connector-backed instructions (e.g. ``github_connector``) are included
    only when the orchestrator has at least one enabled connection of the
    matching service. Non-connector global instructions are always included.
    """
    all_instructions = list_instructions()
    if not all_instructions:
        return []
    try:
        from core.orchestrators import get_enabled_connections
        enabled_ids = get_enabled_connections(orchestrator_slug)
        enabled_services = set()
        if enabled_ids:
            from core.connectors import get_connection
            for cid in enabled_ids:
                conn = get_connection(cid)
                svc = (conn or {}).get("service")
                if svc:
                    enabled_services.add(str(svc))
    except Exception:
        enabled_services = set()
    result = []
    for inst in all_instructions:
        iid = inst.get("id")
        svc = _connector_service_for_instruction(iid)
        if svc is not None and svc not in enabled_services:
            continue
        result.append(inst)
    return result


def get_instruction_for(orchestrator_slug: str, instruction_id: str) -> Optional[Dict[str, Any]]:
    """Return a global instruction, or None when it is not available to the orchestrator.

    Applies the same connector filter as ``list_instructions_for``.
    """
    iid = (instruction_id or "").strip()
    svc = _connector_service_for_instruction(iid)
    if svc is not None:
        available = {i["id"] for i in list_instructions_for(orchestrator_slug)}
        if iid not in available:
            return None
    return get_instruction(iid)


def ensure_global_instructions() -> Dict[str, str]:
    """Import bundled global instructions from defaults/instructions/*.md.

    Idempotent: existing runtime files are never overwritten (user edits in
    DATA_DIR/orchestrators/global_instructions/ are preserved). Returns a
    status dict {id: "created" | "exists" | "error"}.
    """
    from core.defaults import defaults_root
    src_dir = os.path.join(defaults_root(), "instructions")
    if not os.path.isdir(src_dir):
        return {}
    status: Dict[str, str] = {}
    for fname in sorted(os.listdir(src_dir)):
        if not fname.endswith(".md"):
            continue
        default_id = fname[:-3]
        try:
            with open(os.path.join(src_dir, fname), "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        meta, body = parse_front_matter(raw, default_id=default_id)
        iid = meta.get("id") or default_id
        if _read_instruction_file(iid):
            status[iid] = "exists"
            continue
        ok = _write_instruction_file(
            iid,
            name=meta.get("name") or iid,
            description=meta.get("description", ""),
            prompt_text=body,
        )
        status[iid] = "created" if ok else "error"
    return status


def create_instruction(name: str, description: str, prompt_text: str,
                       instruction_id: Optional[str] = None) -> Optional[str]:
    """Create a new instruction and return its id, or None on failure."""
    iid = (instruction_id or "").strip()
    if not iid:
        iid = uuid.uuid4().hex[:8]
    ok = _write_instruction_file(iid, name or iid, description or "", prompt_text or "")
    return iid if ok else None


def update_instruction(instruction_id: str, name: str, description: str,
                       prompt_text: str) -> bool:
    """Update an existing instruction. Returns True on success."""
    iid = (instruction_id or "").strip()
    if not iid:
        return False
    return _write_instruction_file(iid, name or iid, description or "", prompt_text or "")


def delete_instruction(instruction_id: str) -> bool:
    """Delete an instruction by id. Returns True when it no longer exists."""
    iid = (instruction_id or "").strip()
    if not iid:
        return False
    path = _instruction_path(iid)
    try:
        if os.path.isfile(path):
            os.remove(path)
        return True
    except Exception:
        return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
