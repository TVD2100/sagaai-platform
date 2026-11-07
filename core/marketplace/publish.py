"""
core.marketplace.publish - publisher tooling for the catalog repository.

The catalog is a Git repository with a fixed layout; this module builds the
files that layout expects, so an author never hand-edits a manifest or a
hash. The module depends on nothing but the standard library and
``cryptography``: the very same file is published into the catalog as
``scripts/publish.py``, which is why the folder mapping and the timestamp
helper live here instead of being imported from the platform.

Three invariants drive the implementation:

  * **deterministic packages**. ``package.tar.gz`` must hash to the same
    sha256 on every rebuild, otherwise ``package.sha256`` changes without a
    content change and the package cache misses forever. Members are sorted,
    timestamps, owners and compression mtime are fixed.
  * **a version is immutable**. ``<type>/<id>/versions/<semver>/`` is written
    once; ``latest/`` and ``index.json`` are pointers rebuilt from whatever
    versions exist, so a failed publish never leaves a half-written version.
  * **the index is signed**. ``index.json.sig`` carries an Ed25519 signature
    over the canonical JSON of the index, with the public key shipped in
    ``keys/``. A catalog served without a signature still works while the
    platform does not require one, and a tampered index stops verifying.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tarfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from core.signing import (
    SIGNING_KEY_ENV,
    SigningError,
    generate_keypair,  # noqa: F401 - part of the publisher's public surface
    key_id_of,
    key_id_of_private,  # noqa: F401 - used by the CLI to name the key file
    load_public_key,
    verify_file,
    write_signature,
)

#: Index schema written by :func:`rebuild_index`.
INDEX_SCHEMA = "sagaai.marketplace.index/1"

#: Bundle schema, kept here so the publisher writes one shape.
BUNDLE_SCHEMA = "sagaai.marketplace.bundle/1"

#: Catalog folders are plural (``skills/``) while entity types are singular.
TYPE_FOLDERS = {
    "agent": "agents",
    "skill": "skills",
    "rag": "rag",
    "mcp": "mcp",
}

#: Fixed timestamp of every archive member (1980-01-01, the zip epoch).
_ARCHIVE_MTIME = 315532800

#: Semver, without the build part the platform never compares.
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

#: Charset a catalog id may use; the id reaches a filesystem path.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Paths a package must not contain, mirroring the installer's checks.
_UNSAFE_PARTS = ("..",)


class PublishError(Exception):
    """Raised when a catalog cannot be built from the given folder.

    Carries ``code``, ``message`` and ``details`` so the CLI and the platform
    can report a refusal without parsing the text.
    """

    def __init__(self, code: str, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = str(code or "publish_failed")
        self.message = str(message or "")
        self.details = details or {}


# ─── Helpers ──────────────────────────────────────────────────────────────

def type_folder(type_: str) -> str:
    """Return the catalog folder name of an entity type."""
    return TYPE_FOLDERS.get(str(type_ or "").strip(), str(type_ or "").strip())


def _now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_bytes(data: bytes) -> str:
    """Return the hex sha256 of *data*."""
    return hashlib.sha256(data).hexdigest()


#: Agent kinds a publisher may ship. An agent entity is either an
#: orchestrator (an employee bundle) or an assistant (a prompt profile); the
#: installer places each kind in its own folder.
AGENT_KINDS = ("orchestrator", "assistant")


def _check_agent_kind(entity: dict, type_: str) -> None:
    """Refuse an agent manifest whose kind the platform cannot install.

    The check lives in the publisher rather than in the installer alone: a
    catalog that accepts an unknown kind produces an entity every installation
    refuses, and the author learns about it from a user instead of from the
    publish run.
    """
    if type_ != "agent":
        return
    kind = str(entity.get("agent_kind") or "orchestrator").strip()
    if kind not in AGENT_KINDS:
        raise PublishError(
            "invalid_agent_kind",
            f"Unknown agent kind '{kind}'",
            {"agent_kind": kind, "known": list(AGENT_KINDS)},
        )


def _check_id(value: str, field: str) -> str:
    """Return a validated catalog id or raise."""
    text = str(value or "").strip()
    if not _ID_RE.match(text):
        raise PublishError(
            "invalid_id",
            f"{field} '{text or '-'}' must be lowercase [a-z0-9_-], up to 64 chars",
            {"field": field, "value": text},
        )
    return text


def _check_version(value: str) -> str:
    """Return a validated semver or raise."""
    text = str(value or "").strip()
    if not _SEMVER_RE.match(text):
        raise PublishError(
            "invalid_version",
            f"Version '{text or '-'}' is not valid semver",
            {"version": text},
        )
    return text


# ─── Deterministic package ────────────────────────────────────────────────

def collect_files(folder: str) -> Dict[str, bytes]:
    """Return ``{relative path: bytes}`` for every file under *folder*.

    Hidden files and ``__pycache__`` are skipped: they are never part of a
    package and would make the archive differ between two machines. Unsafe
    names are refused here, before anything is written anywhere.
    """
    root = os.path.abspath(folder)
    if not os.path.isdir(root):
        raise PublishError(
            "folder_missing", f"Entity folder not found: {folder}", {"folder": folder}
        )
    result: Dict[str, bytes] = {}
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            full = os.path.join(current, name)
            rel_path = os.path.relpath(full, root).replace(os.sep, "/")
            if any(part in _UNSAFE_PARTS for part in rel_path.split("/")):
                raise PublishError(
                    "unsafe_path",
                    f"Package path escapes the entity folder: {rel_path}",
                    {"path": rel_path},
                )
            if "\\" in rel_path or any(ord(ch) < 32 for ch in rel_path):
                raise PublishError(
                    "unsafe_path",
                    f"Package path contains a forbidden character: {rel_path!r}",
                    {"path": rel_path},
                )
            with open(full, "rb") as handle:
                result[rel_path] = handle.read()
    if not result:
        raise PublishError(
            "empty_folder", f"Entity folder holds no files: {folder}", {"folder": folder}
        )
    return result


def build_package(files: Dict[str, bytes]) -> bytes:
    """Build a deterministic ``tar.gz`` from ``{relative path: bytes}``.

    Every member gets the same mtime, owner and mode, and the gzip stream is
    written with ``mtime=0``, so two runs over the same files produce the
    same bytes and therefore the same sha256.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for rel_path in sorted(files):
            payload = files[rel_path]
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(payload)
            info.mtime = _ARCHIVE_MTIME
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(payload))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


