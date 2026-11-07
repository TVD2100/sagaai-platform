"""
core.signing - detached Ed25519 signatures over manifest bytes.

The trust model is deliberately small. A publisher signs the exact bytes of
a manifest (``file_versions.json``, a catalog ``entity.json`` or
``index.json``) and ships ``<manifest>.sig`` next to it. A client verifies
that signature with a public key it already has on disk, so the check never
touches the network: whoever controls the download channel cannot substitute
both the file and the key that vouches for it.

Properties this module guarantees:

  * what is signed is exactly what is downloaded - no JSON canonicalisation,
    no re-serialisation between signing and verification;
  * ``key_id`` is derived from the public key itself (the first 8 bytes of
    the SHA-256 of its DER encoding), so a key cannot claim somebody else's
    identifier;
  * an unknown ``key_id`` is a refusal, never a silent pass;
  * several public keys may sit in the keyring at once, which is what makes
    a rotation possible: add the new key, sign with it, retire the old one
    once every installation has picked the new release up.

Channel policy lives here too. ``stable`` requires a signature; ``raw``,
``dev`` and any other channel warn and journal the omission instead, so a
development build stays installable while the fact is recorded.

This module imports nothing from the platform on purpose: the publisher
ships a copy of the same primitives into the catalog repository, and that
copy has to run in a clone with no SagaAI next to it.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

#: Bytes of the key digest that make up a key id (16 hex characters).
KEY_ID_BYTES = 8

#: Schema of the detached signature record written next to a manifest.
SIGNATURE_SCHEMA = "sagaai.marketplace.signature/1"

#: The only signature algorithm this build knows.
ALGORITHM = "ed25519"

#: Environment variable carrying the private key: PEM text or a file path.
SIGNING_KEY_ENV = "SAGAII_SIGNING_KEY"

#: Suffix of the detached signature file.
SIGNATURE_SUFFIX = ".sig"

#: Channels that must carry a signature (fail-closed).
STABLE_CHANNELS = ("stable",)

#: Channels where an unsigned manifest is accepted with a warning.
WARNING_CHANNELS = ("raw", "dev", "main", "beta")


class SigningError(Exception):
    """Raised when a key cannot be loaded or a signature cannot be written.

    Carries a machine-readable ``code`` so a caller reports the failure
    without parsing the message text.
    """

    def __init__(self, code: str, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = str(code or "signing_failed")
        self.message = str(message or "")
        self.details = details or {}


# ─── small helpers ──────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_hex(payload: bytes) -> str:
    """Return the hex sha256 of *payload*."""
    return hashlib.sha256(payload).hexdigest()


def canonical_json(payload) -> bytes:
    """Return the canonical JSON encoding of *payload*.

    Kept for verifying signatures written by earlier releases, which
    signed a canonical re-serialisation rather than the shipped bytes. New
    signatures never use it: what is signed is exactly what is downloaded.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def signature_path(payload_path: str) -> str:
    """Return the detached signature path of a manifest file."""
    return str(payload_path) + SIGNATURE_SUFFIX


def _serialization():
    """Import and return the ``cryptography`` serialization module.

    The import is lazy so importing :mod:`core.signing` never fails on a
    machine that has no ``cryptography``: only a signing or verification
    call needs it, and the platform declares it as a required dependency.
    """
    from cryptography.hazmat.primitives import serialization

    return serialization


def _ed25519_private():
    """Return the Ed25519 private key class."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey


def _ed25519_public():
    """Return the Ed25519 public key class."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey


# ─── Key ids ───────────────────────────────────────────────────────────

def public_key_der(public_key) -> bytes:
    """Return the DER encoding (SubjectPublicKeyInfo) of a public key."""
    serialization = _serialization()
    return public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def public_key_pem(public_key) -> bytes:
    """Return the PEM encoding of a public key."""
    serialization = _serialization()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def key_id_from_public(public_key) -> str:
    """Return the key id of a public key: 8 bytes of its DER digest.

    Deriving the identifier from the key itself is what stops one key from
    presenting another key's id: the client recomputes it from the bytes it
    holds and looks the key up by the result.
    """
    return hashlib.sha256(public_key_der(public_key)).hexdigest()[: KEY_ID_BYTES * 2]


