# -*- coding: utf-8 -*-
"""
Tests for the dialog-upload helpers in core.threads_devagent:

- save_thread_file_data stores RAW BYTES into history/<tid>/files under the
  original file name (no content parsing by the platform);
- list_thread_files reports files with size/text/binary probe info;
- read_thread_file reads text as utf-8/cp1251 with an offset/limit window,
  reports binary files as is_text=False, and rejects traversal/garbage names
  without escaping the thread folder.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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


EXPECTED_TID = "20260101_000000_test01"


def test_save_thread_file_data_writes_history_files_dir(tmp_path):
    """Raw bytes land in DATA_DIR/history/<tid>/files under the original name."""
    from core.threads_devagent import save_thread_file_data
    from core.paths import get_thread_dir

    saved = save_thread_file_data(EXPECTED_TID, "Отчёт.csv", "a;b\n" .encode())
    expected_dir = os.path.join(get_thread_dir(EXPECTED_TID), "files")
    assert saved == os.path.join(expected_dir, "Отчёт.csv")
    with open(saved, "rb") as fh:
        assert fh.read() == "a;b\n".encode()


def test_save_thread_file_data_keeps_binary_payload_verbatim(tmp_path):
    """Binary content (e.g. zip/jpg bytes) is stored unchanged - the platform
    never parses uploads."""
    from core.threads_devagent import save_thread_file_data

    blob = b"PK\x03\x04" + bytes(range(256)) * 4
    saved = save_thread_file_data(EXPECTED_TID, "archive.zip", blob)
    with open(saved, "rb") as fh:
        assert fh.read() == blob


def test_save_thread_file_data_rejects_empty_and_bad_names(tmp_path):
    """Traversal-style and empty names must be rejected/neutralized."""
    from core.threads_devagent import save_thread_file_data

    with pytest.raises(ValueError):
        save_thread_file_data(EXPECTED_TID, "", b"x")
    with pytest.raises(ValueError):
        save_thread_file_data(EXPECTED_TID, "..", b"x")
    # Directory components are stripped to a plain basename.
    saved = save_thread_file_data(EXPECTED_TID, "../../evil.txt", b"ok")
    assert os.path.basename(saved) == "evil.txt"
    assert ".." not in saved.split("files")[-1]


def test_save_thread_file_data_rejects_oversize(tmp_path, monkeypatch):
    """Files above the dialog size cap are refused without touching disk."""
    from core.threads_devagent import save_thread_file_data
    monkeypatch.setattr("core.threads_devagent.MAX_THREAD_FILE_BYTES", 10)

    with pytest.raises(ValueError):
        save_thread_file_data(EXPECTED_TID, "big.bin", b"x" * 11)


def test_list_thread_files_sorted_with_text_probe(tmp_path):
    """Listing returns sorted records with size and a short text probe."""
    from core.threads_devagent import save_thread_file_data, list_thread_files

    save_thread_file_data(EXPECTED_TID, "b.txt", "line1\nline2".encode())
    save_thread_file_data(EXPECTED_TID, "a.txt", "one".encode())

    records = list_thread_files(EXPECTED_TID)
    assert [r["name"] for r in records] == ["a.txt", "b.txt"]
    assert records[0]["bytes"] == 3
    assert records[0]["is_text"] is True
    assert records[0]["probe"] == "one"
    assert all(r["path"].startswith(os.sep) for r in records)


def test_list_thread_files_marks_binary_without_crash(tmp_path):
    """A non-decodable binary payload is reported as is_text=False."""
    from core.threads_devagent import save_thread_file_data, list_thread_files

    save_thread_file_data(EXPECTED_TID, "img.bin", b"\xf0\x9f\x00\xff")
    records = list_thread_files(EXPECTED_TID)
    assert len(records) == 1
    assert records[0]["name"] == "img.bin"
    assert records[0]["is_text"] is False
    assert records[0]["probe"] == ""


def test_list_thread_files_missing_thread_returns_empty(tmp_path):
    """A thread with no uploads produces an empty list, not an error."""
    from core.threads_devagent import list_thread_files
    assert list_thread_files("no_such_thread_123") == []


def test_read_thread_file_full_text(tmp_path):
    """A UTF-8 text file is returned whole with metadata."""
    from core.threads_devagent import save_thread_file_data, read_thread_file

    save_thread_file_data(EXPECTED_TID, "notes.txt", "alpha\nbeta\n".encode())
    res = read_thread_file(EXPECTED_TID, "notes.txt")
    assert res["ok"] is True
    assert res["content"] == "alpha\nbeta\n"
    assert res["is_text"] is True
    assert res["decoded_as"] == "utf-8"
    assert res["total_lines"] == 3
    assert res["remaining"] == 0


def test_read_thread_file_cp1251_fallback(tmp_path):
    """Legacy cp1251 text is decoded via the fallback."""
    from core.threads_devagent import save_thread_file_data, read_thread_file

    cp1251 = "Привет мир".encode("cp1251")
    save_thread_file_data(EXPECTED_TID, "legacy.txt", cp1251)
    res = read_thread_file(EXPECTED_TID, "legacy.txt")
    assert res["ok"] is True
    assert res["content"] == "Привет мир"
    assert res["decoded_as"] == "cp1251"


def test_read_thread_file_offset_limit_window(tmp_path):
    """offset/limit return a line window and report remaining lines."""
    from core.threads_devagent import save_thread_file_data, read_thread_file

    save_thread_file_data(EXPECTED_TID, "lines.txt", "1\n2\n3\n4\n5".encode())
    res = read_thread_file(EXPECTED_TID, "lines.txt", offset=1, limit=2)
    assert res["content"] == "2\n3"
    assert res["offset"] == 1
    assert res["limit"] == 2
    assert res["total_lines"] == 5
    assert res["remaining"] == 2


def test_read_thread_file_binary_reports_hint(tmp_path):
    """Binary files: ok=True, empty content, is_text=False and a run_code hint."""
    from core.threads_devagent import save_thread_file_data, read_thread_file

    save_thread_file_data(EXPECTED_TID, "photo.jpg", b"\xff\xd8\xff\xe0")
    res = read_thread_file(EXPECTED_TID, "photo.jpg")
    assert res["ok"] is True
    assert res["is_text"] is False
    assert res["content"] == ""
    assert "run_code" in res["hint"]


def test_read_thread_file_missing_returns_error(tmp_path):
    """A missing upload yields a clean structured error."""
    from core.threads_devagent import read_thread_file

    res = read_thread_file(EXPECTED_TID, "gone.txt")
    assert res["ok"] is False
    assert "not found" in res["error"]


def test_read_thread_file_traversal_cannot_escape(tmp_path):
    """A name with directory components stays inside the thread files dir."""
    from core.threads_devagent import save_thread_file_data, read_thread_file

    save_thread_file_data(EXPECTED_TID, "secret.txt", "top".encode())
    res = read_thread_file(EXPECTED_TID, "../../secret.txt")
    assert res["ok"] is True
    assert res["content"] == "top"
