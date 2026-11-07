"""
test_marketplace_limits.py - package integrity and archive safety.

A package is the only thing the marketplace brings in from outside, so the
checks here matter more than any other test module: path traversal,
symlinks, a tar bomb and a corrupted hash must all be rejected before a
single byte lands in the skills library. The limits themselves come from
D-15 and match the ones the skills library already applies to ZIP files.
"""
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.marketplace import verify
from tests.marketplace_fixtures import (  # noqa: E402
    make_package_tar,
    marketplace_data_dir,  # noqa: F401 - fixture
    sha256_hex,
)


def _write_package(tmp_path: Path, members) -> Path:
    """Write a tar.gz from raw ``TarInfo``/payload pairs and return its path."""
    path = tmp_path / "package.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for info, payload in members:
            archive.addfile(info, io.BytesIO(payload))
    return path


def _file_info(name: str, payload: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mode = 0o644
    return info


class TestPackageHash:
    def test_matching_hash_passes(self, tmp_path):
        data = b"package content"
        assert verify.verify_package(data, sha256_hex(data)) == sha256_hex(data)

    def test_mismatch_is_rejected(self, tmp_path):
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.verify_package(b"tampered", sha256_hex(b"original"))
        assert excinfo.value.code == "package_hash_mismatch"
        # The details must show both hashes for a useful error message.
        assert excinfo.value.details["actual"] != excinfo.value.details["expected"]

    def test_missing_hash_is_rejected(self):
        # Without a declared hash the download cannot be distinguished from
        # a substituted package, so an empty declaration is an error.
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.verify_package(b"data", "")
        assert excinfo.value.code == "missing_package_hash"

    def test_oversized_package_is_rejected(self, monkeypatch):
        monkeypatch.setattr(verify, "MAX_ARCHIVE_BYTES", 4)
        data = b"12345"
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.verify_package(data, sha256_hex(data))
        assert excinfo.value.code == "package_too_large"


class TestArchiveValidation:
    def test_valid_archive_lists_members(self, tmp_path):
        path = _write_package(tmp_path, [
            (_file_info("SKILL.md", b"body"), b"body"),
            (_file_info("scripts/run.py", b"print(1)"), b"print(1)"),
        ])
        members = verify.validate_archive(str(path))
        assert {m.name for m in members} == {"SKILL.md", "scripts/run.py"}

    def test_missing_file_is_rejected(self, tmp_path):
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(tmp_path / "absent.tar.gz"))
        assert excinfo.value.code == "package_missing"

    def test_not_a_tar_is_rejected(self, tmp_path):
        path = tmp_path / "garbage.tar.gz"
        path.write_bytes(b"not a tar archive at all")
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.code == "invalid_archive"

    def test_empty_archive_is_rejected(self, tmp_path):
        path = _write_package(tmp_path, [])
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.code == "empty_archive"


class TestPathSafety:
    @pytest.mark.parametrize("name", [
        "../escape.md",
        "nested/../../escape.md",
        "/absolute.md",
        "C:/windows.md",
        "back\\slash.md",
    ])
    def test_unsafe_paths_are_rejected(self, tmp_path, name):
        path = _write_package(tmp_path, [(_file_info(name, b"x"), b"x")])
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.code == "unsafe_path"

    def test_extraction_never_writes_outside_the_root(self, tmp_path):
        path = _write_package(tmp_path, [
            (_file_info("SKILL.md", b"body"), b"body"),
        ])
        members = verify.validate_archive(str(path))
        destination = tmp_path / "staging"
        verify.extract_archive(str(path), str(destination), members)
        assert (destination / "SKILL.md").read_bytes() == b"body"
        assert not (tmp_path.parent / "escape.md").exists()

    def test_symlink_is_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        path = _write_package(tmp_path, [(info, b"")])
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.code == "unsafe_member"
        assert excinfo.value.details["kind"] == "symlink"

    def test_hardlink_is_rejected(self, tmp_path):
        info = tarfile.TarInfo(name="hard")
        info.type = tarfile.LNKTYPE
        info.linkname = "SKILL.md"
        path = _write_package(tmp_path, [(info, b"")])
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.details["kind"] == "hardlink"

    def test_duplicate_member_is_rejected(self, tmp_path):
        path = _write_package(tmp_path, [
            (_file_info("SKILL.md", b"first"), b"first"),
            (_file_info("SKILL.md", b"second"), b"second"),
        ])
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.code == "duplicate_member"