def key_id_from_pem(raw: bytes) -> str:
    """Return the key id of a PEM-encoded public key."""
    return key_id_from_public(load_public_key_bytes(raw))


def key_id_of(key) -> str:
    """Return the key id of a private or public key object."""
    public = key.public_key() if hasattr(key, "public_key") else key
    return key_id_from_public(public)


def key_id_of_private(path: Optional[str] = None) -> str:
    """Return the key id of the private key at *path* or in the environment.

    The publisher names ``keys/<key_id>.pub`` after this value, so the file
    name and the key inside it are derived from one source.
    """
    return key_id_of(load_private_key(path))


# ─── Key loading ──────────────────────────────────────────────────────

def load_public_key_bytes(raw: bytes):
    """Return the Ed25519 public key encoded in *raw*, or raise.

    A key of another algorithm is refused rather than accepted: the whole
    trust model rests on the key being Ed25519.
    """
    serialization = _serialization()
    try:
        key = serialization.load_pem_public_key(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same refusal
        raise SigningError(
            "key_unreadable",
            "Public key is not a readable PEM document",
            {"error": str(exc)},
        ) from exc
    if not isinstance(key, _ed25519_public()):
        raise SigningError(
            "key_not_ed25519",
            "Public key is not an Ed25519 key",
            {"type": type(key).__name__},
        )
    return key


def load_public_key(path: str):
    """Load an Ed25519 public key from *path*, or raise :class:`SigningError`."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise SigningError(
            "key_missing", f"Public key not found: {path}", {"path": str(path)}
        ) from exc
    return load_public_key_bytes(raw)


def load_private_key(path: Optional[str] = None, env_var: str = SIGNING_KEY_ENV):
    """Load the Ed25519 private key from *path* or from the environment.

    The environment form accepts either the PEM text itself (what a CI
    secret holds) or a path to the file. Signing without a key is an error:
    a publish run must never fall back to emitting unsigned output.
    """
    serialization = _serialization()
    raw: Optional[bytes] = None
    source = str(path or "").strip()
    if source:
        try:
            with open(source, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise SigningError(
                "key_missing", f"Signing key not found: {source}", {"path": source}
            ) from exc
    else:
        value = os.environ.get(env_var, "")
        text = value.strip()
        if not text:
            raise SigningError(
                "key_missing",
                f"No signing key: pass a path or set {env_var}",
                {"env": env_var},
            )
        if "BEGIN" in text:
            raw = text.encode("utf-8")
        else:
            try:
                with open(os.path.expanduser(text), "rb") as handle:
                    raw = handle.read()
            except OSError as exc:
                raise SigningError(
                    "key_missing",
                    f"Signing key not found: {text}",
                    {"path": text, "env": env_var},
                ) from exc
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same refusal
        raise SigningError(
            "key_unreadable",
            "Signing key is not a readable unencrypted PEM private key",
            {"error": str(exc)},
        ) from exc
    if not isinstance(key, _ed25519_private()):
        raise SigningError(
            "key_not_ed25519",
            "Signing key is not an Ed25519 key",
            {"type": type(key).__name__},
        )
    return key


def generate_keypair(private_path: str, public_path: str, key_id: str = "") -> dict:
    """Create an Ed25519 pair, write both halves and return their ids.

    The private half is written with owner-only permissions; nothing here
    checks where the caller puts it, but every tool in this repository
    writes it outside the working tree. *key_id* is accepted for callers
    written against the earlier publisher and ignored: the identifier is
    derived from the key, so it can never disagree with a signature.
    """
    serialization = _serialization()
    private = _ed25519_private().generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key_pem(private.public_key())
    os.makedirs(os.path.dirname(os.path.abspath(private_path)), exist_ok=True)
    with open(private_path, "wb") as handle:
        handle.write(private_pem)
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(public_path)), exist_ok=True)
    with open(public_path, "wb") as handle:
        handle.write(public_pem)
    return {
        "ok": True,
        "key_id": key_id_from_public(private.public_key()),
        "public_key": str(public_path),
        "private_key": str(private_path),
        "fingerprint": sha256_hex(public_pem),
    }


# ─── Keyring ──────────────────────────────────────────────────────────

def keyring_paths(key_dir: str) -> list:
    """Return the public key files of a keyring folder, sorted.

    Both ``.pem`` and ``.pub`` files are read: the repository ships
    ``keys/ed25519-public.pem`` while a catalog may keep ``<key_id>.pub``.
    """
    folder = str(key_dir or "")
    if not folder or not os.path.isdir(folder):
        return []
    names = sorted(os.listdir(folder))
    return [
        os.path.join(folder, name)
        for name in names
        if name.lower().endswith((".pem", ".pub")) and not name.startswith(".")
    ]


def load_keyring(key_dir: str) -> Dict[str, Any]:
    """Return ``{key_id: public key}`` for every key file in *key_dir*.

    A file that cannot be read is skipped rather than fatal: one broken key
    in the folder must not stop the others from being trusted. The count of
    skipped files is available through :func:`keyring_report`.
    """
    ring: Dict[str, Any] = {}
    for path in keyring_paths(key_dir):
        try:
            key = load_public_key(path)
        except SigningError:
            continue
        ring[key_id_from_public(key)] = key
    return ring


def keyring_report(key_dir: str) -> dict:
    """Return which key files are trusted and which could not be read."""
    trusted = []
    skipped = []
    for path in keyring_paths(key_dir):
        try:
            key = load_public_key(path)
        except SigningError as exc:
            skipped.append({"path": path, "reason": exc.code})
            continue
        trusted.append({"path": path, "key_id": key_id_from_public(key)})
    return {"ok": True, "dir": str(key_dir), "trusted": trusted, "skipped": skipped}


# ─── Signing and verification ────────────────────────────────────────

def sign_bytes(payload: bytes, private_key, key_id: Optional[str] = None) -> dict:
    """Sign *payload* and return the detached signature record.

    The signature covers the bytes as they are: a caller that re-serialises
    a document between signing and shipping breaks verification, which is
    the intended behaviour - what is signed must be what is downloaded.
    """
    signature = private_key.sign(payload)
    return {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": ALGORITHM,
        "key_id": str(key_id or key_id_of(private_key)),
        "signature": base64.b64encode(signature).decode("ascii"),
        "signed_sha256": sha256_hex(payload),
        "signed_at": _now_iso(),
    }


def write_json_file(path: str, payload: dict) -> None:
    """Write *payload* as indented JSON with a trailing newline, atomically."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_signature(
    payload_path: str,
    private_key_path: Optional[str] = None,
    key_id: Optional[str] = None,
) -> dict:
    """Sign the file at *payload_path* and write ``<payload>.sig``.

    Returns the record written. A missing key raises: a publish step that
    cannot sign must stop, not quietly produce an unsigned release.
    """
    with open(payload_path, "rb") as handle:
        payload = handle.read()
    private_key = load_private_key(private_key_path)
    record = sign_bytes(payload, private_key, key_id=key_id)
    write_json_file(signature_path(payload_path), record)
    return record


