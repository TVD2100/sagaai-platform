"""
test_signing.py - detached Ed25519 signatures over manifest bytes.

The tests pin the properties the trust model rests on: the key id is derived
from the key, a signature covers exactly the bytes it was computed over, an
unknown key is a refusal, several keys may be trusted at once so a rotation
never breaks an installation, and the channel policy decides between a hard
refusal and a journalled warning.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import signing  # noqa: E402

# ─── helpers ───────────────────────────────────────────────────────────────

def _pair(tmp_path, name="key"):
    """Create a key pair under *tmp_path* and return (private path, key id)."""
    private = tmp_path / name / "private.pem"
    public = tmp_path / name / "public.pem"
    report = signing.generate_keypair(str(private), str(public))
    return private, public, report["key_id"]


def _keyring(tmp_path, *pairs):
    """Write the public halves of *pairs* into one keyring folder."""
    folder = tmp_path / "keyring"
    folder.mkdir(parents=True, exist_ok=True)
    for index, (_private, public, _key_id) in enumerate(pairs):
        (folder / f"key-{index}.pem").write_bytes(public.read_bytes())
    return folder


def _manifest(tmp_path, text="{\"units\": {}, \"selectable\": {}}\n"):
    """Write a small manifest file and return its path."""
    path = tmp_path / "file_versions.json"
    path.write_bytes(text.encode("utf-8"))
    return path


# ─── key identity ────────────────────────────────────────────────────────

class TestKeyId:
    def test_the_id_is_derived_from_the_key(self, tmp_path):
        _private, public, key_id = _pair(tmp_path)

        assert signing.key_id_from_pem(public.read_bytes()) == key_id
        assert len(key_id) == signing.KEY_ID_BYTES * 2
        assert all(char in "0123456789abcdef" for char in key_id)

    def test_two_keys_get_different_ids(self, tmp_path):
        _first_private, _first_public, first_id = _pair(tmp_path, "one")
        _second_private, _second_public, second_id = _pair(tmp_path, "two")

        assert first_id != second_id

    def test_a_non_ed25519_key_is_refused(self, tmp_path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        with pytest.raises(signing.SigningError) as excinfo:
            signing.load_public_key_bytes(pem)

        assert excinfo.value.code == "key_not_ed25519"

    def test_the_private_key_is_not_world_readable(self, tmp_path):
        private, _public, _key_id = _pair(tmp_path)

        assert os.stat(private).st_mode & 0o777 == 0o600


# ─── signing and verification ─────────────────────────────────────────

class TestSignAndVerify:
    def test_a_signed_manifest_verifies(self, tmp_path):
        private, _public, key_id = _pair(tmp_path)
        path = _manifest(tmp_path)

        record = signing.write_signature(str(path), str(private))
        result = signing.verify_file(str(path), str(_keyring(tmp_path, (private, _public, key_id))))

        assert record["key_id"] == key_id
        assert record["algorithm"] == "ed25519"
        assert result["ok"] is True
        assert result["reason"] == "verified"
        assert result["key_id"] == key_id

    def test_one_changed_byte_breaks_the_signature(self, tmp_path):
        """The signature covers the bytes, not the parsed document."""
        private, public, key_id = _pair(tmp_path)
        path = _manifest(tmp_path)
        signing.write_signature(str(path), str(private))
        original = path.read_bytes()
        path.write_bytes(original[:-2] + b" }" + original[-1:])

        result = signing.verify_file(str(path), str(_keyring(tmp_path, (private, public, key_id))))

        assert result["ok"] is False
        assert result["reason"] in ("signature_invalid", "signature_malformed")

    def test_an_edited_record_is_refused(self, tmp_path):
        private, public, key_id = _pair(tmp_path)
        path = _manifest(tmp_path)
        signing.write_signature(str(path), str(private))
        signature_file = signing.signature_path(str(path))
        record = json.loads(Path(signature_file).read_text(encoding="utf-8"))
        record["signature"] = "not-base64!!"
        Path(signature_file).write_text(json.dumps(record), encoding="utf-8")

        result = signing.verify_file(str(path), str(_keyring(tmp_path, (private, public, key_id))))

        assert result["ok"] is False
        assert result["reason"] == "signature_malformed"

    def test_a_missing_signature_is_reported(self, tmp_path):
        private, public, key_id = _pair(tmp_path)
        path = _manifest(tmp_path)

        result = signing.verify_file(str(path), str(_keyring(tmp_path, (private, public, key_id))))

        assert result["ok"] is False
        assert result["reason"] == "signature_missing"

    def test_an_unknown_key_id_is_a_refusal(self, tmp_path):
        """A signature under a key the client does not hold is not trusted."""
        private, _public, _key_id = _pair(tmp_path, "signer")
        _other_private, other_public, other_id = _pair(tmp_path, "other")
        path = _manifest(tmp_path)
        signing.write_signature(str(path), str(private))
        folder = _keyring(tmp_path, (_other_private, other_public, other_id))

        result = signing.verify_file(str(path), str(folder))

        assert result["ok"] is False
        assert result["reason"] == "unknown_key_id"

    def test_signing_without_a_key_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv(signing.SIGNING_KEY_ENV, raising=False)
        path = _manifest(tmp_path)

        with pytest.raises(signing.SigningError) as excinfo:
            signing.write_signature(str(path))

        assert excinfo.value.code == "key_missing"
        assert not Path(signing.signature_path(str(path))).exists()

    def test_the_key_can_come_from_the_environment(self, tmp_path, monkeypatch):
        private, public, key_id = _pair(tmp_path)
        monkeypatch.setenv(signing.SIGNING_KEY_ENV, private.read_text(encoding="utf-8"))
        path = _manifest(tmp_path)

        record = signing.write_signature(str(path))
        result = signing.verify_file(str(path), str(_keyring(tmp_path, (private, public, key_id))))

        assert record["key_id"] == key_id
        assert result["ok"] is True

    def test_the_environment_may_name_a_key_file(self, tmp_path, monkeypatch):
        private, _public, key_id = _pair(tmp_path)
        monkeypatch.setenv(signing.SIGNING_KEY_ENV, str(private))

        assert signing.key_id_of(signing.load_private_key()) == key_id


# ─── rotation ──────────────────────────────────────────────────────────────

class TestRotation:
    def test_both_keys_are_trusted_at_once(self, tmp_path):
        old_private, old_public, old_id = _pair(tmp_path, "old")
        new_private, new_public, new_id = _pair(tmp_path, "new")
        folder = _keyring(tmp_path, (old_private, old_public, old_id), (new_private, new_public, new_id))
        first = _manifest(tmp_path)
        second_dir = tmp_path / "second"
        second_dir.mkdir()
        second = second_dir / "file_versions.json"
        second.write_bytes(first.read_bytes())
        signing.write_signature(str(first), str(old_private))
        signing.write_signature(str(second), str(new_private))

        assert signing.verify_file(str(first), str(folder))["ok"] is True
        assert signing.verify_file(str(second), str(folder))["ok"] is True

    def test_retiring_a_key_stops_accepting_it(self, tmp_path):
        old_private, old_public, old_id = _pair(tmp_path, "old")
        new_private, new_public, new_id = _pair(tmp_path, "new")
        path = _manifest(tmp_path)
        signing.write_signature(str(path), str(old_private))
        full = _keyring(tmp_path, (old_private, old_public, old_id), (new_private, new_public, new_id))
        assert signing.verify_file(str(path), str(full))["ok"] is True

        retired = tmp_path / "retired"
        retired.mkdir()
        (retired / "only-new.pem").write_bytes(new_public.read_bytes())
        result = signing.verify_file(str(path), str(retired))

        assert result["ok"] is False
        assert result["reason"] == "unknown_key_id"

    def test_an_unreadable_key_file_does_not_hide_the_others(self, tmp_path):
        private, public, key_id = _pair(tmp_path)
        folder = _keyring(tmp_path, (private, public, key_id))
        (folder / "broken.pem").write_text("not a key", encoding="utf-8")
        path = _manifest(tmp_path)
        signing.write_signature(str(path), str(private))

        report = signing.keyring_report(str(folder))
        result = signing.verify_file(str(path), str(folder))

        assert [item["key_id"] for item in report["trusted"]] == [key_id]
        assert [item["reason"] for item in report["skipped"]] == ["key_unreadable"]
        assert result["ok"] is True


# ─── channel policy ────────────────────────────────────────────────────────

class TestChannelPolicy:
    def test_the_channel_name_is_the_trailing_segment(self):
        assert signing.channel_name("https://raw.githubusercontent.com/o/r/stable") == "stable"
        assert signing.channel_name("raw/") == "raw"
        assert signing.channel_name("") == "stable"

    def test_stable_requires_a_signature(self):
        policy = signing.channel_policy("https://host/repo/stable")

        assert policy["enforced"] is True
        assert policy["mode"] == "require"

    def test_a_development_channel_only_warns(self):
        for channel in ("raw", "dev", "main", "https://host/repo/main"):
            policy = signing.channel_policy(channel)

            assert policy["enforced"] is False
            assert policy["mode"] == "warn"
            assert policy["warn"] is True

    def test_the_explicit_setting_wins_over_the_channel(self):
        policy = signing.policy_for("raw", {"channel": "raw", "require_signature": True})

        assert policy["enforced"] is True
        assert policy["channel"] == "raw"


# ─── journal ────────────────────────────────────────────────────────────

class TestJournal:
    def test_each_decision_lands_on_its_own_line(self, tmp_path):
        path = tmp_path / "logs" / "signature-journal.jsonl"

        assert signing.append_journal(str(path), {"event": "unsigned", "channel": "raw"})
        assert signing.append_journal(str(path), {"event": "unsigned", "channel": "dev"})

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["channel"] == "raw"
        assert json.loads(lines[0])["at"].endswith("Z")

    def test_a_missing_path_is_not_an_error(self):
        assert signing.append_journal("", {"event": "x"}) is False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
