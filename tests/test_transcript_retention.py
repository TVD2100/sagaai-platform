# -*- coding: utf-8 -*-
"""tests/test_transcript_retention.py - keeping the store bounded.

The store grows forever unless something stops it, and every way of stopping
it is a way to lose data. The tests therefore separate the CHEAP decisions
(compress, orphan detection) from the EXPENSIVE one (delete), and check the
rules that keep a deletion from reaching a conversation a user can still
open:

- only rotated segments are cut, never the active file;
- a transcript whose thread still has a row is never expired, however quiet;
- a dry run reports and removes nothing.
"""
import gzip
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import transcript as T  # noqa: E402


@pytest.fixture(autouse=True)
def history_root(tmp_path, monkeypatch):
    """Point the transcript store at a fresh folder."""
    monkeypatch.setattr(T, "history_root", lambda: tmp_path)
    return tmp_path


def _write(thread_id, count=3, parent=None):
    """Write *count* ordinary records into a thread."""
    for i in range(count):
        T.append_record({"v": 1, "seq": i + 1, "role": "user",
                         "content": f"message {i}", "ts": ""},
                        thread_id, parent)


def _rotate(thread_id, parent=None):
    """Force a rotation by making the active file look oversized."""
    folder = T.thread_dir(thread_id, parent)
    active = folder / T.TRANSCRIPT_NAME
    original = T.MAX_FILE_BYTES
    try:
        T.MAX_FILE_BYTES = 0
        T._active_segment(folder)
    finally:
        T.MAX_FILE_BYTES = original
    return active


# ─── numbering survives compression ────────────────────────────────────────

def test_a_compressed_segment_keeps_its_number(history_root):
    """The sequence is defined by the number, not by the suffix."""
    _write("t1")
    folder = T.thread_dir("t1")
    (folder / "transcript.1.jsonl").write_text("{}\n", encoding="utf-8")
    (folder / "transcript.2.jsonl.gz").write_text("", encoding="utf-8")

    names = [p.name for p in T._segments(folder)]

    assert names == ["transcript.1.jsonl", "transcript.2.jsonl.gz",
                     "transcript.jsonl"]


def test_the_active_file_is_always_last(history_root):
    """New records go to the end of the log, so it must read last."""
    _write("t2")
    folder = T.thread_dir("t2")
    (folder / "transcript.9.jsonl").write_text("{}\n", encoding="utf-8")

    assert T._segments(folder)[-1].name == T.TRANSCRIPT_NAME


def test_a_rotated_segment_does_not_overwrite_a_compressed_one(history_root):
    """Rotation numbers past everything, compressed files included."""
    _write("t3")
    folder = T.thread_dir("t3")
    (folder / "transcript.4.jsonl.gz").write_text("", encoding="utf-8")

    _rotate("t3")

    assert (folder / "transcript.5.jsonl").exists()


# ─── a compressed segment still reads ──────────────────────────────────────

