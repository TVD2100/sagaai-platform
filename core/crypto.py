"""
core.crypto - Fernet-based encryption for secrets stored in the database.

Key storage hierarchy (security hardening):
1. ``SAGAAI_ENCRYPTION_KEY`` environment variable (highest priority).
2. External key file: ``~/.sagaai/encryption_key`` by default, overridable
   via ``SAGAAI_KEY_FILE``.  The file is created automatically with 0600
   permissions when no key exists.
3. Legacy migration: if ``DATA_DIR/.encryption_key`` exists and no other
   key source is configured, it is moved to the external key file and
   removed from ``DATA_DIR`` so the key is no longer stored next to the
   database file.

``decrypt`` raises ``cryptography.fernet.InvalidToken`` instead of
silently returning the input token when decryption fails.
"""
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet

import core.paths

_ENV_KEY_NAME = "SAGAAI_ENCRYPTION_KEY"
_ENV_KEY_FILE_NAME = "SAGAAI_KEY_FILE"


def _legacy_key_file_path() -> str:
    """Return the legacy .encryption_key path inside the current DATA_DIR.

    Reads ``core.paths.DATA_DIR`` at call time so tests that override
    ``core.paths.DATA_DIR`` get the temporary path.
    """
    return os.path.join(core.paths.DATA_DIR, ".encryption_key")


def get_key_file_path() -> str:
    """Return the active key-file path (from env or default)."""
    return os.environ.get(_ENV_KEY_FILE_NAME, "") or _default_key_file()


def _default_key_file() -> str:
    """Return the default external key file path (~/.sagaai/encryption_key)."""
    home = os.path.expanduser("~")
    return os.path.join(home, ".sagaai", "encryption_key")


def _write_key_file(path: str, key: bytes) -> None:
    """Create the key file with user-only permissions (0600)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _migrate_legacy_key(key_file: str) -> bool:
    """Move ``DATA_DIR/.encryption_key`` to the external key file.

    Returns True if a legacy key was found and migrated, False otherwise.
    """
    legacy = _legacy_key_file_path()
    if not os.path.exists(legacy):
        return False
    with open(legacy, "rb") as f:
        key = f.read().strip()
    if not key:
        return False
    if os.path.abspath(key_file) == os.path.abspath(legacy):
        return False
    _write_key_file(key_file, key)
    try:
        os.remove(legacy)
    except OSError:
        pass
    return True


def get_encryption_key() -> bytes:
    """Return the Fernet encryption key.

    Resolution order:
    1. ``SAGAAI_ENCRYPTION_KEY`` environment variable.
    2. Key file (external, not next to the DB).
    3. Legacy migration from ``DATA_DIR/.encryption_key``.
    4. Generate a new key and store it in the external key file.

    Returns:
        Raw Fernet key bytes (already base64-encoded by Fernet).
    """
    env_key = os.environ.get(_ENV_KEY_NAME, "").strip()
    if env_key:
        return env_key.encode("utf-8")

    key_file = get_key_file_path()
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            key = f.read().strip()
        if key:
            return key

    if _migrate_legacy_key(key_file):
        with open(key_file, "rb") as f:
            return f.read().strip()

    key = Fernet.generate_key()
    _write_key_file(key_file, key)
    return key


def encrypt(plaintext: str) -> str:
    """Encrypt a string and return the Fernet token as text."""
    if not plaintext:
        return ""
    key = get_encryption_key()
    f = Fernet(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a Fernet token and return the plaintext.

    Raises:
        InvalidToken: if the token cannot be decrypted with the current key.
    """
    if not token:
        return ""
    key = get_encryption_key()
    f = Fernet(key)
    return f.decrypt(token.encode("utf-8")).decode("utf-8")


def is_secret_key(key_name: str, services: dict) -> bool:
    """Return True if *key_name* matches a configured secret field."""
    if not key_name:
        return False
    for svc in services.values():
        if svc.get("config_key") == key_name:
            return True
        if svc.get("config_key2") == key_name:
            return True
    return False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
