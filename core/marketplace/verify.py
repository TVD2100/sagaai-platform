"""
core.marketplace.verify - package integrity and archive safety checks.

Three independent checks live here:

  * ``verify_package`` - the downloaded bytes hash to the declared sha256
  * ``validate_archive`` - the tar is safe to unpack (no symlinks, no path
    traversal, no absolute paths, bounded size and file count)
  * ``verify_files`` - every file of the unpacked package matches the
    per-file manifest of the entity

A package that fails any check never reaches the skills library: the
installer calls these functions while the package is still in staging.

``verify_signature`` is a deliberate stub: the manifest already carries
``signature``/``key_id`` fields and ``keys/`` exists, so enabling Ed25519
later is a change of one function body rather than a breaking format
change.
"""
from __future__ import annotations

import hashlib
import os
import tarfile
import unicodedata
from typing import Dict, Iterable, List, Optional, Set

from core.marketplace import registry, store


# Limits (D-15). A skill package is tens of kilobytes; these bounds exist to
# stop a hostile archive from exhausting disk or memory during unpacking.
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES = 5000
MAX_UNPACKED_RATIO = 3


class VerifyError(store.MarketplaceError):
    """Raised when a package fails an integrity or safety check."""


def sha256_bytes(data: bytes) -> str:
    """Return the hex sha256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    """Return the hex sha256 of a file's content."""
    return registry._sha256_file(path)


def verify_package(data: bytes, expected_sha256: str) -> str:
    """Check package bytes against their declared hash and return the hash.

    An empty declaration is an error: without a hash the download cannot be
    distinguished from a corrupted or substituted package.
    """
    expected = str(expected_sha256 or "").strip().lower()
    if not expected:
        raise VerifyError(
            "missing_package_hash",
            "Entity manifest declares no package sha256",
        )
    actual = sha256_bytes(data)
    if actual != expected:
        raise VerifyError(
            "package_hash_mismatch",
            "Package content does not match the declared sha256",
            {"expected": expected, "actual": actual, "size": len(data)},
        )
    if len(data) > MAX_ARCHIVE_BYTES:
        raise VerifyError(
            "package_too_large",
            f"Package exceeds the {MAX_ARCHIVE_BYTES} byte limit",
            {"size": len(data)},
        )
    return actual


def _normalise(name: str) -> str:
    """Return the NFC-normalised archive member name."""
    return unicodedata.normalize("NFC", name)


def _unsafe_reason(name: str) -> Optional[str]:
    """Return why an archive member name is unsafe, or None when it is fine.

    Rejected: absolute paths, Windows drive letters, backslashes, any ``..``
    component and control characters. A leading ``./`` is stripped by the
    caller before this check.
    """
    if not name:
        return "empty name"
    if name.startswith("/") or name.startswith("\\"):
        return "absolute path"
    if len(name) > 1 and name[1] == ":":
        return "drive letter"
    if "\\" in name:
        return "backslash in path"
    parts = name.split("/")
    if any(part == ".." for part in parts):
        return "parent directory escape"
    if any(ord(ch) < 32 for ch in name):
        return "control character"
    return None


def _member_name(member: tarfile.TarInfo) -> str:
    """Return the normalised relative name of an archive member.

    Only a literal ``./`` prefix is removed. ``str.lstrip("./")`` would be
    wrong here: it treats its argument as a character set, so ``../evil``
    would silently become ``evil`` instead of being rejected by
    :func:`_unsafe_reason`. Danger is checked on the raw name, this helper
    only normalises the stored path.
    """
    name = _normalise(member.name)
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def validate_archive(path: str) -> List[tarfile.TarInfo]:
    """Validate a tar archive and return its file members.

    Only regular files and directories are allowed: symlinks, hardlinks,
    device nodes and FIFOs are rejected, because the unpacker would either
    follow them out of the staging folder or block on them. The declared
    sizes are checked before extraction so a tar bomb is refused without
    writing anything to disk.
    """
    if not os.path.isfile(path):
        raise VerifyError("package_missing", f"Package file not found: {path}")
    size = os.path.getsize(path)
    if size > MAX_ARCHIVE_BYTES:
        raise VerifyError(
            "package_too_large",
            f"Package exceeds the {MAX_ARCHIVE_BYTES} byte limit",
            {"size": size},
        )
    try:
        archive = tarfile.open(path, "r:*")
    except tarfile.TarError as exc:
        raise VerifyError(
            "invalid_archive",
            f"Package is not a readable tar archive: {exc}",
        ) from exc

    members: List[tarfile.TarInfo] = []
    seen: Set[str] = set()
    total = 0
    try:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                kind = "symlink" if member.issym() else (
                    "hardlink" if member.islnk() else "special file"
                )
                raise VerifyError(
                    "unsafe_member",
                    f"Archive contains a {kind}: {member.name}",
                    {"member": member.name, "kind": kind},
                )
            name = _member_name(member)
            reason = _unsafe_reason(_normalise(member.name)) or _unsafe_reason(name)
            if reason:
                raise VerifyError(
                    "unsafe_path",
                    f"Archive member rejected ({reason}): {member.name}",
                    {"member": member.name, "reason": reason},
                )
            if name in seen:
                raise VerifyError(
                    "duplicate_member",
                    f"Archive contains a duplicate member: {name}",
                    {"member": name},
                )
            seen.add(name)
            if member.size > MAX_FILE_BYTES:
                raise VerifyError(
                    "file_too_large",
                    f"Archive member exceeds the per-file limit: {name}",
                    {"member": name, "size": member.size},
                )
            total += member.size
            if total > MAX_ARCHIVE_BYTES * MAX_UNPACKED_RATIO:
                raise VerifyError(
                    "archive_too_large",
                    "Unpacked archive exceeds the size limit",
                    {"unpacked": total},
                )
            if len(seen) > MAX_FILES:
                raise VerifyError(
                    "too_many_files",
                    f"Archive contains more than {MAX_FILES} files",
                )
            members.append(member)
    finally:
        archive.close()
    if not members:
        raise VerifyError("empty_archive", "Package contains no files")
    return members


