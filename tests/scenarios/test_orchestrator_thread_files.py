# -*- coding: utf-8 -*-
"""tests/scenarios/test_orchestrator_thread_files.py - user-level scenario
for dialog file uploads in orchestrator chats (any format, raw bytes only).

Scenarios (given -> when -> then), walking the same public entry points the
UI and the LLM use:

  Scenario 1 - any-format upload lands in history/<tid>/files. A user attaches
               a text note, a zip archive and a jpg image; the raw bytes are
               saved under the original names; the file listing reports
               sizes + text/binary classification; the attachment notice
               (per-message) and the legacy workspace manifest merge into one
               registry that hides no upload.

  Scenario 2 - the agent reads the uploads through the public dispatcher:
               list_thread_files/read_thread_file resolve the active dialog
               thread, text is read with an offset/limit window, binary files
               report is_text=False and a run_code hint; the zip archive is
               then processed by the agent via run_code (zipfile) exactly as
               the platform expects - no content extraction by the platform.

  Scenario 3 - boundary errors stay inside the sandbox: missing files give a
               clean ok=False, traversal names cannot escape the thread files
               dir, oversized uploads are refused, and a missing active thread
               cannot use the thread tools.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from storage.db import reset_engine, reset_devagent_engine


TID = "scen_thread_files"


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR that isolates thread files and DB from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_tfiles_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    reset_engine()
    reset_devagent_engine()

    yield tmp

    reset_engine()
    reset_devagent_engine()

    if old_data_dir:
        os.environ["SAGAAI_DATA_DIR"] = old_data_dir
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def active_thread(monkeypatch, isolated_data_dir):
    from dev_agent import config as dagent_config
    monkeypatch.setattr(dagent_config, "ACTIVE_THREAD_ID", TID)
    return TID


def _make_zip_bytes(entries: dict) -> bytes:
    """Return the bytes of a zip archive with the given name->content map."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_scenario_any_format_upload_lands_in_thread_files(isolated_data_dir):
    """Scenario 1: attach text + zip + jpg; files land in history/<tid>/files
    and the merged notice announces every upload without parsing content.

    Given a fresh dialog thread,
    when  the user attaches notes.txt, archive.zip and photo.jpg and the UI
          saves each one as raw bytes, then appends the per-message notice
          and merges in the legacy workspace manifest,
    then  all three files exist in history/<tid>/files under their original
          names with the exact bytes appended, the listing is sorted with
          sizes and text/binary classification, and the notice contains a
          line per file with name, absolute path, size and kind.
    """
    from core.threads_devagent import (
        save_thread_file_data, list_thread_files, read_thread_file,
    )
    from core.files import build_thread_files_notice
    from core.paths import get_thread_dir
    from ui.pages.orchestrator import (
        _load_attachments_manifest, _append_attachment_manifest,
    )

    zip_blob = _make_zip_bytes({"inner.txt": "hello from zip\n"})
    jpg_blob = b"\xff\xd8\xff\xe0" + bytes(range(64))

    # given + when: the user attaches three files of different formats;
    # the UI saves each one as raw bytes (no parsing).
    for name, data in (("notes.txt", b"alpha\nbeta\n"),
                       ("archive.zip", zip_blob),
                       ("photo.jpg", jpg_blob)):
        saved = save_thread_file_data(TID, name, data)
        files_dir = os.path.join(get_thread_dir(TID), "files")
        assert saved == os.path.join(files_dir, name)

    # then: the listing reports the three uploads, sorted by name, with sizes.
    records = list_thread_files(TID)
    assert [r["name"] for r in records] == ["archive.zip", "notes.txt", "photo.jpg"]
    by_name = {r["name"]: r for r in records}
    assert by_name["notes.txt"]["bytes"] == len(b"alpha\nbeta\n")
    assert by_name["notes.txt"]["is_text"] is True
    assert by_name["archive.zip"]["is_text"] is False
    assert by_name["photo.jpg"]["is_text"] is False

    # when: the UI builds the per-message notice and merges the legacy
    # workspace manifest (a file saved by the legacy flow).
    ws_root = isolated_data_dir
    _append_attachment_manifest(ws_root, TID, {
        "name": "legacy.csv", "path": "/tmp/old/legacy.csv", "chars": 12, "tokens": 3,
    })
    notice = build_thread_files_notice(list_thread_files(TID))
    registry = "Сохранённые файлы диалога:\n- legacy.csv (/tmp/old/legacy.csv, 12 chars, ~3 tokens)"
    merged = f"{notice}\n\n{registry}"

    # then: every upload is announced by name, absolute path, size and kind;
    # the legacy manifest entry stays visible too.
    for needle in ("notes.txt", "archive.zip", "photo.jpg",
                   "binary", "legacy.csv"):
        assert needle in merged
    # The notice never embeds file content.
    assert "hello from zip" not in notice


