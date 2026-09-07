# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""core/updater_apply.py - cold-start applier for staged SagaAI updates.

Pure-stdlib module (no imports from `core`) that atomically applies update
files staged by `core.updater` into `<project_root>/.dev_agent/updates/`.
It is imported and executed from `app.py` BEFORE the Streamlit app starts,
so a running process never has its own code replaced underneath it.

Update store layout:

    <root>/.dev_agent/updates/
        pending.json           # user-confirmed selection: {files: {rel: {version, sha256}}, selected: [rel, ...]}
        pending/<rel path>     # staged new content, mirroring the project tree
        backup/<run_id>/<rel>  # pre-replacement snapshot of every changed file
        state.json             # last applied state per file (version, sha256, applied_at)
        health.json            # last cold-start apply outcome (shown in UI / logs)

Safety properties:
  - never raises: every outcome is returned as a report dict and mirrored
    into health.json;
  - every selected path is validated (rejects absolute paths, '..',
    backslashes, non-normalized forms) so a malicious manifest cannot write
    outside the project root;
  - every staged file is sha256-checked BEFORE anything is touched;
  - replacements are atomic (temp file + os.replace) and every replaced file
    is backed up first;
  - on any failure the whole run is rolled back from the run's backup;
  - applied files are removed from pending.json, so the store stays
    consistent across restarts.
"""

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone

UPDATES_RELDIR = ".dev_agent/updates"
PENDING_MANIFEST = "pending.json"
PENDING_DIR = "pending"
BACKUP_DIR = "backup"
STATE_FILE = "state.json"
HEALTH_FILE = "health.json"
RUN_META_NAME = ".run-meta.json"


def _utc_now():
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def updates_dir(root):
    return os.path.join(root, UPDATES_RELDIR)


def pending_dir(root):
    return os.path.join(updates_dir(root), PENDING_DIR)


def _backup_dir(root):
    return os.path.join(updates_dir(root), BACKUP_DIR)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path, default=None):
    """Read and parse a JSON file; return `default` on any error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _verify_rel(rel):
    """Validate a manifest-relative path; return it normalized or raise
    ValueError. Rejects absolute paths, '..' components, backslashes and
    non-normalized forms so a malicious manifest can never write outside
    the project root."""
    if not isinstance(rel, str) or not rel:
        raise ValueError("empty path")
    if "\\" in rel:
        raise ValueError("backslash in path: %r" % rel)
    norm = os.path.normpath(rel)
    parts = norm.split("/")
    if (
        norm.startswith("/")
        or ":" in parts[0]
        or ".." in parts
        or parts == [""]
    ):
        raise ValueError("unsafe path: %r" % rel)
    if rel != norm:
        raise ValueError("non-normalized path: %r" % rel)
    return norm


def _atomic_write_bytes(root, rel, data):
    """Atomically write bytes to <root>/<rel>: create parent dirs, write a
    temp file in the destination directory, then os.replace() it."""
    dst = os.path.join(root, rel)
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    tmp = dst + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _atomic_copy(src, dst):
    """Atomically copy a file: temp file in the destination directory +
    os.replace()."""
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    tmp = dst + ".tmp-%d" % os.getpid()
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_state(root):
    """Return the applied-state dict ({entries: {rel: {...}}, last_run: {...}}),
    creating defaults when state.json is absent or broken."""
    state = _read_json(os.path.join(updates_dir(root), STATE_FILE), None)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("entries", {})
    state.setdefault("last_run", {})
    return state


