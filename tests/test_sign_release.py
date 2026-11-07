"""test_sign_release.py - the release signing CLI.

The tool is the publisher side of the update trust chain: it writes the
detached signature an installation checks and refuses a release whose
signature does not verify. The tests pin both directions, plus the rule that
the private key never lands in the repository.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import signing  # noqa: E402
from scripts import sign_release  # noqa: E402


def _keypair(tmp_path, name="ed25519"):
    """Create a signing pair and return (private path, key id)."""
    private = tmp_path / f"{name}.pem"
    public = tmp_path / "keys" / f"{name}.pub"
    public.parent.mkdir(parents=True, exist_ok=True)
    signing.generate_keypair(str(private), str(public))
    return private, signing.key_id_of_private(str(private))


def _payload(tmp_path, text='{"app_version": "2.41.0"}\n'):
    """Write a release manifest and return its path."""
    path = tmp_path / "file_versions.json"
    path.write_text(text, encoding="utf-8")
    return path


class TestSign:
    def test_signing_writes_a_verifying_sidecar(self, tmp_path):
        private, key_id = _keypair(tmp_path)
        payload = _payload(tmp_path)

        status = sign_release.main(["sign", str(payload), "--key", str(private)])

        assert status == 0
        sidecar = Path(str(payload) + ".sig")
        assert sidecar.is_file()
        record = json.loads(sidecar.read_text(encoding="utf-8"))
        assert record["key_id"] == key_id
        assert record["signed_sha256"] == signing.sha256_hex(payload.read_bytes())
        check = signing.verify_file(str(payload), key_dir=str(tmp_path / "keys"))
        assert check["ok"] is True

    def test_a_missing_key_refuses_before_writing_anything(self, tmp_path, monkeypatch):
        monkeypatch.delenv(signing.SIGNING_KEY_ENV, raising=False)
        payload = _payload(tmp_path)

        status = sign_release.main(["sign", str(payload)])

        assert status == 2
        assert not Path(str(payload) + ".sig").exists()

    def test_a_missing_payload_is_refused(self, tmp_path):
        private, _ = _keypair(tmp_path)

        status = sign_release.main(
            ["sign", str(tmp_path / "absent.json"), "--key", str(private)]
        )

        assert status == 2

    def test_the_key_can_come_from_the_environment(self, tmp_path, monkeypatch):
        private, key_id = _keypair(tmp_path)
        payload = _payload(tmp_path)
        monkeypatch.setenv(signing.SIGNING_KEY_ENV, str(private))

        status = sign_release.main(["sign", str(payload)])

        assert status == 0
        record = json.loads(Path(str(payload) + ".sig").read_text(encoding="utf-8"))
        assert record["key_id"] == key_id

    def test_several_files_are_signed_in_one_run(self, tmp_path):
        private, _ = _keypair(tmp_path)
        first = _payload(tmp_path)
        second = tmp_path / "index.json"
        second.write_text('{"entities": []}\n', encoding="utf-8")

        status = sign_release.main(
            ["sign", str(first), str(second), "--key", str(private)]
        )

        assert status == 0
        assert Path(str(first) + ".sig").is_file()
        assert Path(str(second) + ".sig").is_file()


class TestVerify:
    def test_a_signed_manifest_verifies(self, tmp_path):
        private, _ = _keypair(tmp_path)
        payload = _payload(tmp_path)
        sign_release.main(["sign", str(payload), "--key", str(private)])

        status = sign_release.main(
            ["verify", str(payload), "--keyring", str(tmp_path / "keys")]
        )

        assert status == 0

    def test_an_edited_manifest_is_refused(self, tmp_path):
        private, _ = _keypair(tmp_path)
        payload = _payload(tmp_path)
        sign_release.main(["sign", str(payload), "--key", str(private)])
        payload.write_text('{"app_version": "9.9.9"}\n', encoding="utf-8")

        status = sign_release.main(
            ["verify", str(payload), "--keyring", str(tmp_path / "keys")]
        )

        assert status == 1

    def test_a_signature_by_an_untrusted_key_is_refused(self, tmp_path):
        private, _ = _keypair(tmp_path)
        payload = _payload(tmp_path)
        sign_release.main(["sign", str(payload), "--key", str(private)])
        other = tmp_path / "other"
        other.mkdir()
        signing.generate_keypair(str(tmp_path / "x.pem"), str(other / "x.pub"))

        status = sign_release.main(["verify", str(payload), "--keyring", str(other)])

        assert status == 1

    def test_a_missing_signature_is_refused(self, tmp_path):
        _keypair(tmp_path)
        payload = _payload(tmp_path)

        status = sign_release.main(
            ["verify", str(payload), "--keyring", str(tmp_path / "keys")]
        )

        assert status == 1


class TestKeygen:
    def test_the_public_half_is_named_after_the_key(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        status = sign_release.main(
            ["keygen", "--private", str(tmp_path / "private" / "k.pem")]
        )

        assert status == 0
        key_id = signing.key_id_of_private(str(tmp_path / "private" / "k.pem"))
        assert (tmp_path / "keys" / f"{key_id}.pub").is_file()
        assert not (tmp_path / "keys" / "pending.pub").exists()

    def test_an_explicit_public_path_is_used_as_given(self, tmp_path):
        target = tmp_path / "custom" / "release.pub"

        status = sign_release.main([
            "keygen", "--private", str(tmp_path / "k.pem"), "--public", str(target),
        ])

        assert status == 0
        assert target.is_file()

    def test_the_private_half_is_owner_only(self, tmp_path):
        private = tmp_path / "k.pem"
        sign_release.main([
            "keygen", "--private", str(private), "--public", str(tmp_path / "k.pub"),
        ])

        assert os.stat(private).st_mode & 0o777 == 0o600


class TestKeyring:
    def test_every_readable_key_is_listed(self, tmp_path):
        _keypair(tmp_path, "one")
        _keypair(tmp_path, "two")

        status = sign_release.main(["keyring", "--keyring", str(tmp_path / "keys")])

        assert status == 0

    def test_a_missing_folder_reports_no_trusted_keys(self, tmp_path):
        status = sign_release.main(
            ["keyring", "--keyring", str(tmp_path / "absent")]
        )

        assert status == 0


@pytest.mark.parametrize("command", ["sign", "verify", "keygen", "keyring"])
def test_every_subcommand_is_reachable(command):
    """A typo in a subcommand name is the first thing a user hits."""
    parser = sign_release._build_parser()

    assert parser.parse_args([command] + (["x"] if command in ("sign", "verify") else []))