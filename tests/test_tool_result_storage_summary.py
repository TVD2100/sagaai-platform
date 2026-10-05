# -*- coding: utf-8 -*-
"""
M2 tests: compact persistence of hidden tool_result payloads.

Covers both the pure helper ``summarize_tool_result_for_storage`` and the
DB persistence paths in ``core.threads_devagent`` (append + full save).
A giant tool_result (e.g. a 1.6M-character list_files) must never be stored
raw into devagent.db; it is reduced to its scalar fields plus ``bulk_sizes``.
"""
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import config
from dev_agent.agent_loop import (
    summarize_tool_result_for_storage,
    _TOOL_RESULT_STORAGE_KEEP_LIMIT,
    _TOOL_RESULT_STORAGE_FALLBACK_LIMIT,
    _TOOL_RESULT_STORAGE_FIELD_LIMIT,
    _TOOL_RESULT_INLINE_LIMIT,
    _apply_tool_result_cap,
)


def _tool_result_payload(files_chars: int, pad: int = 0) -> str:
    """Build a realistic giant list_files tool_result JSON string."""
    tr = {
        "ok": True,
        "base": "src",
        "count": 42,
        "files": [{"path": f"src/file_{i}.py", "size_bytes": 123} for i in range(500)],
        "dirs": [{"path": f"src/pkg_{i}"} for i in range(100)],
        "content": "x" * files_chars,
    }
    if pad:
        tr["pad"] = "p" * pad
    return json.dumps({"tool_result": tr}, ensure_ascii=False)


# ─── Pure helper unit tests ───────────────────────────────────────────────────


def test_small_tool_result_returned_unchanged():
    """Results under the keep-limit are stored verbatim."""
    small = _tool_result_payload(200)
    assert len(small) <= _TOOL_RESULT_STORAGE_KEEP_LIMIT
    assert summarize_tool_result_for_storage(small) == small


def test_large_tool_result_compacted_bulk_fields_to_sizes():
    """Bulk fields of an oversized result are replaced by their serialized
    size in ``bulk_sizes``; scalar fields survive."""
    big = _tool_result_payload(_TOOL_RESULT_STORAGE_KEEP_LIMIT + 5_000)
    out = summarize_tool_result_for_storage(big)
    assert len(out) < len(big) // 10
    data = json.loads(out)
    tr = data["tool_result"]
    assert tr["ok"] is True
    assert tr["base"] == "src"
    assert tr["count"] == 42
    bulk = tr["bulk_sizes"]
    assert set(bulk) == {"files", "dirs", "content"}
    assert bulk["content"] == _TOOL_RESULT_STORAGE_KEEP_LIMIT + 5_000 + 2
    assert isinstance(bulk["files"], int)
    assert isinstance(bulk["dirs"], int)
    assert "xx" not in out  # none of the giant payload leaked in


def test_large_scalar_string_truncated_with_full_len():
    """An oversized non-bulk scalar keeps a truncated head + ``_full_len``."""
    tr = {
        "ok": False,
        "error": "E" * (_TOOL_RESULT_STORAGE_FIELD_LIMIT + 700),
        "result_size": 123456,
        "pad": "p" * _TOOL_RESULT_STORAGE_KEEP_LIMIT,
    }
    big = json.dumps({"tool_result": tr}, ensure_ascii=False)
    out = summarize_tool_result_for_storage(big)
    data = json.loads(out)
    etr = data["tool_result"]
    assert etr["ok"] is False
    assert len(etr["error"]) == _TOOL_RESULT_STORAGE_FIELD_LIMIT
    assert etr["error_full_len"] == _TOOL_RESULT_STORAGE_FIELD_LIMIT + 700
    assert etr["result_size"] == 123456
    assert "pad_full_len" in etr  # oversized scalar truncated with _full_len


def test_broken_json_kept_raw_up_to_fallback_limit():
    """Unparseable oversized content is truncated raw to the fallback limit."""
    broken = '{"tool_result"' + "x" * 100_000
    out = summarize_tool_result_for_storage(broken)
    assert len(out) == _TOOL_RESULT_STORAGE_FALLBACK_LIMIT
    assert out.startswith('{"tool_result"')