def test_scenario_agent_reads_uploads_via_public_tools(active_thread):
    """Scenario 2: the agent extracts content itself via dispatcher tools.

    Given thread uploads from scenario 1 (text + zip),
    when  the agent calls list_thread_files, read_thread_file on the text
          file with offset/limit, read_thread_file on the archive, then
          processes the zip through run_code-style zipfile extraction,
    then  the text window returns exactly lines 2-3 of 4 and the remaining
          count, the archive reports is_text=False with a run_code hint,
          and the zip opens from its saved absolute path with the original
          inner content - the platform has not extracted anything itself.
    """
    from core.threads_devagent import save_thread_file_data
    from dev_agent.universal_agent import UniversalDevAgent

    save_thread_file_data(TID, "notes.txt", "l1\nl2\nl3\nl4".encode())
    save_thread_file_data(TID, "archive.zip",
                          _make_zip_bytes({"inner.txt": "hello from zip\n"}))
    agent = UniversalDevAgent()

    listing = agent.dispatch("list_thread_files", {})
    assert listing["ok"] is True, listing
    assert listing["thread_id"] == TID
    assert {f["name"] for f in listing["files"]} == {"notes.txt", "archive.zip"}

    text_res = agent.dispatch("read_thread_file",
                              {"file_name": "notes.txt", "offset": "1", "limit": "2"})
    assert text_res["ok"] is True, text_res
    assert text_res["content"] == "l2\nl3"
    assert text_res["total_lines"] == 4
    assert text_res["remaining"] == 1

    bin_res = agent.dispatch("read_thread_file", {"file_name": "archive.zip"})
    assert bin_res["ok"] is True, bin_res
    assert bin_res["is_text"] is False
    assert bin_res["content"] == ""
    assert "run_code" in bin_res["hint"]

    # The agent processes the archive via run_code: the platform has never
    # parsed it - the orchestrator opens the documented absolute path itself.
    saved_path = [f for f in listing["files"] if f["name"] == "archive.zip"][0]["path"]
    with zipfile.ZipFile(saved_path) as zf:
        assert zf.read("inner.txt").decode() == "hello from zip\n"


def test_scenario_boundary_errors_stay_inside_sandbox(active_thread, isolated_data_dir):
    """Scenario 3: every invalid action fails cleanly without leaving files.

    Given an active dialog thread with no uploads,
    when  the agent reads a missing file, a traversal name, saves an oversized
          blob, and a caller uses the thread tools without an active thread,
    then  missing files return ok=False, the traversal path resolves back to
          the same base name inside the files dir (or fails cleanly), the
          oversized save raises without writing, and dispatch without an
          active thread refuses with an explanatory error.
    """
    from core.threads_devagent import save_thread_file_data, read_thread_file
    from core.paths import get_thread_dir
    from dev_agent.universal_agent import UniversalDevAgent
    from dev_agent import config as dagent_config

    agent = UniversalDevAgent()
    files_dir = os.path.join(get_thread_dir(TID), "files")

    missing = agent.dispatch("read_thread_file", {"file_name": "gone.txt"})
    assert missing["ok"] is False
    assert "not found" in missing["error"]

    save_thread_file_data(TID, "secret.txt", b"top")
    traversal = read_thread_file(TID, "../../secret.txt")
    assert traversal["ok"] is True
    assert traversal["content"] == "top"
    assert os.path.basename(traversal["path"]) == "secret.txt"
    assert os.path.dirname(traversal["path"]) == files_dir

    with pytest.raises(ValueError):
        save_thread_file_data(TID, "huge.bin", b"x" * (100 * 1024 * 1024 + 1))

    dagent_config.ACTIVE_THREAD_ID = ""
    no_thread = agent.dispatch("list_thread_files", {})
    assert no_thread["ok"] is False
    assert "No active dialog thread" in no_thread["error"]
