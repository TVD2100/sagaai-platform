"""
scripts/marketplace_publish.py - publish one entity version into a catalog.

The catalog repository is built from this command; nothing in it is edited
by hand. Typical use, from a checkout of ``sagaai-marketplace``::

    python scripts/marketplace_publish.py \\
        --type agent --id ya_agent --version 2.11.0 \\
        --path ./build/ya_agent --manifest ./build/ya_agent/entity.json \\
        --root . --sign-key ~/.sagaai/marketplace.pem

What it does, in order: read the descriptive manifest, collect the entity
folder, build the deterministic package, write the immutable version folder,
refresh ``latest/``, rebuild ``index.json`` and sign it. A refused publish
leaves the catalog untouched.

``--init-key`` creates the signing pair instead of publishing: the private
half stays outside the repository, the public half is written to
``keys/<key_id>.pub``.

``--export-standalone PATH`` writes a copy of this publisher that does not
import the platform, for committing into the catalog repository as
``scripts/publish.py``. The catalog needs a publisher that runs in a fresh
clone; keeping the logic here and exporting a copy means the two can never
drift - the export is generated from this very file.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.marketplace import publish as publish_mod  # noqa: E402

#: The standalone copy keeps the library of ``core.marketplace.publish`` but
#: must not import it, so its docstring points at the catalog it builds.
STANDALONE_DOCSTRING = '''"""
scripts/publish.py - build the marketplace catalog repository.

Self-contained on purpose: Python 3.9+, the standard library and
``cryptography`` for signing, nothing from the platform. The catalog can be
rebuilt from a fresh clone without SagaAI next to it.

One entity version at a time::

    python scripts/publish.py --type skill --id my_skill --version 1.0.0 \\
        --path ./build/my_skill --manifest ./build/my_skill/entity.json \\
        --root . --sign-key ~/.sagaai/marketplace.pem

Check a published catalog without changing it::

    python scripts/publish.py --root . --verify-only

``--init-key`` creates the signing pair instead of publishing: the private
half stays outside the repository, the public half goes to
``keys/<key_id>.pub``.
"""
'''


def _read_manifest(path: str, type_: str, entity_id: str, version: str) -> dict:
    """Return the descriptive manifest with id/type/version filled in.

    A manifest file is optional: an entity whose package is self-describing
    can be published from the command line alone, and the fields that decide
    where the version lands are always taken from the arguments so a stale
    file cannot publish into the wrong folder.
    """
    manifest = {}
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cannot read manifest {path}: {exc}")
        if not isinstance(loaded, dict):
            raise SystemExit(f"manifest {path} must be a JSON object")
        manifest = loaded
    manifest.update({"type": type_, "id": entity_id, "version": version})
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the publisher CLI."""
    parser = argparse.ArgumentParser(
        description="Publish one entity version into a SagaAI marketplace catalog"
    )
    parser.add_argument("--root", default=".", help="catalog repository root")
    parser.add_argument("--type", dest="type_", help="agent | skill | rag | mcp")
    parser.add_argument("--id", dest="entity_id", help="catalog id of the entity")
    parser.add_argument("--version", help="semver of the version being published")
    parser.add_argument("--path", help="folder that holds the entity files")
    parser.add_argument("--manifest", default="", help="descriptive entity.json")
    parser.add_argument("--key-id", default="sagaai-marketplace-2026")
    parser.add_argument("--sign-key", default="", help="Ed25519 private key (PEM)")
    parser.add_argument("--channel", default="main", help="channel recorded in the index")
    parser.add_argument("--init-key", action="store_true",
                        help="create the signing pair instead of publishing")
    parser.add_argument("--private-key", default="",
                        help="where --init-key writes the private half")
    parser.add_argument("--verify-only", action="store_true",
                        help="check index.json.sig and exit without changing anything")
    parser.add_argument("--export-standalone", default="",
                        help="write a platform-independent copy of this publisher")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def _report_text(report: dict) -> str:
    """Return the human-readable summary of a publish report."""
    state = "written" if report.get("changed") else "already published"
    lines = [
        f"{report['type']}/{report['id']} {report['version']}: {state}",
        f"  package sha256: {report['package_sha256']}",
        f"  files: {report['file_count']}, index entities: {report['index_entities']}",
    ]
    if report.get("signed"):
        verified = "yes" if report.get("signature_verified") else "no"
        lines.append(f"  signed: yes (verified: {verified})")
    else:
        lines.append("  signed: no")
    return "\n".join(lines)