def test_non_tool_result_text_unchanged():
    """Regular user/assistant messages are never compacted."""
    text = "Обычное сообщение пользователя " + "длинный текст " * 100
    assert summarize_tool_result_for_storage(text) == text


def test_tool_result_list_values_counted():
    """List-shaped values are summarised by element count in ``bulk_sizes``."""
    tr = {"ok": True, "paths": ["a", "b", "c"], "tool": "list_files"}
    over = json.dumps({
        "tool_result": {**tr, "pad": "p" * _TOOL_RESULT_STORAGE_KEEP_LIMIT}
    })
    out = summarize_tool_result_for_storage(over)
    data = json.loads(out)
    assert data["tool_result"]["bulk_sizes"]["paths"] == 3
    assert data["tool_result"]["tool"] == "list_files"


# ─── DB persistence integration tests ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point SagaAI at a fresh temp data dir and reload path-dependent modules."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import storage.db as db_mod
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()

    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)

    yield tmp_path

    db_mod.reset_engine()
    db_mod.reset_devagent_engine()


def test_append_thread_message_compacts_giant_tool_result(tmp_path):
    """append_thread_message must not store a giant tool_result raw in the DB."""
    from core.threads_devagent import (
        create_devagent_thread,
        append_thread_message,
        load_thread_messages,
    )

    tid = create_devagent_thread(title="m2 append", orchestrator_slug="dev_agent")
    giant = _tool_result_payload(_TOOL_RESULT_STORAGE_KEEP_LIMIT + 20_000)
    append_thread_message(tid, "user", giant)

    msgs = load_thread_messages(tid)
    assert len(msgs) == 1
    stored = msgs[0]["content"]
    assert len(stored) < 5_000
    data = json.loads(stored)
    assert "bulk_sizes" in data["tool_result"]
    assert "files" not in data["tool_result"]


def test_save_thread_messages_compacts_giant_tool_result(tmp_path):
    """The full-history save path also compacts giant tool_results."""
    from core.threads_devagent import (
        create_devagent_thread,
        save_thread_messages,
        load_thread_messages,
    )

    tid = create_devagent_thread(title="m2 save", orchestrator_slug="dev_agent")
    giant = _tool_result_payload(_TOOL_RESULT_STORAGE_KEEP_LIMIT + 20_000)
    save_thread_messages(tid, [
        {"role": "user", "content": "задача"},
        {"role": "assistant", "content": "выполняю"},
        {"role": "user", "content": giant},
    ])

    msgs = load_thread_messages(tid)
    assert [m["content"] for m in msgs[:2]] == ["задача", "выполняю"]
    stored = msgs[2]["content"]
    assert len(stored) < 5_000
    data = json.loads(stored)
    assert "bulk_sizes" in data["tool_result"]


def test_small_tool_result_saved_verbatim(tmp_path):
    """Small results still round-trip unmodified through the DB."""
    from core.threads_devagent import (
        create_devagent_thread,
        append_thread_message,
        load_thread_messages,
    )

    tid = create_devagent_thread(title="m2 small", orchestrator_slug="dev_agent")
    small = _tool_result_payload(200)
    append_thread_message(tid, "user", small)
    msgs = load_thread_messages(tid)
    assert msgs[0]["content"] == small


def test_events_survive_compaction(tmp_path):
    """The _events JSON prefix must still be restored after compaction."""
    from core.threads_devagent import (
        create_devagent_thread,
        append_thread_message,
        load_thread_messages,
    )

    tid = create_devagent_thread(title="m2 events", orchestrator_slug="dev_agent")
    giant = _tool_result_payload(_TOOL_RESULT_STORAGE_KEEP_LIMIT + 20_000)
    append_thread_message(tid, "user", giant,
                          events=[{"type": "tool_result", "tool": "list_files"}],
                          tokens={"in": 10, "out": 2, "cache": 0})

    msgs = load_thread_messages(tid)
    assert msgs[0]["_events"] == [{"type": "tool_result", "tool": "list_files"}]
    assert msgs[0]["_tokens"] == {"in": 10, "out": 2, "cache": 0}
    assert "bulk_sizes" in json.loads(msgs[0]["content"])["tool_result"]