# ─── Entity manifest ──────────────────────────────────────────────────────

def build_manifest(
    entity: dict,
    files: Dict[str, bytes],
    package_url: str,
) -> Tuple[dict, bytes]:
    """Return ``(manifest, package bytes)`` with every hash filled in.

    The caller supplies the descriptive part of the manifest (name,
    description, licence, preset, ...). Everything that must match the bytes
    on disk - ``files``, ``package.sha256``, ``package.size`` - is computed
    here and overwrites whatever the input carried, because a manifest whose
    hashes disagree with its package is the one failure the platform cannot
    recover from.
    """
    entity_id = _check_id(entity.get("id"), "id")
    version = _check_version(entity.get("version"))
    type_ = str(entity.get("type") or "skill").strip()
    if type_ not in TYPE_FOLDERS:
        raise PublishError(
            "invalid_type",
            f"Unknown entity type '{type_}'",
            {"type": type_, "known": sorted(TYPE_FOLDERS)},
        )
    package = build_package(files)
    _check_agent_kind(entity, type_)
    manifest = {key: value for key, value in entity.items() if key != "files"}
    if type_ == "agent":
        # The kind is recorded explicitly: a catalog entry without one looks
        # like an incomplete manifest to any consumer that reads the index.
        manifest["agent_kind"] = str(entity.get("agent_kind") or "orchestrator").strip()
    manifest.update({
        "schema": str(entity.get("schema") or "sagaai.marketplace.entity/1"),
        "type": type_,
        "id": entity_id,
        "version": version,
        "files": {path: sha256_bytes(payload) for path, payload in sorted(files.items())},
        "package": {
            **(entity.get("package") if isinstance(entity.get("package"), dict) else {}),
            "url": package_url,
            "sha256": sha256_bytes(package),
            "size": len(package),
        },
    })
    return manifest, package


# ─── Repository layout ────────────────────────────────────────────────────

def version_dir(root: str, type_: str, entity_id: str, version: str) -> str:
    """Return ``<root>/<type folder>/<id>/<version>``.

    The version sits directly under the entity folder, next to ``latest/``:
    that is the layout the platform transport resolves, and keeping the two
    in step matters more than a deeper tree.
    """
    return os.path.join(
        root, type_folder(type_), entity_id, version
    )