def export_standalone(destination: str) -> dict:
    """Write a copy of this publisher that does not import the platform.

    The library half comes from ``core.marketplace.publish`` and the CLI half
    from the functions of this file, both read through ``inspect`` rather than
    sliced by markers: a function may contain any text, and only its real
    source is what the catalog needs. Returns ``{"path", "bytes", "sha256"}``.
    """
    core_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "core", "marketplace", "publish.py",
    )
    with open(core_path, "r", encoding="utf-8") as handle:
        library = handle.read()
    # Drop the module docstring and the licence trailer of the library; the
    # standalone copy carries its own header and footer.
    library = library.split("# SPDX-FileCopyrightText")[0].rstrip()
    first = library.index('"""')
    second = library.index('"""', first + 3)
    body = library[second + 3:].lstrip("\n")
    body = body.replace("from __future__ import annotations\n", "", 1).lstrip("\n")

    pieces = [inspect.getsource(func).rstrip()
              for func in (_read_manifest, _build_parser, _report_text, _run, main)]
    cli_source = "\n\n\n".join(pieces)
    # The exported copy cannot read the platform library, so it has none of the
    # options that only make sense next to it.
    cli_source = cli_source.replace(
        '    parser.add_argument("--export-standalone", default="",\n'
        '                        help="write a platform-independent copy of this publisher")\n',
        "",
    )
    cli_source = cli_source.replace(
        "    if args.export_standalone:\n"
        "        print(json.dumps(export_standalone(args.export_standalone), indent=2))\n"
        "        return 0\n\n",
        "",
    )
    cli_source = cli_source.replace("publish_mod.", "")

    standalone = "\n".join([
        STANDALONE_DOCSTRING.rstrip(),
        "from __future__ import annotations",
        "",
        "import argparse",
        "import sys",
        "",
        body,
        "",
        cli_source,
        "",
        'if __name__ == "__main__":',
        "    raise SystemExit(main())",
        "",
        "# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.",
        "# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0",
        "",
    ])
    target = os.path.abspath(destination)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(standalone)
    return {
        "path": target,
        "bytes": len(standalone.encode("utf-8")),
        "sha256": publish_mod.sha256_bytes(standalone.encode("utf-8")),
    }


def _run(args) -> int:
    """Execute the action selected by the parsed arguments."""
    root = os.path.abspath(args.root)

    if args.export_standalone:
        print(json.dumps(export_standalone(args.export_standalone), indent=2))
        return 0

    if args.verify_only:
        public_path = os.path.join(root, "keys", f"{args.key_id}.pub")
        check = publish_mod.verify_index_signature(root, public_path)
        print(json.dumps(check, ensure_ascii=False, indent=2))
        return 0 if check.get("ok") else 1

    if args.init_key:
        private_path = args.private_key or os.path.expanduser("~/.sagaai/marketplace.pem")
        public_path = os.path.join(root, "keys", f"{args.key_id}.pub")
        report = publish_mod.generate_keypair(private_path, public_path, args.key_id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("Keep the private key out of the repository.", file=sys.stderr)
        return 0

    missing = [
        name for name, value in (
            ("--type", args.type_), ("--id", args.entity_id),
            ("--version", args.version),
        ) if not value
    ]
    # An MCP entity is a preset with no files: it publishes from the manifest
    # alone. Every other type ships a package and needs its folder.
    needs_folder = str(args.type_ or "").strip() != "mcp"
    if needs_folder and not args.path:
        missing.append("--path")
    if missing:
        _build_parser().error("missing required arguments: " + ", ".join(missing))

    manifest = _read_manifest(args.manifest, args.type_, args.entity_id, args.version)
    try:
        report = publish_mod.publish(
            root,
            os.path.abspath(args.path) if args.path else None,
            manifest,
            private_key_path=args.sign_key or None,
            key_id=args.key_id,
            channel=args.channel,
        )
    except publish_mod.PublishError as exc:
        print(f"publish refused ({exc.code}): {exc.message}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else _report_text(report))
    return 0


def main(argv=None) -> int:
    """Run the publisher CLI and return its exit status."""
    return _run(_build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
