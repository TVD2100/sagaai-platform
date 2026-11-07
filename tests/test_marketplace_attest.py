"""
test_marketplace_attest.py - signature policy of the marketplace.

The catalog is untrusted input, so these tests pin the policy layer: the
trust anchors are local files, a manifest is verified against the bytes it
arrived as, the transport refuses an unsigned index on an enforcing channel
before anything parses it, and an installer refuses an unsigned entity
before it downloads a package.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import signing  # noqa: E402
from core.marketplace import attest, verify  # noqa: E402

# ─── helpers ───────────────────────────────────────────────────────────────

def _keyring(tmp_path):
    """Create a keyring with one key and return (folder, private, key_id)."""
    folder = tmp_path / "keys"
    folder.mkdir(parents=True, exist_ok=True)
    private = tmp_path / "release.pem"
    public = folder / "release.pem"
    report = signing.generate_keypair(str(private), str(public))
    return folder, private, report["key_id"]


def _record(private, payload: bytes) -> dict:
    """Sign *payload* with the private half of a test pair."""
    return signing.sign_bytes(payload, signing.load_private_key(str(private)))


def _fetching(files: dict):
    """Return a fetch_bytes callable serving *files* and failing elsewhere."""

    def fetch(relative: str) -> bytes:
        try:
            return files[relative]
        except KeyError:
            raise verify.VerifyError("missing", "no such file", {"path": relative})

    return fetch


# ─── trust anchors ─────────────────────────────────────────────────────────

class TestKeyringLocation:
    def test_the_installation_folder_is_the_default(self, monkeypatch):
        monkeypatch.setattr(attest, "_settings", lambda: {})

        path = attest.signature_keyring()

        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(attest.__file__))))
        assert path == os.path.join(root, "keys")

    def test_the_configured_folder_wins(self, monkeypatch):
        monkeypatch.setattr(
            attest, "_settings", lambda: {"signature_keyring": "/srv/mirror/keys"}
        )

        assert attest.signature_keyring() == "/srv/mirror/keys"

    def test_an_explicit_argument_wins_over_the_setting(self, monkeypatch):
        monkeypatch.setattr(
            attest, "_settings", lambda: {"signature_keyring": "/srv/mirror/keys"}
        )

        assert attest.signature_keyring("/tmp/keys") == "/tmp/keys"


# ─── manifest verification ─────────────────────────────────────────────────

class TestVerifySignature:
    def test_a_signed_manifest_verifies(self, tmp_path, monkeypatch):
        folder, private, key_id = _keyring(tmp_path)
        payload = b'{"id": "demo", "version": "1.0.0"}'
        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "main"})

        report = attest.verify_signature(
            {"id": "demo"},
            signature=_record(private, payload),
            raw_entity=payload,
            key_dir=str(folder),
        )

        assert report["verified"] is True
        assert report["key_id"] == key_id
        assert report["enforced"] is False

    def test_an_edited_manifest_is_refused(self, tmp_path, monkeypatch):
        folder, private, _ = _keyring(tmp_path)
        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "main"})

        report = attest.verify_signature(
            {"id": "demo"},
            signature=_record(private, b'{"id": "demo"}'),
            raw_entity=b'{"id": "demo", "extra": true}',
            key_dir=str(folder),
        )

        assert report["verified"] is False
        assert report["reason"] == "signature_invalid"

    def test_a_missing_signature_is_never_verified(self, tmp_path, monkeypatch):
        folder, _private, _ = _keyring(tmp_path)
        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "main"})

        report = attest.verify_signature(
            {"id": "demo"}, signature=None, raw_entity=b"{}", key_dir=str(folder)
        )

        assert report["verified"] is False
        assert report["reason"] == "signature_missing"

    def test_the_stable_channel_turns_a_failure_into_an_error(self, tmp_path):
        folder, private, _ = _keyring(tmp_path)

        with pytest.raises(verify.VerifyError) as excinfo:
            attest.verify_signature(
                {"id": "demo"},
                signature=_record(private, b"{}"),
                raw_entity=b'{"tampered": true}',
                key_dir=str(folder),
                channel="stable",
            )

        assert excinfo.value.code == "signature_required"

    def test_require_signature_wins_over_the_channel(self, tmp_path, monkeypatch):
        folder, private, _ = _keyring(tmp_path)
        monkeypatch.setattr(
            attest, "_settings",
            lambda: {"channel": "main", "require_signature": True},
        )

        with pytest.raises(verify.VerifyError):
            attest.verify_signature(
                {"id": "demo"},
                signature=_record(private, b"{}"),
                raw_entity=b'{"other": 1}',
                key_dir=str(folder),
            )

    def test_a_payload_is_required(self, tmp_path, monkeypatch):
        folder, _private, _ = _keyring(tmp_path)
        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "main"})

        report = attest.verify_signature({"id": "demo"}, key_dir=str(folder))

        assert report["verified"] is False
        assert report["reason"] == "payload_missing"


# ─── transport side ────────────────────────────────────────────────────────

class TestCheckManifest:
    def test_a_signed_manifest_passes(self, tmp_path, monkeypatch):
        folder, private, key_id = _keyring(tmp_path)
        monkeypatch.setattr(
            attest, "_settings", lambda: {"channel": "main", "signature_keyring": str(folder)}
        )
        payload = b'{"units": {}, "selectable": {}}'
        files = {"index.json": payload, "index.json.sig": json.dumps(_record(private, payload)).encode()}

        report = attest.check_manifest(_fetching(files), "index.json", payload, {}, "main")

        assert report["verified"] is True
        assert report["key_id"] == key_id

    def test_a_missing_sidecar_is_tolerated_on_a_warning_channel(self, tmp_path):
        _keyring(tmp_path)
        payload = b'{"units": {}}'
        files = {"index.json": payload}

        report = attest.check_manifest(_fetching(files), "index.json", payload, {}, "main")

        assert report["verified"] is False
        assert report["reason"] == "signature_missing"

    def test_the_stable_channel_refuses_before_parsing(self, tmp_path):
        _keyring(tmp_path)
        payload = b'{"units": {}}'
        files = {"index.json": payload}

        from core.marketplace.source import SourceError

        with pytest.raises(SourceError) as excinfo:
            attest.check_manifest(_fetching(files), "index.json", payload, {}, "stable")

        assert excinfo.value.code == "signature_refused"

    def test_a_malformed_sidecar_is_not_silently_dropped(self, tmp_path, monkeypatch):
        folder, _private, _ = _keyring(tmp_path)
        files = {"index.json.sig": b"not json"}
        monkeypatch.setattr(attest, "signature_keyring", lambda key_dir=None: str(folder))

        record = attest.fetch_signature(_fetching(files), "index.json")

        assert record == {"malformed": True}


# ─── installer side ────────────────────────────────────────────────────────

class _FakeSource:
    """Minimal transport stand-in carrying verification reports."""

    def __init__(self, signatures=None):
        self.signatures = dict(signatures or {})
        self.package_requests = 0


class TestInstallGate:
    def _entity(self):
        return {"id": "demo", "type": "skill", "version": "1.0.0"}

    def test_a_verified_entity_passes(self, monkeypatch):
        source = _FakeSource({"skills/demo/1.0.0/entity.json": {"verified": True, "key_id": "k"}})
        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "main"})

        outcome = attest.install_gate(self._entity(), source=source, channel="main")

        assert outcome["verified"] is True
        assert outcome["key_id"] == "k"

    def test_an_enforcing_channel_refuses_an_unsigned_entity(self, monkeypatch):
        source = _FakeSource()
        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "stable"})

        with pytest.raises(verify.VerifyError) as excinfo:
            attest.install_gate(self._entity(), source=source, channel="stable")

        assert excinfo.value.code == "signature_required"
        assert excinfo.value.details["reason"] == "signature_missing"

    def test_a_warning_channel_reports_and_continues(self, monkeypatch):
        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "main"})

        outcome = attest.install_gate(self._entity(), source=_FakeSource(), channel="main")

        assert outcome["verified"] is False
        assert outcome["enforced"] is False
        assert outcome["policy"] == "warn"

    def test_a_missing_transport_means_no_signature(self, monkeypatch):
        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "main"})

        outcome = attest.install_gate(self._entity(), source=None, channel="main")

        assert outcome["verified"] is False


class TestInstallerIntegration:
    def test_the_gate_runs_before_the_package_is_downloaded(self, tmp_path, monkeypatch):
        from core.marketplace import installer

        monkeypatch.setattr(attest, "_settings", lambda: {"channel": "stable"})
        source = _FakeSource()

        with pytest.raises(installer.InstallError) as excinfo:
            installer.install_entity(
                {"id": "demo", "type": "skill", "version": "1.0.0"},
                channel="stable",
                source=source,
            )

        assert excinfo.value.code == "signature_required"
        assert source.package_requests == 0
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