def _write_json(path: str, payload: dict) -> None:
    """Write JSON with a trailing newline, creating the parent folder."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _check_republish(
    target: str,
    manifest_path: str,
    package_path: str,
    extra: Dict[str, bytes],
    full: dict,
    package: bytes,
    entity_id: str,
    version: str,
) -> bool:
    """Return True when the version must be written, refuse a content change.

    Identical bytes are a no-op; the same version with other content is
    refused, because a client that pinned it would receive unreviewed bytes.
    """
    if not os.path.isfile(manifest_path):
        return True
    existing_package = b""
    if os.path.isfile(package_path):
        with open(package_path, "rb") as handle:
            existing_package = handle.read()
    same_extra = True
    for name, payload in extra.items():
        existing_extra = os.path.join(target, name)
        if not os.path.isfile(existing_extra):
            same_extra = False
            break
        with open(existing_extra, "rb") as handle:
            if handle.read() != payload:
                same_extra = False
                break
    if _read_json(manifest_path) == full and existing_package == package and same_extra:
        return False
    raise PublishError(
        "version_exists",
        f"Version {version} of '{entity_id}' is already published with other content",
        {"id": entity_id, "version": version},
    )


def _maybe_sign(path: str, private_key_path: Optional[str]) -> None:
    """Sign *path* when a key is configured; leave it alone otherwise.

    ``publish_entity`` is also called by tooling that only rewrites a tree
    (a rebuild, a migration); such a caller has no key and must not be forced
    to have one. The publishing entry point itself has no such choice.
    """
    if private_key_path or os.environ.get(SIGNING_KEY_ENV, "").strip():
        sign_manifest(path, private_key_path)


def publish_entity(
    root: str,
    entity_folder: Optional[str],
    manifest: dict,
    *,
    package_name: str = "package.tar.gz",
    extra_files: Optional[Dict[str, bytes]] = None,
    private_key_path: Optional[str] = None,
) -> dict:
    """Publish one version of an entity into a catalog tree.

    The package and its manifest are written once into the versioned folder;
    ``latest/entity.json`` is then refreshed. Re-publishing the same version
    with the same content is a no-op, with other content it is refused: a
    published version never changes under a client that pinned it.

    *entity_folder* may be ``None`` for an entity that ships no files (an MCP
    server is a preset): its manifest then carries neither ``files`` nor
    ``package``, which is what the installer expects - a package block the
    catalog cannot serve would be a promise it breaks. *private_key_path*
    signs the manifest and the refreshed pointer.

    Returns a report with ``type``, ``id``, ``version``, ``path``,
    ``package_sha256`` and ``changed``.
    """
    entity_id = _check_id(manifest.get("id"), "id")
    version = _check_version(manifest.get("version"))
    type_ = str(manifest.get("type") or "skill").strip()
    _check_agent_kind(manifest, type_)
    folder_name = type_folder(type_)

    target = version_dir(root, type_, entity_id, version)
    manifest_path = os.path.join(target, "entity.json")
    extra = dict(extra_files or {})
    if entity_folder is None and type_ not in ("mcp", "rag"):
        raise PublishError(
            "folder_required",
            f"An entity of type '{type_}' ships files and needs a folder",
            {"type": type_, "id": entity_id},
        )

    package = b""
    if entity_folder is None:
        full = {key: value for key, value in manifest.items()
                if key not in ("files", "package")}
        full.update({
            "schema": str(manifest.get("schema") or "sagaai.marketplace.entity/1"),
            "type": type_,
            "id": entity_id,
            "version": version,
        })
    else:
        files = collect_files(entity_folder)
        package_url = f"{folder_name}/{entity_id}/{version}/{package_name}"
        full, package = build_manifest(manifest, files, package_url)

    # A prebuilt index is published next to the package rather than inside it:
    # the base installer downloads it through its own URL and verifies that one
    # hash, so the block describing it is derived from the bytes about to be
    # written instead of from what the caller believed they were.
    if type_ == "rag" and "index.db" in extra:
        payload = extra["index.db"]
        distribution = dict(full.get("distribution") or {})
        distribution.setdefault("mode", "prebuilt_index")
        distribution["index_url"] = f"{folder_name}/{entity_id}/{version}/index.db"
        distribution["index_sha256"] = sha256_bytes(payload)
        distribution["index_bytes"] = len(payload)
        full["distribution"] = distribution

    package_path = os.path.join(target, package_name)
    changed = _check_republish(
        target, manifest_path, package_path, extra, full, package, entity_id, version
    )
    if changed:
        os.makedirs(target, exist_ok=True)
        if package:
            with open(package_path, "wb") as handle:
                handle.write(package)
        for name, payload in extra.items():
            with open(os.path.join(target, name), "wb") as handle:
                handle.write(payload)
        _write_json(manifest_path, full)
        _maybe_sign(manifest_path, private_key_path)

    _refresh_latest(root, type_, entity_id, private_key_path=private_key_path)
    return {
        "ok": True,
        "type": type_,
        "id": entity_id,
        "version": version,
        "path": os.path.relpath(manifest_path, root).replace(os.sep, "/"),
        "package_sha256": str((full.get("package") or {}).get("sha256") or ""),
        "file_count": len(full.get("files") or {}),
        "changed": changed,
    }


def list_versions(root: str, type_: str, entity_id: str) -> List[str]:
    """Return the published versions of one entity, newest first."""
    folder = os.path.join(root, type_folder(type_), entity_id)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    versions = [name for name in names if _SEMVER_RE.match(name)]
    return sorted(versions, key=_version_key, reverse=True)


def _version_key(version: str) -> tuple:
    """Return a sortable key of a semver string (pre-release sorts lower)."""
    core, _sep, pre = version.partition("-")
    parts = tuple(int(part) for part in core.split(".") if part.isdigit())
    return (parts, 0 if pre else 1, pre)


def _refresh_latest(
    root: str,
    type_: str,
    entity_id: str,
    *,
    private_key_path: Optional[str] = None,
) -> Optional[str]:
    """Point ``<type>/<id>/latest/entity.json`` at the newest version.

    The pointer is signed when a key is available: it is the file a client
    fetches to ask for the newest release.
    """
    versions = list_versions(root, type_, entity_id)
    if not versions:
        return None
    newest = versions[0]
    source_path = os.path.join(
        version_dir(root, type_, entity_id, newest), "entity.json"
    )
    try:
        with open(source_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    latest_path = os.path.join(
        root, type_folder(type_), entity_id, "latest", "entity.json"
    )
    _write_json(latest_path, payload)
    _maybe_sign(latest_path, private_key_path)
    return newest


# ─── Index ────────────────────────────────────────────────────────────────

def _read_json(path: str) -> Optional[dict]:
    """Read a JSON object, or None when the file is missing or broken."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _index_entry(entity: dict) -> dict:
    """Return the index summary of one entity manifest."""
    type_ = str(entity.get("type") or "skill")
    entity_id = str(entity.get("id") or "")
    version = str(entity.get("version") or "")
    entry = {
        "type": type_,
        "id": entity_id,
        "version": version,
        "name": entity.get("name"),
        "description": entity.get("description"),
        "path": f"{type_folder(type_)}/{entity_id}/{version}",
    }
    for key in ("author", "tags", "icon", "license"):
        if entity.get(key):
            entry[key] = entity[key]
    platform = entity.get("platform") if isinstance(entity.get("platform"), dict) else None
    if platform:
        entry["platform"] = platform
    # The kind travels with the index entry: the showcase tags an agent card
    # as an employee or a helper without downloading its full manifest.
    if type_ == "agent":
        entry["agent_kind"] = str(entity.get("agent_kind") or "orchestrator")
    return entry