def extract_archive(
    path: str,
    destination: str,
    members: Optional[Iterable[tarfile.TarInfo]] = None,
    skip: Optional[Iterable[str]] = None,
) -> List[str]:
    """Extract validated members into *destination* and return their names.

    *skip* lists relative paths (``scripts/x.py``) that must not be written -
    this is how ``allow_code = false`` keeps executable files out of the
    install without a second archive pass. Paths are re-checked here even
    though :func:`validate_archive` already did it: the two functions are
    also called independently.
    """
    skipped = {_normalise(item).lstrip("./").strip("/") for item in (skip or ())}
    os.makedirs(destination, exist_ok=True)
    written: List[str] = []
    archive = tarfile.open(path, "r:*")
    try:
        source = list(members) if members is not None else [
            m for m in archive.getmembers()
        ]
        root = os.path.abspath(destination)
        for member in source:
            if member.isdir():
                continue
            if not member.isfile():
                continue
            name = _member_name(member)
            reason = _unsafe_reason(_normalise(member.name)) or _unsafe_reason(name)
            if reason:
                raise VerifyError(
                    "unsafe_path",
                    f"Archive member rejected ({reason}): {member.name}",
                )
            if name in skipped:
                continue
            target = os.path.abspath(os.path.join(root, name))
            if os.path.commonpath([root, target]) != root:
                raise VerifyError(
                    "unsafe_path",
                    f"Archive member escapes the staging folder: {name}",
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with extracted, open(target, "wb") as handle:
                while True:
                    chunk = extracted.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            written.append(name)
    finally:
        archive.close()
    return written


def verify_files(staging: str, files: Dict[str, str]) -> List[str]:
    """Return the relative paths whose content differs from *files*.

    ``files`` is the ``{path: sha256}`` manifest of the entity. A path that
    is listed but missing from *staging* counts as a mismatch - a partial
    unpack must never be committed.
    """
    mismatched: List[str] = []
    for rel_path, expected in (files or {}).items():
        full = os.path.join(staging, rel_path)
        if not os.path.isfile(full):
            mismatched.append(rel_path)
            continue
        if sha256_file(full) != str(expected or "").lower():
            mismatched.append(rel_path)
    return mismatched


def verify_signature(
    entity: dict,
    raw_index: Optional[bytes] = None,
    key_dir: Optional[str] = None,
) -> dict:
    """Verify the catalog signature of an entity manifest.

    Signature verification (Ed25519 over canonical JSON) is not part of this
    slice. When ``marketplace.require_signature`` is false - the default -
    the check passes and reports ``verified: False, enforced: False`` so the
    caller can record the state. When the flag is true the call fails with
    ``signature_not_implemented`` instead of silently accepting an unsigned
    package: a configuration that demands signatures must not be satisfied
    by a stub. ``keys/`` holds the trust anchors once signing lands.
    """
    required = False
    try:
        from core.marketplace import config as marketplace_config

        required = bool(marketplace_config.marketplace_settings().get("require_signature"))
    except Exception:
        required = False
    if required:
        raise VerifyError(
            "signature_not_implemented",
            "Signature verification is enabled but not implemented in this build",
            {"id": entity.get("id"), "key_id": entity.get("key_id")},
        )
    return {
        "verified": False,
        "enforced": False,
        "key_id": entity.get("key_id"),
        "reason": "signature verification is not implemented in this slice",
    }


def is_code_path(rel_path: str) -> bool:
    """True when a package path belongs to the executable part of a skill.

    Skills keep their runnable helpers under ``scripts/``; agents keep
    ``functions/``. Both are gated by ``allow_code`` (D-12).
    """
    normalized = _normalise(rel_path).replace(os.sep, "/").lstrip("./")
    return normalized.startswith("scripts/") or normalized.startswith("functions/")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
