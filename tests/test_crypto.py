"""
test_crypto.py - Security tests for core.crypto key storage.

Verifies:
1. New key is created in an external file, not next to the DB.
2. A legacy key in DATA_DIR is migrated to the external file and removed.
3. encrypt/decrypt round-trip works.
4. Decryption of a corrupted/foreign token raises InvalidToken.
5. SAGAAI_ENCRYPTION_KEY env var takes precedence over the key file.
6. SAGAAI_KEY_FILE env var overrides the default key file location.
"""
import importlib
import os
import stat
import sys

import pytest
from cryptography.fernet import Fernet, InvalidToken

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_crypto(tmp_path, monkeypatch):
    """Give each test its own DATA_DIR and key-file location."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)

    key_file = str(tmp_path / "keys" / "encryption_key")

    monkeypatch.setenv("SAGAAI_DATA_DIR", data_dir)
    monkeypatch.setenv("SAGAAI_KEY_FILE", key_file)
    monkeypatch.delenv("SAGAAI_ENCRYPTION_KEY", raising=False)

    # Reload paths + crypto so module-level constants pick up the env vars
    import core.paths as paths_mod
    importlib.reload(paths_mod)
    import core.crypto as crypto_mod
    importlib.reload(crypto_mod)

    yield

    importlib.reload(crypto_mod)


# --- Key file location -------------------------------------------------------

def test_key_is_created_in_external_file_not_next_to_db():
    from core.crypto import get_encryption_key, get_key_file_path

    key = get_encryption_key()
    key_file = get_key_file_path()

    assert os.path.exists(key_file)
    with open(key_file, "rb") as f:
        assert f.read().strip() == key

    # No legacy key file should be created in DATA_DIR (where the DB lives)
    legacy = os.path.join(os.environ["SAGAAI_DATA_DIR"], ".encryption_key")
    assert not os.path.exists(legacy)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Unix permissions not supported on Windows")
def test_key_file_permissions_are_0600():
    from core.crypto import get_encryption_key, get_key_file_path

    get_encryption_key()
    key_file = get_key_file_path()

    mode = stat.S_IMODE(os.stat(key_file).st_mode)
    assert mode == 0o600


def test_encrypt_decrypt_round_trip():
    from core.crypto import encrypt, decrypt

    plain = "sk-test-secret-123"
    token = encrypt(plain)
    assert token != plain
    assert decrypt(token) == plain


def test_encrypt_empty_returns_empty():
    from core.crypto import encrypt, decrypt

    assert encrypt("") == ""
    assert decrypt("") == ""


def test_decrypt_invalid_token_raises():
    from core.crypto import decrypt

    with pytest.raises(InvalidToken):
        decrypt("not-a-valid-fernet-token")


def test_decrypt_token_from_foreign_key_raises():
    from core.crypto import encrypt, decrypt
    from cryptography.fernet import Fernet as F

    other_key = F.generate_key()
    other = F(other_key)
    foreign_token = other.encrypt(b"secret").decode("utf-8")

    with pytest.raises(InvalidToken):
        decrypt(foreign_token)


# --- Legacy migration --------------------------------------------------------

def test_legacy_key_is_migrated_and_removed(tmp_path, monkeypatch):
    """When DATA_DIR/.encryption_key exists it must be migrated to the
    external key file and removed from DATA_DIR."""
    data_dir = os.environ["SAGAAI_DATA_DIR"]
    key_file = os.environ["SAGAAI_KEY_FILE"]

    legacy_key = Fernet.generate_key()
    legacy_path = os.path.join(data_dir, ".encryption_key")
    with open(legacy_path, "wb") as f:
        f.write(legacy_key)

    from core.crypto import get_encryption_key

    key = get_encryption_key()
    assert key == legacy_key

    # Legacy file is gone, external file holds the key
    assert not os.path.exists(legacy_path)
    assert os.path.exists(key_file)
    with open(key_file, "rb") as f:
        assert f.read().strip() == legacy_key


def test_no_legacy_file_does_not_fail():
    from core.crypto import get_encryption_key

    key = get_encryption_key()
    assert key


# --- Environment variable precedence ----------------------------------------

def test_env_key_takes_precedence(monkeypatch):
    from core.crypto import get_encryption_key

    env_key = Fernet.generate_key()
    monkeypatch.setenv("SAGAAI_ENCRYPTION_KEY", env_key.decode("utf-8"))

    # Even if a key file already exists, the env var wins
    from core.crypto import get_key_file_path
    key_file = get_key_file_path()
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    with open(key_file, "wb") as f:
        f.write(Fernet.generate_key())

    assert get_encryption_key() == env_key


def test_env_key_file_override(monkeypatch, tmp_path):
    from core.crypto import get_key_file_path

    custom = str(tmp_path / "custom" / "my.key")
    monkeypatch.setenv("SAGAAI_KEY_FILE", custom)

    assert get_key_file_path() == custom