def test_records_are_yielded_from_a_compressed_segment(history_root):
    """Compression is a storage detail; a reader must not notice it."""
    _write("t4", count=0)
    folder = T.thread_dir("t4")
    folder.mkdir(parents=True, exist_ok=True)
    line = '{"v": 1, "seq": 1, "role": "user", "content": "from gz"}\n'
    with gzip.open(folder / "transcript.1.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(line)
    _write("t4", count=1)

    texts = [r["content"] for r in T.iter_records("t4")]

    assert texts == ["from gz", "message 0"]


def test_exists_sees_a_compressed_only_thread(history_root):
    folder = T.thread_dir("t5")
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / "transcript.1.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write('{"role": "user", "content": "x"}\n')

    assert T.exists("t5") is True


# ─── compression ───────────────────────────────────────────────────────────

def test_compression_replaces_the_plain_segment(history_root):
    """Keeping both would defeat the point of compressing at all."""
    _write("t6")
    _rotate("t6")
    folder = T.thread_dir("t6")
    plain = folder / "transcript.1.jsonl"

    result = T.compress_segment(plain)

    assert result["ok"] is True
    assert plain.exists() is False
    assert (folder / "transcript.1.jsonl.gz").exists() is True


def test_compression_shrinks_the_file(history_root):
    """Repeated content must not cost full size forever."""
    _write("t7", count=200)
    _rotate("t7")
    plain = T.thread_dir("t7") / "transcript.1.jsonl"

    result = T.compress_segment(plain)

    assert result["bytes_after"] < result["bytes_before"]


def test_an_already_compressed_segment_is_left_alone(history_root):
    _write("t8", count=1)
    _rotate("t8")
    folder = T.thread_dir("t8")
    plain = folder / "transcript.1.jsonl"
    T.compress_segment(plain)

    again = T.compress_segment(folder / "transcript.1.jsonl.gz")

    assert again["ok"] is True
    assert again["skipped"] == "already compressed"


def test_the_active_file_is_never_compressed(history_root):
    """Compressing an open-for-append file would lose every concurrent write."""
    _write("t9", count=3)

    result = T.compress_transcript("t9", older_than_days=0)

    assert result["compressed"] == 0
    assert (T.thread_dir("t9") / T.TRANSCRIPT_NAME).exists() is True


def test_a_young_segment_is_not_compressed(history_root):
    """Compression is for cold data; a fresh segment is still being read."""
    _write("t10")
    _rotate("t10")

    result = T.compress_transcript("t10", older_than_days=30)

    assert result["compressed"] == 0


def test_compression_keeps_the_time_of_the_last_real_write(history_root):
    """Compression is not activity: it must not reset the retention clock."""
    _write("t15")
    _rotate("t15")
    folder = T.thread_dir("t15")
    plain = folder / "transcript.1.jsonl"
    stamp = time.time() - 40 * 86400
    os.utime(plain, (stamp, stamp))

    T.compress_segment(plain)

    compressed = folder / "transcript.1.jsonl.gz"
    assert abs(compressed.stat().st_mtime - stamp) < 2


def test_a_compressed_segment_still_ages_out_of_the_window(history_root):
    """A compressed segment must not look fresh enough to survive forever."""
    _write("t16")
    _rotate("t16")
    folder = T.thread_dir("t16")
    plain = folder / "transcript.1.jsonl"
    stamp = time.time() - 40 * 86400
    os.utime(plain, (stamp, stamp))
    T.compress_segment(plain)

    result = T.compress_transcript("t16", older_than_days=0)

    assert result["compressed"] == 0


# ─── the quota drops whole segments, oldest first ──────────────────────────

def test_the_quota_drops_the_oldest_segment(history_root):
    _write("t11")
    _rotate("t11")
    _write("t11", count=2)
    folder = T.thread_dir("t11")
    assert (folder / "transcript.1.jsonl").exists() is True

    result = T.enforce_quota("t11", max_bytes=10)

    assert result["dropped"] == ["transcript.1.jsonl"]


def test_the_quota_keeps_the_active_file_whatever_happens(history_root):
    """The newest records are the ones a resume is about to read."""
    _write("t12", count=30)

    T.enforce_quota("t12", max_bytes=1)

    assert (T.thread_dir("t12") / T.TRANSCRIPT_NAME).exists() is True


def test_a_thread_under_its_quota_is_untouched(history_root):
    _write("t13")
    _rotate("t13")

    result = T.enforce_quota("t13", max_bytes=T.MAX_THREAD_BYTES)

    assert result["dropped"] == []


def test_the_quota_reports_what_it_could_not_reclaim(history_root):
    """An oversized active file is a fact, not a silent failure."""
    _write("t14", count=50)

    result = T.enforce_quota("t14", max_bytes=1)

    assert result["ok"] is True
    assert result["bytes_after"] > 1


# ─── orphans: only folders no row points at ────────────────────────────────

def test_a_child_folder_without_a_row_is_an_orphan(history_root):
    _write("dialog", count=1)
    _write("lost-child", count=1, parent="dialog")

    orphans = T.find_orphans(is_known=lambda tid: False)

    assert [o["thread_id"] for o in orphans] == ["lost-child"]
    assert orphans[0]["parent_thread_id"] == "dialog"


def test_a_child_folder_with_a_row_is_kept(history_root):
    _write("dialog", count=1)
    _write("known-child", count=1, parent="dialog")

    assert T.find_orphans(is_known=lambda tid: True) == []


def test_a_dialog_folder_without_a_row_is_not_an_orphan(history_root):
    """A legacy dialog may hold uploads before it holds a transcript."""
    _write("legacy-dialog", count=1)

    assert T.find_orphans(is_known=lambda tid: False) == []


def test_an_unreadable_row_store_keeps_everything(history_root):
    """A broken database must never make every transcript deletable."""
    _write("dialog", count=1)
    _write("child", count=1, parent="dialog")

    def _boom(tid):
        raise RuntimeError("db is down")

    assert T.find_orphans(is_known=_boom) == []


# ─── the sweep: policy and its safety rules ────────────────────────────────

def test_a_dry_run_removes_nothing(history_root):
    _write("old-dialog", count=1)
    folder = T.thread_dir("old-dialog")
    ancient = time.time() - 400 * 86400
    os.utime(folder / T.TRANSCRIPT_NAME, (ancient, ancient))

    result = T.sweep(is_known=lambda tid: False)

    assert result["dry_run"] is True
    assert result["expired"]
    assert folder.exists() is True


def test_a_quiet_dialog_that_still_has_a_row_is_not_expired(history_root):
    """The user can still open it, so its history is still needed."""
    _write("quiet-dialog", count=1)
    folder = T.thread_dir("quiet-dialog")
    ancient = time.time() - 400 * 86400
    os.utime(folder / T.TRANSCRIPT_NAME, (ancient, ancient))

    result = T.sweep(dry_run=False, is_known=lambda tid: True)

    assert result["expired"] == []
    assert folder.exists() is True


def test_an_expired_transcript_is_really_removed(history_root):
    """The one destructive path, taken only when asked for."""
    _write("dead-dialog", count=1)
    folder = T.thread_dir("dead-dialog")
    ancient = time.time() - 400 * 86400
    os.utime(folder / T.TRANSCRIPT_NAME, (ancient, ancient))

    result = T.sweep(dry_run=False, is_known=lambda tid: False)

    assert [e["thread_id"] for e in result["expired"]] == ["dead-dialog"]
    assert folder.exists() is False


def test_the_sweep_compresses_a_cold_child_segment(history_root):
    """A subagent transcript ages out of reading too, not just the dialog."""
    _write("dlg", count=1)
    _write("kid", count=30, parent="dlg")
    _rotate("kid", parent="dlg")
    segment = T.thread_dir("kid", "dlg") / "transcript.1.jsonl"
    ancient = time.time() - 40 * 86400
    os.utime(segment, (ancient, ancient))

    result = T.sweep(dry_run=False, is_known=lambda tid: True)

    assert len(result["compressed"]) == 1
    assert segment.exists() is False


def test_the_sweep_reports_bytes_before_and_after(history_root):
    """A retention run that cannot be measured cannot be trusted."""
    _write("measured", count=5)

    result = T.sweep(dry_run=True, is_known=lambda tid: True)

    assert result["bytes_before"] > 0
    assert result["bytes_after"] == result["bytes_before"]


def test_orphan_purging_is_off_unless_asked(history_root):
    _write("dlg2", count=1)
    _write("kid2", count=1, parent="dlg2")

    result = T.sweep(dry_run=False, is_known=lambda tid: False)

    assert result["orphans"]
    assert T.thread_dir("kid2", "dlg2").exists() is True
