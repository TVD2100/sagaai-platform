# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""tests/test_updater_apply.py - targeted tests for core/updater_apply.py.

Covers the cold-start applier: apply + rollback, atomic failure handling,
path validation, and staged-file lifecycle.
"""

import hashlib
import json
import os

from core.updater_apply import (
    apply_pending,
    pending_dir,
    rollback,
    updates_dir,
)


def _sha(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _write_staged(root, rel, content):
    p = os.path.join(pending_dir(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def _manifest(root, rel, content, selected=None):
    files = {rel: {"version": "1.2.3", "sha256": _sha(content)}}
    m = {"files": files, "selected": selected or [rel]}
    with open(os.path.join(updates_dir(root), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)


def setup_staged(root, rel, content):
    _write_staged(root, rel, content)
    _manifest(root, rel, content)


def test_apply_single_file_happy_path(tmp_path):
    root = str(tmp_path)
    target = "langs/ru.json"
    os.makedirs(os.path.join(root, "langs"), exist_ok=True)
    with open(os.path.join(root, target), "w", encoding="utf-8") as f:
        f.write('{"old": true}\n')

    new_content = '{"new": true}\n'
    setup_staged(root, target, new_content)

    report = apply_pending(root)
    assert report["ok"], report
    assert report["applied"] == [target]

    with open(os.path.join(root, target), encoding="utf-8") as f:
        assert f.read() == new_content
    assert not os.path.exists(os.path.join(pending_dir(root), target))
    assert not os.path.exists(os.path.join(updates_dir(root), "pending.json"))

    state = json.load(open(os.path.join(updates_dir(root), "state.json"), encoding="utf-8"))
    assert state["entries"][target]["version"] == "1.2.3"
    assert state["last_run"]["ok"] is True


def test_apply_no_pending_returns_ok(tmp_path):
    report = apply_pending(str(tmp_path))
    assert report["ok"]
    assert report["applied"] == []


def test_apply_rejects_unsafe_path(tmp_path):
    root = str(tmp_path)
    os.makedirs(pending_dir(root), exist_ok=True)
    with open(os.path.join(pending_dir(root), "escape.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    m = {"files": {"../escape.txt": {"version": "1.0.0", "sha256": _sha("x")}}}
    with open(os.path.join(updates_dir(root), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)

    report = apply_pending(root)
    assert not report["ok"]
    assert "unsafe path" in report["error"]
    assert not os.path.exists(os.path.join(root, "escape.txt"))


def test_apply_rollback_on_failed_second_file(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "a"), exist_ok=True)
    os.makedirs(os.path.join(root, "b"), exist_ok=True)
    with open(os.path.join(root, "a", "f.txt"), "w", encoding="utf-8") as f:
        f.write("old-a")
    with open(os.path.join(root, "b", "f.txt"), "w", encoding="utf-8") as f:
        f.write("old-b")

    first = "a/f.txt"
    second = "b/f.txt"
    _write_staged(root, first, "new-a")
    # second: manifest sha mismatch of staged content
    bad_sha = _sha("not-the-staged-value")
    _write_staged(root, second, "new-b")
    m = {
        "files": {
            first: {"version": "2.0.0", "sha256": _sha("new-a")},
            second: {"version": "2.0.0", "sha256": bad_sha},
        },
        "selected": [first, second],
    }
    with open(os.path.join(updates_dir(root), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)

    report = apply_pending(root)
    assert not report["ok"]
    assert "sha256 mismatch" in report["error"]
    # rollback restored the original contents
    assert open(os.path.join(root, first), encoding="utf-8").read() == "old-a"
    assert open(os.path.join(root, second), encoding="utf-8").read() == "old-b"
    # pending files not consumed
    assert os.path.exists(os.path.join(pending_dir(root), first))
    assert os.path.exists(os.path.join(pending_dir(root), second))


def test_apply_handles_new_file_then_rollback(tmp_path):
    # second file is new -> apply creates it; failure on first rolls back
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "existing"), exist_ok=True)
    with open(os.path.join(root, "existing", "f.txt"), "w", encoding="utf-8") as f:
        f.write("old")

    first = "existing/f.txt"
    second = "brand/new.txt"
    _write_staged(root, first, "new")
    _write_staged(root, second, "brand-new-content")
    # make first sha mismatch so the run fails
    _manifest(root, first, "new", selected=[first, second])
    m = json.load(open(os.path.join(updates_dir(root), "pending.json"), encoding="utf-8"))
    m["files"][first]["sha256"] = _sha("wrong")
    m["files"][second] = {"version": "1.5.0", "sha256": _sha("brand-new-content")}
    with open(os.path.join(updates_dir(root), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)

    report = apply_pending(root)
    assert not report["ok"]
    assert open(os.path.join(root, first), encoding="utf-8").read() == "old"
    assert not os.path.exists(os.path.join(root, second))


def test_apply_new_file_success(tmp_path):
    root = str(tmp_path)
    rel = "new_dir/new_file.txt"
    setup_staged(root, rel, "hello")
    report = apply_pending(root)
    assert report["ok"], report
    assert open(os.path.join(root, rel), encoding="utf-8").read() == "hello"


def test_apply_removes_only_applied(tmp_path):
    # two files, apply only first -> pending.json keeps second
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "k"), exist_ok=True)
    with open(os.path.join(root, "k", "1.txt"), "w", encoding="utf-8") as f:
        f.write("old1")
    with open(os.path.join(root, "k", "2.txt"), "w", encoding="utf-8") as f:
        f.write("old2")
    c1, c2 = "new1", "new2"
    _write_staged(root, "k/1.txt", c1)
    _write_staged(root, "k/2.txt", c2)
    m = {
        "files": {
            "k/1.txt": {"version": "1.0.1", "sha256": _sha(c1)},
            "k/2.txt": {"version": "1.0.2", "sha256": _sha(c2)},
        },
        "selected": ["k/1.txt"],
    }
    with open(os.path.join(updates_dir(root), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)

    report = apply_pending(root)
    assert report["ok"], report
    assert report["applied"] == ["k/1.txt"]
    # pending.json still exists with remaining file
    remaining = json.load(open(os.path.join(updates_dir(root), "pending.json"), encoding="utf-8"))
    assert list(remaining["files"].keys()) == ["k/2.txt"]
    assert os.path.exists(os.path.join(pending_dir(root), "k/2.txt"))


def test_apply_updates_state_with_version(tmp_path):
    root = str(tmp_path)
    rel = "core/version.py"
    os.makedirs(os.path.join(root, "core"), exist_ok=True)
    with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
        f.write('__version__ = "1.0.0"')
    new_version = '2.0.0'
    setup_staged(root, rel, '__version__ = "%s"' % new_version)
    report = apply_pending(root)
    assert report["ok"]
    state = json.load(open(os.path.join(updates_dir(root), "state.json"), encoding="utf-8"))
    assert state["entries"][rel]["version"] == "1.2.3"


def test_rollback_nonexistent_store(tmp_path):
    report = rollback(str(tmp_path))
    assert not report["ok"]
    assert "no backup" in report["error"]


def test_rollback_single_file(tmp_path):
    root = str(tmp_path)
    rel = "file.txt"
    with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
        f.write("before")
    setup_staged(root, rel, "after")
    report = apply_pending(root)
    assert report["ok"]
    assert open(os.path.join(root, rel), encoding="utf-8").read() == "after"

    rb = rollback(root, rel=rel)
    assert rb["ok"], rb
    assert open(os.path.join(root, rel), encoding="utf-8").read() == "before"
    state = json.load(open(os.path.join(updates_dir(root), "state.json"), encoding="utf-8"))
    assert rel not in state["entries"]


def test_rollback_newly_created_file_deletes_it(tmp_path):
    root = str(tmp_path)
    rel = "created_by_update.txt"
    setup_staged(root, rel, "new file")
    report = apply_pending(root)
    assert report["ok"]
    assert os.path.exists(os.path.join(root, rel))

    rb = rollback(root, rel=rel)
    assert rb["ok"], rb
    assert not os.path.exists(os.path.join(root, rel))


def test_rollback_whole_run(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "d"), exist_ok=True)
    with open(os.path.join(root, "d", "1.txt"), "w", encoding="utf-8") as f:
        f.write("old1")
    content2 = "old2"
    # second file does not exist yet
    c1, c2 = "new1", "new2"
    _write_staged(root, "d/1.txt", c1)
    _write_staged(root, "d/2.txt", c2)
    m = {
        "files": {
            "d/1.txt": {"version": "2.0.0", "sha256": _sha(c1)},
            "d/2.txt": {"version": "2.0.0", "sha256": _sha(c2)},
        }
    }
    with open(os.path.join(updates_dir(root), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)
    report = apply_pending(root)
    assert report["ok"], report

    rb = rollback(root)
    assert rb["ok"], rb
    assert set(rb["restored"]) == {"d/1.txt", "d/2.txt"}
    # 'd/2.txt' didn't exist before the run, so it should be deleted
    assert not os.path.exists(os.path.join(root, "d", "2.txt"))
    assert open(os.path.join(root, "d", "1.txt"), encoding="utf-8").read() == "old1"


def test_apply_is_idempotent_when_pending_empty(tmp_path):
    root = str(tmp_path)
    os.makedirs(updates_dir(root), exist_ok=True)
    r1 = apply_pending(root)
    r2 = apply_pending(root)
    assert r1["ok"] and r2["ok"]
    assert r2["applied"] == []
    assert any("no staged updates" in line for line in r2["logs"])


def test_apply_rejects_nonexistent_staged_file(tmp_path):
    root = str(tmp_path)
    rel = "ghost.txt"
    m = {"files": {rel: {"version": "1.0.0", "sha256": _sha("x")}}}
    os.makedirs(updates_dir(root), exist_ok=True)
    with open(os.path.join(updates_dir(root), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)
    report = apply_pending(root)
    assert not report["ok"]
    assert "staged file missing" in report["error"]


def test_apply_rejects_empty_sha(tmp_path):
    root = str(tmp_path)
    rel = "file.txt"
    with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
        f.write("old")
    _write_staged(root, rel, "new")
    m = {"files": {rel: {"version": "1.0.0", "sha256": ""}}}
    with open(os.path.join(updates_dir(root), "pending.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)
    report = apply_pending(root)
    assert not report["ok"]
    assert "no sha256" in report["error"]


def test_load_state_on_broken_json(tmp_path):
    from core.updater_apply import load_state

    root = str(tmp_path)
    os.makedirs(updates_dir(root), exist_ok=True)
    with open(os.path.join(updates_dir(root), "state.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    state = load_state(root)
    assert state["entries"] == {}
    assert state["last_run"] == {}


def test_state_file_not_part_of_manifest(tmp_path):
    # .dev_agent must never be shipped; updater files live in the store only
    from core.updater_apply import UPDATES_RELDIR

    assert UPDATES_RELDIR == ".dev_agent/updates"
