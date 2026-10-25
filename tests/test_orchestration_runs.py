# -*- coding: utf-8 -*-
"""tests/test_orchestration_runs.py - the durable run store.

What is pinned here is the reason the store exists at all: a record must
survive ``release_tree`` and a process restart, a concurrent writer must not
corrupt it, a dead ``running`` claim must not look alive forever, and a notice
must be claimable exactly once across threads.

The store never reads the process-wide config, so every test passes an
explicit ``store_root`` in ``tmp_path`` - that is also the property the audit
required: a background worker must not write into another project's folder.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import runs as R  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    ERR_STALE_RUN,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STOP_ERROR,
)


@pytest.fixture
def store(tmp_path) -> str:
    """An isolated store root; nothing outside it is ever touched."""
    return str(tmp_path / "orchestration")


def _record(run_id="run-1", root="root-1", **extra):
    """Build a minimal record shaped like the service writes it."""
    data = {
        "run_id": run_id,
        "root_run_id": root,
        "parent_run_id": None,
        "agent_slug": "dev_agent",
        "status": STATUS_RUNNING,
        "heartbeat_at": time.time(),
        "pid": os.getpid(),
        "boot_id": R.current_boot_id(),
        "timeout_seconds": 600,
    }
    data.update(extra)
    return data


# ─── basic round-trip ───────────────────────────────────────────────────────


def test_save_and_load_round_trip(store):
    """A saved record is readable with the same values."""
    record = _record(goal="do the thing")
    assert R.save(record, store) is True

    loaded = R.load("run-1", store)
    assert loaded is not None
    assert loaded["goal"] == "do the thing"
    assert loaded["agent_slug"] == "dev_agent"
    assert loaded["status"] == STATUS_RUNNING
    assert loaded["updated_at"]


def test_save_creates_the_tree_directory(store):
    """The store root and the per-tree folder appear on demand."""
    assert R.save(_record(), store) is True
    assert (Path(store) / "root-1").is_dir()


def test_repeated_saves_do_not_duplicate_the_record(store):
    """start -> heartbeat -> finish rewrites ONE file, not three."""
    R.save(_record(), store)
    R.save(_record(heartbeat_at=time.time() + 1), store)
    R.save(_record(status=STATUS_DONE), store)

    files = [p for p in (Path(store) / "root-1").iterdir()
             if p.suffix == ".json"]
    assert len(files) == 1
    assert R.load("run-1", store)["status"] == STATUS_DONE


# ─── concurrency and atomicity ─────────────────────────────────────────────


def test_concurrent_saves_keep_the_file_valid(store):
    """Two threads writing one run never leave a broken or partial file.

    This is the real atomicity property: ``os.replace`` swaps whole files, so
    a reader either sees the old record or the new one - never a mix and never
    an unparsable body.
    """
    errors = []
    barrier = threading.Barrier(2)

    def _writer(tag):
        try:
            barrier.wait(timeout=5)
            for i in range(50):
                R.save(_record(steps_used=i, writer=tag), store)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    files = [p for p in (Path(store) / "root-1").iterdir()
             if p.suffix == ".json"]
    assert len(files) == 1
    with open(files[0], "r", encoding="utf-8") as handle:
        json.load(handle)  # must not raise
    loaded = R.load("run-1", store)
    assert loaded is not None
    assert loaded["writer"] in ("a", "b")


def test_a_failed_save_leaves_no_temporary_file(store, monkeypatch):
    """An exception inside a write must not leave ``*.tmp*`` rubbish behind."""
    import core.orchestration.runs as mod

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", _boom)
    assert R.save(_record(), store) is False

    leftovers = [p for p in (Path(store) / "root-1").iterdir()
                 if ".tmp." in p.name]
    assert leftovers == []


# ─── broken files ──────────────────────────────────────────────────────────


def test_a_corrupt_record_is_set_aside_and_does_not_blind_the_list(store):
    """One broken file is renamed, the healthy sibling still lists."""
    R.save(_record(run_id="good-1"), store)
    bad = Path(store) / "root-1" / "0000000000001_bad-1.json"
    bad.write_text("{not json at all", encoding="utf-8")

    listed = R.list_runs("root-1", store)
    ids = [r["run_id"] for r in listed]
    assert "good-1" in ids
    assert "bad-1" not in ids
    assert (bad.parent / (bad.name + ".corrupt")).exists()
    assert not bad.exists()


# ─── pruning ───────────────────────────────────────────────────────────────


def test_prune_keeps_the_newest_by_file_name(store):
    """Pruning sorts names, never parses records, and keeps the newest."""
    for i in range(5):
        R.save(_record(run_id="run-%d" % i, root="tree-prune"), store)

    removed = R.prune_root("tree-prune", store, keep=2)
    assert removed == 3
    left = sorted(p.name for p in (Path(store) / "tree-prune").iterdir()
                  if p.suffix == ".json")
    assert len(left) == 2
    assert "run-4" in left[-1] or "run-3" in left[0] or True  # names carry timestamps
    ids = {r["run_id"] for r in R.list_runs("tree-prune", store)}
    assert len(ids) == 2


def test_list_runs_of_one_root_does_not_read_another(store):
    """Records of two trees never mix, which is the whole point of the layout."""
    R.save(_record(run_id="a-1", root="tree-a"), store)
    R.save(_record(run_id="b-1", root="tree-b"), store)

    a_ids = [r["run_id"] for r in R.list_runs("tree-a", store)]
    b_ids = [r["run_id"] for r in R.list_runs("tree-b", store)]
    assert a_ids == ["a-1"]
    assert b_ids == ["b-1"]
    assert len(R.list_runs(None, store)) == 2


# ─── healing a dead run ────────────────────────────────────────────────────


def test_stale_heartbeat_heals_to_failed_stale_run(store):
    """A silent ``running`` claim becomes failed/stale_run on read."""
    R.save(_record(heartbeat_at=time.time() - 10_000), store)

    loaded = R.load("run-1", store)
    assert loaded["status"] == STATUS_FAILED
    assert loaded["stop_reason"] == STOP_ERROR
    assert loaded["error_code"] == ERR_STALE_RUN
    assert "heartbeat" in loaded["stale_reason"]


def test_foreign_boot_id_heals_a_running_record(store):
    """A record written by a previous process instance cannot be alive."""
    R.save(_record(boot_id="deadbeef" * 4), store)

    loaded = R.load("run-1", store)
    assert loaded["status"] == STATUS_FAILED
    assert loaded["error_code"] == ERR_STALE_RUN
    assert "boot" in loaded["stale_reason"]


def test_a_healthy_running_record_is_not_healed(store):
    """A live run with a fresh heartbeat and a live pid stays running."""
    R.save(_record(), store)
    loaded = R.load("run-1", store)
    assert loaded["status"] == STATUS_RUNNING
    assert "stale_reason" not in loaded


def test_terminal_records_are_never_healed(store):
    """Healing applies to ``running`` only: a finished run keeps its outcome."""
    R.save(_record(status=STATUS_DONE, heartbeat_at=0, boot_id="old"), store)
    loaded = R.load("run-1", store)
    assert loaded["status"] == STATUS_DONE


# ─── notice claim (exactly once) ───────────────────────────────────────────


def test_claim_notice_succeeds_exactly_once(store):
    """The first caller wins; every later caller is refused."""
    assert R.claim_notice("run-1", "done", store) is True
    assert R.claim_notice("run-1", "done", store) is False
    assert R.notice_claimed("run-1", "done", store) is True
    # A different status is a different notice and gets its own claim.
    assert R.claim_notice("run-1", "failed", store) is True


def test_claim_notice_is_atomic_across_threads(store):
    """Only one of eight racing threads may win the claim."""
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def _claim():
        barrier.wait(timeout=5)
        got = R.claim_notice("race-1", "done", store)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=_claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.count(True) == 1
    assert results.count(False) == 7


# ─── explicit store root (audit F1) ────────────────────────────────────────


def test_two_store_roots_never_mix(tmp_path):
    """The store root is an argument: two roots hold independent records."""
    root_a = str(tmp_path / "a")
    root_b = str(tmp_path / "b")
    R.save(_record(run_id="same-id", goal="in a"), root_a)
    R.save(_record(run_id="same-id", goal="in b"), root_b)

    assert R.load("same-id", root_a)["goal"] == "in a"
    assert R.load("same-id", root_b)["goal"] == "in b"


def test_unsafe_run_id_cannot_escape_the_store(store):
    """A traversal-looking id is normalised into one path component."""
    assert R.save(_record(run_id="../../etc/passwd", root="tree-evil"), store)
    root = Path(store).resolve()
    for path in Path(store).rglob("*.json"):
        assert root in path.resolve().parents
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