def save_state(root, state):
    _atomic_write_bytes(
        root,
        "/".join([UPDATES_RELDIR, STATE_FILE]),
        (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def read_pending(root):
    """Return (manifest, errors): the pending.json dict (or None) plus a list
    of structural errors."""
    mp = os.path.join(updates_dir(root), PENDING_MANIFEST)
    if not os.path.isfile(mp):
        return None, []
    manifest = _read_json(mp, None)
    if not isinstance(manifest, dict):
        return None, ["pending.json is not a JSON object"]
    return manifest, []


def load_health(root):
    """Return the last cold-start apply outcome (health.json), or None when
    absent or unreadable."""
    return _read_json(os.path.join(updates_dir(root), HEALTH_FILE), None)


def list_backup_runs(root):
    """Return backup run ids (oldest first), or [] when there is no store."""
    bd = _backup_dir(root)
    if not os.path.isdir(bd):
        return []
    return sorted(
        name for name in os.listdir(bd)
        if os.path.isdir(os.path.join(bd, name))
    )


def _report(ok, applied, error, logs, health_path):
    return {
        "ok": ok,
        "applied": list(applied),
        "failed": not ok,
        "error": error,
        "logs": list(logs),
        "health_path": health_path,
    }


def _write_health(root, health, logs):
    """Attach detail logs to the health record, persist it, return its path."""
    health["details"] = list(logs)
    _atomic_write_bytes(
        root,
        "/".join([UPDATES_RELDIR, HEALTH_FILE]),
        (json.dumps(health, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return os.path.join(updates_dir(root), HEALTH_FILE)


def _backup_existing(root, run_id, rel):
    """Snapshot <root>/<rel> into backup/<run_id>/<rel>; return True when the
    target existed (rollback must know whether to delete or restore it)."""
    src = os.path.join(root, rel)
    bak = os.path.join(_backup_dir(root), run_id, rel)
    if os.path.isfile(src):
        _atomic_copy(src, bak)
        return True
    return False


def _restore_backup(root, run_id, rel, existed):
    bak = os.path.join(_backup_dir(root), run_id, rel)
    dst = os.path.join(root, rel)
    if existed:
        _atomic_copy(bak, dst)
    else:
        if os.path.isfile(dst):
            os.remove(dst)


def _write_run_meta(root, run_id, meta):
    """Persist the run manifest {rel: existed_before_apply} so a later
    rollback can distinguish replaced files from newly created ones."""
    _atomic_write_bytes(
        root,
        "/".join([UPDATES_RELDIR, BACKUP_DIR, run_id, RUN_META_NAME]),
        (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _load_run_meta(run_path):
    """Read a run's manifest file; return None when absent or broken."""
    meta = _read_json(os.path.join(run_path, RUN_META_NAME), None)
    if not isinstance(meta, dict):
        return None
    return {
        rel: bool(value)
        for rel, value in meta.items()
        if isinstance(rel, str)
    }


def apply_pending(root, logger=None):
    """Apply the staged updates for <root> at cold start. Never raises.

    Flow: read pending.json -> validate every selected path and its staged
    sha256 BEFORE touching anything -> per file: backup, atomic replace,
    verify hash -> on any failure roll the whole run back -> update
    state.json and pending.json, write health.json. Returns a report dict
    {ok, applied, failed, error, logs, health_path}.
    """
    log = logger if logger is not None else (lambda msg: None)
    logs = []

    def note(msg):
        logs.append(msg)
        log(msg)

    health = {
        "ok": False,
        "at": _utc_now(),
        "applied": [],
        "error": None,
        "details": [],
    }

    manifest, errors = read_pending(root)
    health_path = os.path.join(updates_dir(root), HEALTH_FILE)
    if manifest is None:
        if errors:
            err = errors[0]
            note("invalid %s: %s" % (PENDING_MANIFEST, err))
            health["error"] = err
            _write_health(root, health, logs)
            return _report(False, [], err, logs, health_path)
        # Nothing staged: report healthy cold start.
        note("no staged updates (%s absent)" % PENDING_MANIFEST)
        health["ok"] = True
        health_path = _write_health(root, health, logs)
        return _report(True, [], None, logs, health_path)

    files = manifest.get("files") or {}
    if not isinstance(files, dict):
        err = "pending.json 'files' is not an object"
        note("invalid %s: %s" % (PENDING_MANIFEST, err))
        health["error"] = err
        _write_health(root, health, logs)
        return _report(False, [], err, logs, health_path)
    selected = manifest.get("selected") or list(files.keys())

    # ---- pre-validate the whole plan before changing anything -------------
    plan = []  # (rel, expected_sha, staged_src)
    for rel in selected:
        try:
            rel = _verify_rel(rel)
        except ValueError as exc:
            note("unsafe path rejected: %s" % exc)
            health["error"] = str(exc)
            _write_health(root, health, logs)
            return _report(False, [], str(exc), logs, health_path)
        entry = files.get(rel)
        if not isinstance(entry, dict) or not entry.get("sha256"):
            err = "no sha256 for %s in pending.json" % rel
            note("invalid %s: %s" % (PENDING_MANIFEST, err))
            health["error"] = err
            _write_health(root, health, logs)
            return _report(False, [], err, logs, health_path)
        src = os.path.join(pending_dir(root), rel)
        if not os.path.isfile(src):
            err = "staged file missing for %s" % rel
            note(err)
            health["error"] = err
            _write_health(root, health, logs)
            return _report(False, [], err, logs, health_path)
        actual = _sha256_file(src)
        expected = entry["sha256"]
        if actual != expected:
            err = "sha256 mismatch for %s: expected %s, got %s" % (
                rel, expected, actual)
            note(err)
            health["error"] = err
            _write_health(root, health, logs)
            return _report(False, [], err, logs, health_path)
        plan.append((rel, expected, src))

    # ---- apply with backups; roll back everything on any failure -----------
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    state = load_state(root)
    applied = []
    existed = {}
    run_error = None
    for rel, expected, src in plan:
        try:
            existed[rel] = _backup_existing(root, run_id, rel)
            _atomic_copy(src, os.path.join(root, rel))
            dst_hash = _sha256_file(os.path.join(root, rel))
            if dst_hash != expected:
                raise ValueError("post-copy hash mismatch for %s" % rel)
            applied.append(rel)
            entry = dict(files[rel])
            entry["applied_at"] = _utc_now()
            entry["sha256"] = dst_hash
            state["entries"][rel] = entry
            note("applied %s" % rel)
        except Exception as exc:  # noqa: BLE001 - the rollback path must catch all
            run_error = "%s: %s" % (rel, exc)
            note("FAILED %s: %s" % (rel, exc))
            break

    # Record which targets existed before this run so a later rollback can
    # distinguish replaced files from newly created ones.
    _write_run_meta(root, run_id, existed)

    if run_error:
        note("rolling back %d file(s) of this run" % len(existed))
        for rel in list(existed):
            try:
                _restore_backup(root, run_id, rel, existed[rel])
                state["entries"].pop(rel, None)
                note("rolled back %s" % rel)
            except Exception as exc:  # noqa: BLE001 - keep restoring the rest
                note("rollback failed for %s: %s" % (rel, exc))
        state["last_run"] = {"at": _utc_now(), "ok": False, "error": run_error}
        save_state(root, state)
        health["error"] = run_error
        health_path = _write_health(root, health, logs)
        return _report(False, applied, run_error, logs, health_path)

    # ---- success: drop applied staged files, update pending/state ----------
    for rel in applied:
        src = os.path.join(pending_dir(root), rel)
        try:
            os.remove(src)
        except OSError as exc:
            note("could not remove staged %s: %s" % (rel, exc))
    state["last_run"] = {
        "at": _utc_now(),
        "ok": True,
        "error": None,
        "applied": list(applied),
    }
    save_state(root, state)

    remaining_files = {
        rel: entry for rel, entry in files.items() if rel not in applied
    }
    mp = os.path.join(updates_dir(root), PENDING_MANIFEST)
    if remaining_files:
        new_manifest = dict(manifest)
        new_manifest["files"] = remaining_files
        new_manifest["selected"] = [
            rel for rel in selected
            if rel in remaining_files and isinstance(rel, str)
        ]
        _atomic_write_bytes(
            root,
            "/".join([UPDATES_RELDIR, PENDING_MANIFEST]),
            (json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    else:
        try:
            os.remove(mp)
        except OSError as exc:
            note("could not remove %s: %s" % (PENDING_MANIFEST, exc))

    health["ok"] = True
    health["applied"] = list(applied)
    health_path = _write_health(root, health, logs)
    return _report(True, applied, None, logs, health_path)


def rollback(root, rel=None, run_id=None):
    """Restore <rel> (or every file of run <run_id>; default: the newest run)
    from the backup store. Files that did not exist before the run are
    deleted. Returns a report dict {ok, restored, error}."""
    bd = _backup_dir(root)
    if not os.path.isdir(bd):
        return {"ok": False, "restored": [], "error": "no backup store at %s" % bd}
    if run_id is None:
        runs = sorted(
            name for name in os.listdir(bd)
            if os.path.isdir(os.path.join(bd, name))
        )
        if not runs:
            return {"ok": False, "restored": [], "error": "no backup runs found"}
        run_id = runs[-1]
    run_path = os.path.join(bd, run_id)
    if not os.path.isdir(run_path):
        return {"ok": False, "restored": [], "error": "unknown backup run: %s" % run_id}

    meta = _load_run_meta(run_path) or {}

    if rel is not None:
        try:
            rel = _verify_rel(rel)
        except ValueError as exc:
            return {"ok": False, "restored": [], "error": str(exc)}
        existed = meta.get(rel, True)
        if not existed:
            dst = os.path.join(root, rel)
            if os.path.isfile(dst):
                try:
                    os.remove(dst)
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "restored": [], "error": str(exc)}
            state = load_state(root)
            state["entries"].pop(rel, None)
            save_state(root, state)
            return {"ok": True, "restored": [rel], "error": None}
        backup_rel = os.path.join(run_path, rel)
        if not os.path.isfile(backup_rel):
            return {
                "ok": False,
                "restored": [],
                "error": "no backup for %s in run %s" % (rel, run_id),
            }
        try:
            _atomic_copy(backup_rel, os.path.join(root, rel))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "restored": [], "error": str(exc)}
        state = load_state(root)
        state["entries"].pop(rel, None)
        save_state(root, state)
        return {"ok": True, "restored": [rel], "error": None}

    restored = []
    failures = []
    for dirpath, _dirnames, filenames in os.walk(run_path):
        for name in filenames:
            if name == RUN_META_NAME:
                continue
            file_rel = os.path.relpath(os.path.join(dirpath, name), run_path)
            try:
                _atomic_copy(os.path.join(dirpath, name), os.path.join(root, file_rel))
                restored.append(file_rel)
            except Exception as exc:  # noqa: BLE001
                failures.append("%s: %s" % (file_rel, exc))
    # Newly created files (no backup copy) are deleted.
    for file_rel, existed in meta.items():
        if existed:
            continue
        dst = os.path.join(root, file_rel)
        if os.path.isfile(dst):
            try:
                os.remove(dst)
            except Exception as exc:  # noqa: BLE001
                failures.append("delete %s: %s" % (file_rel, exc))
            else:
                if file_rel not in restored:
                    restored.append(file_rel)
    state = load_state(root)
    for file_rel in restored:
        state["entries"].pop(file_rel, None)
    save_state(root, state)
    if failures:
        return {"ok": False, "restored": restored, "error": "; ".join(failures)}
    return {"ok": True, "restored": restored, "error": None}


def main(argv=None):
    """CLI entry point:

    python3 core/updater_apply.py [--root DIR]                 # apply pending
    python3 core/updater_apply.py --root DIR --rollback [REL]  # restore

    Prints the report as JSON and returns 0 on success, 1 on failure.
    Intended for manual cold-start runs and diagnostics.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    root = os.getcwd()
    if "--root" in argv:
        idx = argv.index("--root")
        if len(argv) <= idx + 1:
            print("--root requires a path", file=sys.stderr)
            return 2
        root = argv[idx + 1]
    if "--rollback" in argv:
        idx = argv.index("--rollback")
        rel = None
        if len(argv) > idx + 1 and not argv[idx + 1].startswith("--"):
            rel = argv[idx + 1]
        report = rollback(root, rel=rel)
    else:
        report = apply_pending(root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
