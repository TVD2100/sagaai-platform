"""Tests for core.files helpers: upload token limit + two-mode attachments."""
from unittest.mock import patch

from core.files import (
    check_upload_tokens, MAX_UPLOAD_TOKENS, estimate_tokens,
    MAX_INLINE_UPLOAD_CHARS, should_store_uploaded_file,
    build_attachment_metadata, build_attachments_context,
    build_saved_files_registry,
)


def test_max_upload_tokens_constant():
    assert MAX_UPLOAD_TOKENS == 500_000


def test_check_upload_tokens_within_limit():
    ok, tokens = check_upload_tokens("short text")
    assert ok is True
    assert tokens >= 1


def test_check_upload_tokens_over_limit():
    with patch("core.files.estimate_tokens", return_value=500_001):
        ok, tokens = check_upload_tokens("some text")
    assert ok is False
    assert tokens == 500_001


def test_check_upload_tokens_at_limit():
    with patch("core.files.estimate_tokens", return_value=500_000):
        ok, tokens = check_upload_tokens("some text")
    assert ok is True
    assert tokens == 500_000


def test_check_upload_tokens_custom_limit():
    with patch("core.files.estimate_tokens", return_value=6):
        ok, tokens = check_upload_tokens("some text", max_tokens=5)
    assert ok is False
    assert tokens == 6


def test_estimate_tokens_returns_positive_int():
    assert isinstance(estimate_tokens("hello"), int)
    assert estimate_tokens("") >= 1


def test_should_store_uploaded_file_threshold():
    assert should_store_uploaded_file("x" * MAX_INLINE_UPLOAD_CHARS) is False
    assert should_store_uploaded_file("x" * (MAX_INLINE_UPLOAD_CHARS + 1)) is True
    assert should_store_uploaded_file("") is False


def test_build_attachment_metadata_small_file():
    meta = build_attachment_metadata("note.txt", "hello")
    assert meta["name"] == "note.txt"
    assert meta["content"] == "hello"
    assert meta["stored"] is False
    assert meta["path"] == ""
    assert meta["chars"] == 5
    assert meta["tokens"] >= 1
    assert meta["preview"] == "hello"


def test_build_attachment_metadata_large_file():
    big = "x" * (MAX_INLINE_UPLOAD_CHARS + 100)
    meta = build_attachment_metadata("big.txt", big, preview_chars=500)
    assert meta["stored"] is True
    assert len(meta["preview"]) == 500
    assert meta["chars"] == len(big)


def test_build_attachments_context_mixed():
    small = build_attachment_metadata("small.txt", "hello world")
    big_content = ("long " * 20_000) + "UNIQUE_TAIL_MARKER"
    big = build_attachment_metadata(
        "big.txt", big_content, preview_chars=200
    )
    big["path"] = "/tmp/thread_xyz/files/big.txt.txt"
    ctx, names = build_attachments_context([small, big])
    assert names == "small.txt, big.txt"
    assert "hello world" in ctx
    assert "/tmp/thread_xyz/files/big.txt.txt" in ctx
    # only the preview is exposed; the tail of the full content must NOT
    # appear anywhere in the context
    assert "UNIQUE_TAIL_MARKER" in big_content
    assert "UNIQUE_TAIL_MARKER" not in ctx
    assert "preview (first 200 chars)" in ctx


def test_build_attachments_context_empty():
    ctx, names = build_attachments_context([])
    assert ctx == ""
    assert names == ""


def test_build_saved_files_registry_empty():
    assert build_saved_files_registry([]) == ""
    assert build_saved_files_registry(None) == ""


def test_build_saved_files_registry_entries():
    entries = [
        {"name": "a.txt", "path": ".dev_agent/attachments/t1/a.txt",
         "chars": 100, "tokens": 30},
        {"name": "b.md", "path": ".dev_agent/attachments/t1/b.md",
         "chars": 5000, "tokens": 1200},
    ]
    reg = build_saved_files_registry(entries)
    assert "Сохранённые файлы диалога:" in reg
    assert "a.txt (.dev_agent/attachments/t1/a.txt, 100 chars, ~30 tokens)" in reg
    assert "b.md (.dev_agent/attachments/t1/b.md, 5000 chars, ~1200 tokens)" in reg
