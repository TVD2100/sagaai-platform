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

from dev_agent.agent_loop import (
    summarize_tool_result_for_storage,
    _TOOL_RESULT_STORAGE_KEEP_LIMIT,
    _TOOL_RESULT_STORAGE_FALLBACK_LIMIT,
    _TOOL_RESULT_STORAGE_FIELD_LIMIT,
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