def read_signature(path: str) -> Optional[dict]:
    """Read a signature file; None when it is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _decode_signature(record: dict) -> Optional[bytes]:
    """Decode the base64 signature of a record; None when malformed."""
    text = str(record.get("signature") or "").strip()
    if not text:
        return None
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def _decode_legacy_signature(record: dict) -> Optional[bytes]:
    """Decode the hex signature of a record written by an earlier release."""
    text = str(record.get("signature") or "").strip()
    if not text:
        return None
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    return raw or None


def verify_bytes(
    payload: bytes,
    record: Optional[dict],
    keys: Dict[str, Any],
    legacy_document: Optional[dict] = None,
) -> dict:
    """Verify a detached signature over *payload* against a keyring.

    Returns ``{"ok", "key_id", "reason"}``. Reasons:

    ``verified``           the signature matches and the key is trusted;
    ``signature_missing``  there is no signature record at all;
    ``signature_malformed`` the record cannot be parsed or decoded;
    ``unknown_key_id``     the record names a key the keyring does not hold;
    ``signature_invalid``  the bytes do not match the signature.

    An unknown key id is a refusal: accepting it would let anybody publish
    under a key of their own choice.

    A record written by an earlier release carries a hex signature over the
    canonical JSON of the parsed document. *legacy_document* lets such a
    record verify as well, so publishing an update does not invalidate an
    already shipped catalog; new records never take that path.
    """
    if not isinstance(record, dict):
        return {"ok": False, "key_id": None, "reason": "signature_missing"}
    key_id = str(record.get("key_id") or "").strip()
    algorithm = str(record.get("algorithm") or ALGORITHM).strip().lower()
    if algorithm != ALGORITHM:
        return {"ok": False, "key_id": key_id or None, "reason": "signature_malformed"}
    signature = _decode_signature(record)
    message = payload
    if signature is None:
        signature = _decode_legacy_signature(record)
        if signature is None or legacy_document is None:
            return {"ok": False, "key_id": key_id or None, "reason": "signature_malformed"}
        message = canonical_json(legacy_document)
    if not key_id:
        return {"ok": False, "key_id": None, "reason": "signature_malformed"}
    key = (keys or {}).get(key_id)
    if key is None:
        return {"ok": False, "key_id": key_id, "reason": "unknown_key_id"}
    from cryptography.exceptions import InvalidSignature

    try:
        key.verify(signature, message)
    except InvalidSignature:
        return {"ok": False, "key_id": key_id, "reason": "signature_invalid"}
    return {"ok": True, "key_id": key_id, "reason": "verified"}


def verify_file(
    payload_path: str,
    key_dir: Optional[str] = None,
    keys: Optional[Dict[str, Any]] = None,
    signature_file: Optional[str] = None,
) -> dict:
    """Verify the detached signature of a manifest on disk.

    With *keys* the keyring is given directly; otherwise it is loaded from
    *key_dir*. Either way the check is local - no key is ever downloaded.
    """
    path = str(payload_path)
    record = read_signature(signature_file or signature_path(path))
    if record is None:
        return {"ok": False, "key_id": None, "reason": "signature_missing", "path": path}
    ring = keys if keys is not None else load_keyring(key_dir or "")
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError:
        return {"ok": False, "key_id": None, "reason": "payload_missing", "path": path}
    legacy_document = None
    if _decode_signature(record) is None:
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        legacy_document = parsed if isinstance(parsed, dict) else None
    result = verify_bytes(payload, record, ring, legacy_document=legacy_document)
    result["path"] = path
    declared = str(record.get("signed_sha256") or "")
    if result["ok"] and declared and declared != sha256_hex(payload):
        # The signature already covers the bytes; this extra check only makes
        # a hand-edited record say so instead of failing on the key check.
        result = {"ok": False, "key_id": result.get("key_id"), "reason": "signature_invalid", "path": path}
    return result


# ─── Channel policy ─────────────────────────────────────────────────────

def channel_name(channel: Optional[str]) -> str:
    """Return the bare channel name of a channel URL or name.

    A channel may be given as a URL (``https://host/TVD2100/repo/main``) or
    as a name (``stable``); only the trailing segment decides the policy.
    """
    text = str(channel or "").strip().rstrip("/")
    if not text:
        return "stable"
    if "://" in text or os.sep in text:
        return text.rsplit("/", 1)[-1].strip() or "stable"
    return text


def channel_policy(channel: Optional[str], require_signature: bool = False) -> dict:
    """Return how a channel treats an unsigned manifest.

    ``enforced`` means a missing or invalid signature is a refusal. The
    explicit setting wins: a user who turned signatures on keeps them on for
    a development channel as well.
    """
    name = channel_name(channel)
    enforced = bool(require_signature) or name in STABLE_CHANNELS
    return {
        "channel": name,
        "enforced": enforced,
        "warn": not enforced,
        "mode": "require" if enforced else "warn",
        "reason": (
            "signature required on the stable channel"
            if name in STABLE_CHANNELS
            else ("signature required by the marketplace setting" if require_signature
                  else "unsigned manifests are accepted with a warning")
        ),
    }


def policy_for(channel: Optional[str], settings: Optional[dict] = None) -> dict:
    """Return the channel policy from marketplace settings (or a plain channel)."""
    settings = settings or {}
    return channel_policy(
        channel or settings.get("channel") or "main",
        bool(settings.get("require_signature")),
    )


# ─── Journal ───────────────────────────────────────────────────────────────

def append_journal(path: str, record: dict) -> bool:
    """Append one JSON line to a journal; False when it could not be written.

    The journal is the durable trace of an accepted-unsigned decision: a
    warning the user dismissed must still be findable afterwards. A failure
    to write it never fails the operation it describes.
    """
    if not path:
        return False
    entry = {"at": _now_iso()}
    entry.update(record or {})
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
