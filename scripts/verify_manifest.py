#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_manifest.py - validate, bootstrap and maintain file_versions.json.

The manifest file_versions.json is the single source of truth for the SagaAI
update pipeline. It lists every shipped file with its own version and sha256,
grouped into atomic units (installed only as a whole) and selectable files
(installed individually).

Modes:
  python scripts/verify_manifest.py              verify only (exit 0 = ok)
  python scripts/verify_manifest.py --strict     also fail on uncovered local files
  python scripts/verify_manifest.py --fix-hashes recompute stale sha256 values
  python scripts/verify_manifest.py --init       bootstrap the initial manifest

Exit codes: 0 - all checks passed; 1 - violations found; 2 - fatal error.

Checks performed in verify mode:
  * schema: top-level keys, sections, version format (semver-like "X.Y[.Z][-pre]"),
    ru/en note dictionaries, 64-hex sha256 values;
  * duplicates: a path may appear in exactly one section;
  * presence: every listed file exists on disk;
  * hashes: sha256 of every listed file matches the manifest;
  * coverage (git available): every git-tracked file (except .gitignore and
    the manifest itself) is listed; manifest entries not yet tracked are warnings;
  * consistency: app_version equals core/version.py __version__.

This tool is developer-side: user installs use core/updater.py instead.
"""

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


MANIFEST_NAME = "file_versions.json"
COVERAGE_SKIP = {".gitignore", MANIFEST_NAME}
CHANNEL = "https://raw.githubusercontent.com/TVD2100/sagaai-platform/main"
UNIT_NAME = "core"

CORE_ROOT_FILES = {"__init__.py", "app.py", "pytest.ini", "requirements.txt"}
CORE_DIRS = ("core/", "storage/", "ui/", "assets/", "certs/", "tests/", "scripts/")

# Directories whose runtime contents are not part of the update manifest.
WALK_SKIP_DIRS = {
    ".git", ".dev_agent", "__pycache__", ".pytest_cache", ".venv", "venv",
    "env", "build", "dist", "apps", "connectors", "assistants", "history",
    "orchestrators", "rag_bases", "skills", "system_prompts", "threads",
    "data", "deliverables", "personal_assistant_data",
}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}

VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?(-[0-9A-Za-z][0-9A-Za-z.\-]*)?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Files whose version is taken from the header line of the file itself.
PROMPT_VERSION_SOURCES = {
    "dev_agent/system_prompt.md": r"^#\s+.*\(v([^)]+)\)",
    "defaults/orchestrators/ya_agent/system_prompt.md": r"^#\s+.*\(v([^)]+)\)",
}

RELEASE_NOTE_INIT = {
    "ru": (
        "Первая публикация конвейера обновлений: манифест версий файлов "
        "(file_versions.json) и встроенный апдейтер."
    ),
    "en": (
        "First release of the update pipeline: file version manifest "
        "(file_versions.json) and the built-in updater."
    ),
}
UNIT_CORE_NOTE = {
    "ru": "Исполняемый код платформы (core, UI, хранилище, тесты). Обновляется только целиком, единым пакетом.",
    "en": "Platform executable code (core, UI, storage, tests). Updates only as a single atomic unit.",
}
DEFAULT_SELECTABLE_NOTE = {
    "ru": "Дополнительный файл (содержимое, не исполняемый код). Можно обновлять выборочно.",
    "en": "Optional content file (non-code). Can be updated individually.",
}


def log(level, msg):
    stream = sys.stderr if level == "ERROR" else sys.stdout
    print("%s: %s" % (level, msg), file=stream)


def find_root():
    """Return the repository root: nearest ancestor of CWD containing .git."""
    d = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.getcwd()
        d = parent


def git_ls_files(root):
    """Return (ok, [relative paths]) for git-tracked files; ok=False without git."""
    if not os.path.isdir(os.path.join(root, ".git")):
        return False, []
    try:
        p = subprocess.run(
            ["git", "-C", root, "ls-files"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False, []
    if p.returncode != 0:
        return False, []
    return True, [line.strip() for line in p.stdout.splitlines() if line.strip()]


def sha256_file(root, rel):
    h = hashlib.sha256()
    with open(os.path.join(root, rel), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_app_version(root):
    path = os.path.join(root, "core", "version.py")
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
    except OSError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__version__"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    return node.value.value
    return None


def header_version(root, rel, pattern):
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            first = f.readline().rstrip("\n")
    except OSError:
        return None
    m = re.match(pattern, first)
    return m.group(1) if m else None


def classify(path):
    if path in CORE_ROOT_FILES:
        return UNIT_NAME
    if path.startswith(CORE_DIRS):
        return UNIT_NAME
    if path.startswith("dev_agent/") and path.endswith(".py"):
        return UNIT_NAME
    return "selectable"


def load_manifest(root):
    path = os.path.join(root, MANIFEST_NAME)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_manifest(root, manifest):
    path = os.path.join(root, MANIFEST_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(root, tracked):
    """Bootstrap the initial manifest from the repository file set."""
    files = sorted(set(tracked) | {"scripts/verify_manifest.py"})
    unit_files = {}
    selectable = {}
    for rel in files:
        if rel in COVERAGE_SKIP:
            continue
        if not os.path.isfile(os.path.join(root, rel)):
            continue
        version = "1.0.0"
        pattern = PROMPT_VERSION_SOURCES.get(rel)
        if pattern:
            hv = header_version(root, rel, pattern)
            if hv:
                version = hv
        entry = {"version": version, "sha256": sha256_file(root, rel)}
        if classify(rel) == UNIT_NAME:
            unit_files[rel] = entry
        else:
            entry["note"] = dict(DEFAULT_SELECTABLE_NOTE)
            selectable[rel] = entry
    app_version = read_app_version(root)
    if not app_version:
        log("ERROR", "core/version.py has no __version__ string constant")
        sys.exit(2)
    manifest = {
        "schema": 1,
        "app_version": app_version,
        "channel": CHANNEL,
        "release_note": dict(RELEASE_NOTE_INIT),
        "updated": now_iso(),
        "units": {
            UNIT_NAME: {
                "version": "1.0.0",
                "note": dict(UNIT_CORE_NOTE),
                "files": unit_files,
            }
        },
        "selectable": {},
    }
    # Keep selectable sorted for stable diffs.
    for rel in sorted(selectable):
        manifest["selectable"][rel] = selectable[rel]
    return manifest


def add_file(root, manifest, rel):
    """Register a new file in the manifest and persist the result."""
    if rel in COVERAGE_SKIP:
        log("ERROR", "%s is managed separately; nothing to add" % rel)
        return 2
    if not os.path.isfile(os.path.join(root, rel)):
        log("ERROR", "no such file: %s" % rel)
        return 2
    covered = set()
    for _, unit in manifest.get("units", {}).items():
        if isinstance(unit, dict) and isinstance(unit.get("files"), dict):
            covered.update(unit["files"])
    covered.update(manifest.get("selectable", {}))
    if rel in covered:
        log("ERROR", "already in manifest: %s" % rel)
        return 2

    version = "1.0.0"
    pattern = PROMPT_VERSION_SOURCES.get(rel)
    if pattern:
        hv = header_version(root, rel, pattern)
        if hv:
            version = hv
    entry = {"version": version, "sha256": sha256_file(root, rel)}

    if classify(rel) == UNIT_NAME:
        units = manifest.get("units", {})
        unit = units.get(UNIT_NAME)
        if not isinstance(unit, dict):
            unit = {"version": "1.0.0", "note": dict(UNIT_CORE_NOTE), "files": {}}
            units[UNIT_NAME] = unit
            manifest["units"] = units
        unit.setdefault("files", {})[rel] = entry
    else:
        entry["note"] = dict(DEFAULT_SELECTABLE_NOTE)
        manifest.setdefault("selectable", {})[rel] = entry

    save_manifest(root, manifest)
    log("INFO", "registered: %s (%s)" % (rel, entry["version"]))
    return 0


def check_note_dict(value, errors, where):
    ok = (
        isinstance(value, dict)
        and isinstance(value.get("ru"), str)
        and value.get("ru").strip()
        and isinstance(value.get("en"), str)
        and value.get("en").strip()
    )
    if not ok:
        errors.append("%s: note must be {ru: str, en: str}" % where)


def check_unit(unit_name, unit, errors):
    if not isinstance(unit, dict):
        errors.append("unit '%s': must be a dict" % unit_name)
        return
    ver = unit.get("version")
    if not (isinstance(ver, str) and VERSION_RE.match(ver)):
        errors.append("unit '%s': invalid version %r" % (unit_name, ver))
    check_note_dict(unit.get("note"), errors, "unit '%s'" % unit_name)
    files = unit.get("files")
    if not (isinstance(files, dict) and files):
        errors.append("unit '%s': files must be a non-empty dict" % unit_name)
        return
    for rel, entry in files.items():
        check_file_entry(rel, entry, errors, require_note=False)


def check_file_entry(rel, entry, errors, require_note):
    where = "file entry %r" % rel
    if not isinstance(rel, str) or not rel:
        errors.append("%s: invalid path" % where)
        return
    if rel.startswith("/") or rel.startswith("./") or "\\" in rel:
        errors.append("%s: path must be a clean POSIX relative path" % where)
        return
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        errors.append("%s: path must be a clean POSIX relative path" % where)
        return
    if not isinstance(entry, dict):
        errors.append("%s: entry must be a dict" % where)
        return
    ver = entry.get("version")
    if not (isinstance(ver, str) and VERSION_RE.match(ver)):
        errors.append("%s: invalid version %r" % (where, ver))
    sha = entry.get("sha256")
    if not (isinstance(sha, str) and SHA_RE.match(sha)):
        errors.append("%s: sha256 must be a 64-char hex string" % where)
    if require_note:
        check_note_dict(entry.get("note"), errors, where)


def verify(
    root,
    manifest,
    strict=False,
    fix_hashes=False,
    json_out=False,
):
    """Validate the manifest; return exit code (0 ok, 1 violations, 2 fatal)."""
    errors = []
    warnings = []
    fixed = 0

    # --- schema ---
    if not isinstance(manifest, dict):
        errors.append("manifest must be a JSON object")
        return 2, errors, warnings, fixed
    if manifest.get("schema") != 1:
        errors.append("schema: expected 1, got %r" % manifest.get("schema"))
    app_ver = manifest.get("app_version")
    if not (isinstance(app_ver, str) and VERSION_RE.match(app_ver)):
        errors.append("app_version: invalid or missing %r" % app_ver)
    channel = manifest.get("channel")
    if not (isinstance(channel, str) and channel.startswith("https://")):
        errors.append("channel: must be an https URL")
    check_note_dict(manifest.get("release_note"), errors, "release_note")
    units = manifest.get("units")
    if not (isinstance(units, dict) and units):
        errors.append("units: must be a non-empty dict")
        units = {}
    selectable = manifest.get("selectable")
    if not isinstance(selectable, dict):
        errors.append("selectable: must be a dict")
        selectable = {}

    covered = set()
    for unit_name, unit in units.items():
        check_unit(unit_name, unit, errors)
        if isinstance(unit, dict) and isinstance(unit.get("files"), dict):
            for rel in unit["files"]:
                if rel in covered:
                    errors.append("duplicate path %r inside units" % rel)
                covered.add(rel)
    for rel, entry in selectable.items():
        check_file_entry(rel, entry, errors, require_note=True)
        if rel in covered:
            errors.append("duplicate path %r across sections" % rel)
        covered.add(rel)

    # --- presence + hashes (--fix-hashes recomputes mismatches) ---
    def iter_entries():
        for _, unit in units.items():
            if isinstance(unit, dict) and isinstance(unit.get("files"), dict):
                for rel, entry in unit["files"].items():
                    yield rel, entry
        for rel, entry in selectable.items():
            yield rel, entry

    for rel, entry in iter_entries():
        if not isinstance(entry, dict):
            continue
        if not os.path.isfile(os.path.join(root, rel)):
            errors.append("file missing on disk: %s" % rel)
            continue
        disk_sha = sha256_file(root, rel)
        if entry.get("sha256") != disk_sha:
            if fix_hashes:
                entry["sha256"] = disk_sha
                fixed += 1
            else:
                errors.append("sha256 mismatch: %s" % rel)

    # --- coverage against git ---
    git_ok, tracked = git_ls_files(root)
    if git_ok:
        shipped_untracked = []
        for rel in tracked:
            if rel in COVERAGE_SKIP:
                continue
            if rel not in covered:
                errors.append("tracked file not in manifest: %s" % rel)
        for rel in sorted(covered):
            if rel not in tracked:
                shipped_untracked.append(rel)
        if shipped_untracked:
            warnings.append(
                "manifest entries not yet tracked by git (new files, publish later): %s"
                % ", ".join(shipped_untracked)
            )
    else:
        warnings.append("no git repo found - coverage check skipped")

    # --- uncovered local files (walk, top level only) ---
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in WALK_SKIP_DIRS]
        else:
            dirnames[:] = []
        for fn in filenames:
            if fn in SKIP_NAMES:
                continue
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            rel = rel.replace(os.sep, "/")
            if rel in covered or rel in COVERAGE_SKIP:
                continue
            msg = "local file not in manifest: %s" % rel
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)

    # --- app_version consistency ---
    code_ver = read_app_version(root)
    if code_ver is not None and code_ver != app_ver:
        errors.append(
            "app_version %r != core/version.py __version__ %r" % (app_ver, code_ver)
        )

    code = 1 if errors else 0
    if json_out:
        print(json.dumps({
            "ok": code == 0,
            "errors": errors,
            "warnings": warnings,
            "entries": len(covered),
            "fixed_hashes": fixed,
            "app_version": app_ver,
        }, ensure_ascii=False, indent=2))
    else:
        for msg in errors:
            log("ERROR", msg)
        for msg in warnings:
            log("WARN", msg)
        if fixed:
            log("INFO", "recomputed %d sha256 value(s)" % fixed)
        if code == 0:
            log("OK", "manifest valid (%d entries, app_version %s)" % (len(covered), app_ver))
    return code, errors, warnings, fixed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="bootstrap the initial manifest")
    parser.add_argument(
        "--force", action="store_true", help="with --init: overwrite an existing manifest"
    )
    parser.add_argument("--fix-hashes", action="store_true", help="recompute stale sha256 values")
    parser.add_argument("--add", metavar="PATH", help="register a new file in the manifest")
    parser.add_argument("--strict", action="store_true", help="treat uncovered local files as errors")
    parser.add_argument("--json", action="store_true", help="machine-readable summary output")
    args = parser.parse_args(argv)

    root = find_root()
    manifest_path = os.path.join(root, MANIFEST_NAME)

    if args.init:
        if os.path.exists(manifest_path) and not args.force:
            log("ERROR", "%s already exists; use --force to overwrite" % MANIFEST_NAME)
            return 2
        git_ok, tracked = git_ls_files(root)
        if not git_ok or not tracked:
            log("ERROR", "--init requires a git repository with tracked files")
            return 2
        manifest = build_manifest(root, tracked)
        save_manifest(root, manifest)
        log("OK", "manifest bootstrapped: %s" % manifest_path)
        return 0

    if not os.path.exists(manifest_path):
        log("ERROR", "%s not found; run with --init first" % MANIFEST_NAME)
        return 2
    if args.add:
        manifest = load_manifest(root)
        return add_file(root, manifest, args.add)
    manifest = load_manifest(root)
    code, _, _, _ = verify(
        root,
        manifest,
        strict=args.strict,
        fix_hashes=args.fix_hashes,
        json_out=args.json,
    )
    if args.fix_hashes and code == 0:
        save_manifest(root, manifest)
    return code


if __name__ == "__main__":
    sys.exit(main())