def collect_entries(root: str) -> List[dict]:
    """Return index entries for every entity of a catalog tree."""
    entries: List[dict] = []
    for type_ in TYPE_FOLDERS:
        type_dir = os.path.join(root, type_folder(type_))
        try:
            names = sorted(os.listdir(type_dir))
        except OSError:
            continue
        for entity_id in names:
            latest = _read_json(
                os.path.join(type_dir, entity_id, "latest", "entity.json")
            )
            if latest is None:
                continue
            entries.append(_index_entry(latest))
    entries.sort(key=lambda item: (item["type"], item["id"]))
    return entries


def collect_bundles(root: str) -> List[dict]:
    """Return the bundle summaries listed in ``bundles/``."""
    folder = os.path.join(root, "bundles")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    result: List[dict] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        payload = _read_json(os.path.join(folder, name))
        if payload is None:
            continue
        bundle_id = str(payload.get("id") or name[:-5])
        result.append({
            "id": bundle_id,
            "name": payload.get("name") or bundle_id,
            "path": f"bundles/{name}",
        })
    return result


def rebuild_index(
    root: str,
    *,
    channel: str = "main",
    updated_at: Optional[str] = None,
) -> dict:
    """Rebuild ``index.json`` from the catalog tree and write it back.

    The index is derived, never hand-maintained: it lists the ``latest``
    manifest of every entity plus the bundles present. Writing it last keeps
    a failed publish out of the index entirely.
    """
    index = {
        "schema": INDEX_SCHEMA,
        "channel": str(channel or "main"),
        "updated_at": updated_at or _now_iso(),
        "entities": collect_entries(root),
    }
    bundles = collect_bundles(root)
    if bundles:
        index["bundles"] = bundles
    _write_json(os.path.join(root, "index.json"), index)
    return index


