#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sign_release.py - sign and verify release artifacts with an Ed25519 key.

The update pipeline trusts a file only when a detached signature next to it
verifies against a key shipped inside the installation. This tool is the
publisher side of that pair: it writes ``<file>.sig`` for a manifest and
checks an existing signature before a release goes out.

Modes::

    python scripts/sign_release.py sign file_versions.json
    python scripts/sign_release.py verify file_versions.json
    python scripts/sign_release.py keygen --public keys/ed25519-public.pem
    python scripts/sign_release.py keyring keys/

The private key never enters the repository: pass it with ``--key``, or set
``SAGAII_SIGNING_KEY`` (PEM text or a path). ``verify`` and ``keyring`` work
offline and need only public keys.

Exit codes: 0 - success; 1 - verification failed; 2 - usage or key error.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.signing import (  # noqa: E402
    SIGNING_KEY_ENV,
    SigningError,
    generate_keypair,
    key_id_of_private,
    keyring_report,
    load_private_key,
    signature_path,
    verify_file,
    write_signature,
)

#: Default keyring folder inside the repository: public halves only.
DEFAULT_KEYRING = os.path.join("keys")


def _load_key(path):
    """Return the private key from *path* or from the environment."""
    return load_private_key(path or None)


def _cmd_sign(args):
    """Sign every file named on the command line; return an exit status."""
    # Load once up front: a missing or unreadable key must fail before any
    # signature file is written, not halfway through the list.
    _load_key(args.key)
    key_id = key_id_of_private(args.key) if args.key else None
    report = []
    for target in args.files:
        if not os.path.isfile(target):
            print(f"ERROR: {target} does not exist", file=sys.stderr)
            return 2
        record = write_signature(target, args.key, key_id=key_id or None)
        report.append({
            "file": target,
            "signature": signature_path(target),
            "key_id": record.get("key_id"),
            "sha256": record.get("signed_sha256"),
        })
        print(f"signed: {target} -> {signature_path(target)}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"key id: {report[0]['key_id'] if report else '-'} (private half untouched)")
    return 0


def _cmd_verify(args):
    """Verify the signature of every file named; return an exit status."""
    keyring = args.keyring or DEFAULT_KEYRING
    status = 0
    report = []
    for target in args.files:
        check = verify_file(target, key_dir=keyring)
        report.append({"file": target, "keyring": keyring, **check})
        if check.get("ok"):
            print(f"verified: {target} (key {check.get('key_id')})")
        else:
            print(f"REFUSED: {target}: {check.get('reason')}", file=sys.stderr)
            status = 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return status


def _cmd_keygen(args):
    """Create a signing pair and print its identifiers."""
    private = args.private or os.path.expanduser("~/.sagaai/ed25519.pem")
    public = args.public or os.path.join(DEFAULT_KEYRING, "pending.pub")
    os.makedirs(os.path.dirname(os.path.abspath(public)), exist_ok=True)
    report = generate_keypair(private, public)
    if args.public:
        final = public
    else:
        final = os.path.join(DEFAULT_KEYRING, f"{report['key_id']}.pub")
        os.replace(public, final)
    report["private_path"] = private
    report["public_path"] = final
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Keep the private key out of the repository.", file=sys.stderr)
    return 0


def _cmd_keyring(args):
    """List the keys a verifier would trust."""
    folder = args.keyring or args.folder or DEFAULT_KEYRING
    report = keyring_report(folder)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


def _build_parser():
    """Return the argument parser of the release signing tool."""
    parser = argparse.ArgumentParser(
        description="Sign and verify SagaAI release artifacts"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sign = sub.add_parser("sign", help="write <file>.sig for every file")
    sign.add_argument("files", nargs="+")
    sign.add_argument("--key", default="",
                      help=f"Ed25519 private key (PEM); default: {SIGNING_KEY_ENV}")
    sign.add_argument("--json", action="store_true")
    sign.set_defaults(func=_cmd_sign)

    verify = sub.add_parser("verify", help="check existing signatures")
    verify.add_argument("files", nargs="+")
    verify.add_argument("--keyring", default="",
                        help="folder of trusted public keys (default: keys/)")
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=_cmd_verify)

    keygen = sub.add_parser("keygen", help="create a signing pair")
    keygen.add_argument("--private", default="", help="where to write the private half")
    keygen.add_argument("--public", default="", help="explicit public key path")
    keygen.set_defaults(func=_cmd_keygen)

    keyring = sub.add_parser("keyring", help="list trusted public keys")
    keyring.add_argument("folder", nargs="?", default="",
                         help="folder of public keys (default: keys/)")
    keyring.add_argument("--keyring", default="")
    keyring.set_defaults(func=_cmd_keyring)
    return parser


def main(argv=None):
    """Run the tool and return its exit status."""
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SigningError as exc:
        print(f"ERROR ({exc.code}): {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())