# ─── C1: batch splitter and wire == stored ────────────────────────────────────


def test_batch_of_small_documents_saved_verbatim():
    """A newline-joined batch may exceed the keep-limit while every single
    document is small; such a batch is stored byte-verbatim (wire == stored)."""
    docs = [
        json.dumps({"tool_result": {"ok": True, "content": "a" * 3_000}},
                   ensure_ascii=False)
        for _ in range(20)
    ]
    batch = "\n".join(docs)
    assert len(batch) > _TOOL_RESULT_STORAGE_KEEP_LIMIT
    assert summarize_tool_result_for_storage(batch) == batch


def test_batch_compacts_only_oversized_member():
    """In a mixed batch only the oversized document is compacted; the small
    document survives byte-verbatim."""
    small = json.dumps({"tool_result": {"ok": True, "content": "s" * 500}})
    big = _tool_result_payload(_TOOL_RESULT_STORAGE_KEEP_LIMIT + 5_000)
    batch = small + "\n" + big

    out = summarize_tool_result_for_storage(batch)

    parts = out.split("\n")
    assert parts[0] == small
    assert "bulk_sizes" in json.loads(parts[1])["tool_result"]


def test_spilled_preview_and_wire_round_trip_verbatim(tmp_path, monkeypatch):
    """C1: the spilled-result preview the model receives is stored byte-
    verbatim, so a resumed thread rebuilds the identical wire document."""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    payload = "x" * 50_000
    preview = _apply_tool_result_cap(
        {"ok": True, "path": "src/big.txt", "content": payload}, "read_file"
    )
    wire = json.dumps({"tool_result": preview}, ensure_ascii=False)
    assert len(wire) <= _TOOL_RESULT_INLINE_LIMIT
    assert len(wire) <= _TOOL_RESULT_STORAGE_KEEP_LIMIT

    from core.threads_devagent import (
        create_devagent_thread,
        append_thread_message,
        load_thread_messages,
    )

    tid = create_devagent_thread(title="c1 verbatim", orchestrator_slug="dev_agent")
    append_thread_message(tid, "user", wire)
    msgs = load_thread_messages(tid)
    assert msgs[0]["content"] == wire


def test_batch_wire_round_trips_verbatim_through_both_paths(tmp_path, monkeypatch):
    """C2: a batch (spilled preview + small docs) larger than the keep-limit
    but with every document small is stored byte-verbatim on BOTH persist
    paths, so a resumed thread re-sends exactly the live wire bytes."""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    payload = "x" * 50_000
    preview = _apply_tool_result_cap(
        {"ok": True, "path": "src/big.txt", "content": payload}, "read_file"
    )
    doc_a = json.dumps({"tool_result": preview}, ensure_ascii=False)
    doc_b = json.dumps({"tool_result": {"ok": True, "count": 1}}, ensure_ascii=False)
    doc_c = json.dumps(
        {"tool_result": {"ok": True, "content": "s" * 35_000}}, ensure_ascii=False
    )
    wire = "\n".join([doc_a, doc_b, doc_c])
    assert len(wire) > _TOOL_RESULT_STORAGE_KEEP_LIMIT  # splitter path is used
    for doc in (doc_a, doc_b, doc_c):
        assert len(doc) <= _TOOL_RESULT_STORAGE_KEEP_LIMIT

    from core.threads_devagent import (
        create_devagent_thread,
        append_thread_message,
        save_thread_messages,
        load_thread_messages,
    )

    tid_a = create_devagent_thread(title="c2 batch append", orchestrator_slug="dev_agent")
    append_thread_message(tid_a, "user", wire)
    loaded_a = load_thread_messages(tid_a)
    assert loaded_a[0]["content"] == wire

    tid_b = create_devagent_thread(title="c2 batch save", orchestrator_slug="dev_agent")
    save_thread_messages(tid_b, [{"role": "user", "content": wire}])
    loaded_b = load_thread_messages(tid_b)
    assert loaded_b[0]["content"] == wire