class TestSizeLimits:
    def test_per_file_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(verify, "MAX_FILE_BYTES", 4)
        path = _write_package(tmp_path, [(_file_info("big.bin", b"12345"), b"12345")])
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.code == "file_too_large"

    def test_unpacked_volume_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(verify, "MAX_UNPACKED_RATIO", 0)
        path = _write_package(tmp_path, [
            (_file_info("a.bin", b"12345"), b"12345"),
            (_file_info("b.bin", b"12345"), b"12345"),
        ])
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.code == "archive_too_large"

    def test_file_count_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(verify, "MAX_FILES", 2)
        members = [
            (_file_info(f"file_{index}.txt", b"x"), b"x") for index in range(3)
        ]
        path = _write_package(tmp_path, members)
        with pytest.raises(verify.VerifyError) as excinfo:
            verify.validate_archive(str(path))
        assert excinfo.value.code == "too_many_files"


class TestExtractionFilter:
    def test_skip_keeps_code_out(self, tmp_path):
        path = _write_package(tmp_path, [
            (_file_info("SKILL.md", b"body"), b"body"),
            (_file_info("scripts/run.py", b"print(1)"), b"print(1)"),
        ])
        members = verify.validate_archive(str(path))
        destination = tmp_path / "staging"
        written = verify.extract_archive(
            str(path), str(destination), members, skip=["scripts/run.py"]
        )
        assert written == ["SKILL.md"]
        assert not (destination / "scripts" / "run.py").exists()