# ─── Signature ─────────────────────────────────────────────────────

def _publish_error(exc) -> "PublishError":
    """Translate a signing failure into a publish refusal."""
    return PublishError(
        str(getattr(exc, "code", "signing_failed")),
        str(getattr(exc, "message", exc)),
        dict(getattr(exc, "details", {}) or {}),
    )


def resolve_signing_key(private_key_path: Optional[str]) -> Optional[str]:
    """Return the signing key path, or None when the environment holds it.

    Publishing has no unsigned mode: without a key the run is refused here
    rather than producing a release nobody can verify.
    """
    source = str(private_key_path or "").strip()
    if source:
        return source
    if os.environ.get(SIGNING_KEY_ENV, "").strip():
        return None
    raise PublishError(
        "signing_key_missing",
        "Publishing requires the Ed25519 private key: pass --sign-key or set "
        + SIGNING_KEY_ENV,
        {},
    )


def sign_manifest(manifest_path: str, private_key_path: Optional[str] = "") -> dict:
    """Sign one manifest and drop ``<manifest>.sig`` beside it.

    An entity manifest is signed at publish time, so an installation can tell
    an entry written by the key holder from one planted in the channel.
    """
    try:
        return write_signature(manifest_path, private_key_path or None)
    except SigningError as exc:
        raise _publish_error(exc) from exc


def sign_index(root: str, private_key_path: Optional[str] = "") -> dict:
    """Sign the bytes of ``index.json`` and write ``index.json.sig``.

    The signature covers the file as stored, so a client verifies the very
    bytes it downloaded. Returns the record that was written.
    """
    index_path = os.path.join(root, "index.json")
    if not os.path.isfile(index_path):
        raise PublishError("index_missing", "index.json is missing or unreadable", {})
    return sign_manifest(index_path, private_key_path)


def verify_index_signature(root: str, public_key_path: str) -> dict:
    """Check ``index.json.sig`` against ``index.json`` and a trusted key.

    *public_key_path* may be one PEM file or a keyring folder; a folder trusts
    every readable key in it, which lets a retiring key and its replacement
    verify at the same time. A missing signature is reported, not raised.
    """
    index_path = os.path.join(root, "index.json")
    if not os.path.isfile(index_path):
        return {"ok": False, "reason": "index_missing"}
    if not os.path.isfile(index_path + ".sig"):
        return {"ok": False, "reason": "signature_missing"}
    path = str(public_key_path or "")
    key_dir = path if os.path.isdir(path) else None
    keys = None
    if key_dir is None:
        try:
            key = load_public_key(path)
        except SigningError:
            return {"ok": False, "reason": "key_unreadable"}
        keys = {key_id_of(key): key}
    result = verify_file(index_path, key_dir=key_dir, keys=keys)
    reason = str(result.get("reason") or "")
    if reason == "signature_invalid":
        reason = "index_changed"
    return {"ok": bool(result.get("ok")), "reason": reason, "key_id": result.get("key_id")}


# ─── One-call publish ──────────────────────────────────────────────────

def publish(
    root: str,
    entity_folder: Optional[str],
    manifest: dict,
    *,
    private_key_path: Optional[str] = None,
    key_id: str = "",
    channel: str = "main",
) -> dict:
    """Publish one entity version, sign every manifest and sign the index.

    The order is the contract: the immutable version is written, its manifest
    is signed, the pointers are refreshed, the index is rebuilt and signed
    last. A run without a key is refused before anything is written: an
    unsigned release looks exactly like a signed one to a client that cannot
    check it. *key_id* is accepted for compatibility and ignored.
    """
    signing_key = resolve_signing_key(private_key_path)
    report = publish_entity(root, entity_folder, manifest, private_key_path=signing_key)
    index = rebuild_index(root, channel=channel)
    index_record = sign_index(root, signing_key)
    keys_dir = os.path.join(root, "keys")
    entity_path = os.path.join(root, str(report.get("path") or ""))
    entity_check = verify_file(entity_path, key_dir=keys_dir) if os.path.isfile(entity_path) else {}
    index_check = verify_index_signature(root, keys_dir)
    report["index_entities"] = len(index.get("entities") or [])
    report["key_id"] = index_record.get("key_id")
    report["signed"] = True
    report["signature_verified"] = bool(entity_check.get("ok"))
    report["index_signature_verified"] = bool(index_check.get("ok"))
    return report
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