class TestFileVerification:
    def test_matching_files_pass(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("body", encoding="utf-8")
        files = {"SKILL.md": sha256_hex(b"body")}
        assert verify.verify_files(str(tmp_path), files) == []

    def test_changed_file_is_reported(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("edited", encoding="utf-8")
        files = {"SKILL.md": sha256_hex(b"body")}
        assert verify.verify_files(str(tmp_path), files) == ["SKILL.md"]

    def test_missing_file_is_reported(self, tmp_path):
        files = {"gone.md": sha256_hex(b"body")}
        assert verify.verify_files(str(tmp_path), files) == ["gone.md"]


class TestCodePathDetection:
    @pytest.mark.parametrize("rel_path,expected", [
        ("SKILL.md", False),
        ("references/notes.md", False),
        ("scripts/run.py", True),
        ("scripts/nested/helper.py", True),
        ("./scripts/run.py", True),
        ("functions/tool.py", True),
    ])
    def test_recognises_executable_paths(self, rel_path, expected):
        assert verify.is_code_path(rel_path) is expected


class TestSignatureCheck:
    def _signed(self, tmp_path, monkeypatch, payload: bytes, key_name="k"):
        """Sign *payload* with a fresh key and install it as the keyring."""
        from core import signing
        from core.marketplace import config as marketplace_config

        keys = tmp_path / "keys"
        keys.mkdir(parents=True, exist_ok=True)
        private = tmp_path / f"{key_name}.pem"
        public = keys / f"{key_name}.pub"
        signing.generate_keypair(str(private), str(public))
        record = signing.sign_bytes(payload, signing.load_private_key(str(private)))
        settings = {"enabled": True, "base_url": "", "allow_code": True,
                    "require_signature": False, "channel": "main",
                    "signature_keyring": str(keys)}
        monkeypatch.setattr(
            marketplace_config, "marketplace_settings", lambda: dict(settings)
        )
        return record, settings, keys

    def test_unsigned_is_not_verified_but_tolerated_on_a_warning_channel(
        self, tmp_path, monkeypatch
    ):
        _, _, _ = self._signed(tmp_path, monkeypatch, b'{"id": "demo"}')

        result = verify.verify_signature(
            {"id": "demo"}, raw_entity=b'{"id": "demo"}', signature=None
        )

        assert result["verified"] is False
        assert result["enforced"] is False
        assert result["reason"] == "signature_missing"
        assert result["policy"] == "warn"

    def test_a_signed_manifest_verifies(self, tmp_path, monkeypatch):
        payload = b'{"id": "demo", "version": "1.0.0"}'
        record, _, keys = self._signed(tmp_path, monkeypatch, payload)

        result = verify.verify_signature(
            {"id": "demo"}, raw_entity=payload, signature=record, key_dir=str(keys)
        )

        assert result["verified"] is True
        assert result["key_id"] == record["key_id"]

    def test_an_edited_manifest_is_refused(self, tmp_path, monkeypatch):
        payload = b'{"id": "demo", "version": "1.0.0"}'
        record, _, keys = self._signed(tmp_path, monkeypatch, payload)

        result = verify.verify_signature(
            {"id": "demo"},
            raw_entity=b'{"id": "demo", "version": "9.9.9"}',
            signature=record,
            key_dir=str(keys),
        )

        assert result["verified"] is False
        assert result["reason"] == "signature_invalid"

    def test_requiring_signatures_turns_a_failure_into_an_error(
        self, tmp_path, monkeypatch
    ):
        record, settings, keys = self._signed(tmp_path, monkeypatch, b'{"id": "demo"}')
        settings["require_signature"] = True

        with pytest.raises(verify.VerifyError) as excinfo:
            verify.verify_signature(
                {"id": "demo"}, raw_entity=b'{"id": "other"}',
                signature=record, key_dir=str(keys),
            )

        assert excinfo.value.code == "signature_required"

    def test_an_untrusted_key_is_refused(self, tmp_path, monkeypatch):
        payload = b'{"id": "demo"}'
        record, _, _ = self._signed(tmp_path, monkeypatch, payload)
        other = tmp_path / "other"
        other.mkdir()
        from core import signing

        signing.generate_keypair(str(tmp_path / "x.pem"), str(other / "x.pub"))

        result = verify.verify_signature(
            {"id": "demo"}, raw_entity=payload, signature=record, key_dir=str(other)
        )

        assert result["verified"] is False
        assert result["reason"] == "unknown_key_id"

    def test_the_keyring_defaults_to_the_installation_keys(self, tmp_path, monkeypatch):
        from core.marketplace import config as marketplace_config

        monkeypatch.setattr(
            marketplace_config, "marketplace_settings",
            lambda: {"signature_keyring": ""},
        )

        path = verify.signature_keyring()

        assert path.endswith(os.path.join("sagaai_github_clean_copy", "keys")) or os.path.basename(path) == "keys"

    def test_a_configured_keyring_wins(self, tmp_path, monkeypatch):
        from core.marketplace import config as marketplace_config

        monkeypatch.setattr(
            marketplace_config, "marketplace_settings",
            lambda: {"signature_keyring": str(tmp_path / "trusted")},
        )

        assert verify.signature_keyring() == str(tmp_path / "trusted")

    def test_the_refusal_belongs_to_the_calling_module(
        self, tmp_path, monkeypatch
    ):
        """A refusal raised through ``verify`` is a ``verify.VerifyError``.

        The policy layer imports the exception class by name when it raises,
        so a reloaded package would otherwise hand the caller an object from
        another copy of this module and ``except VerifyError`` would miss it.
        """
        record, settings, keys = self._signed(tmp_path, monkeypatch, b'{"id": "demo"}')
        settings["require_signature"] = True

        with pytest.raises(verify.VerifyError) as excinfo:
            verify.verify_signature(
                {"id": "demo"}, raw_entity=b'{"id": "other"}',
                signature=record, key_dir=str(keys),
            )

        assert excinfo.value.code == "signature_required"
        assert excinfo.value.details.get("reason") == "signature_invalid"


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
