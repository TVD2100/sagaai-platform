# -*- coding: utf-8 -*-
"""Tests for the read_document tool of UniversalDevAgent.

Covers the catalog entry, the argument specification, absolute and workspace-
relative path resolution, the line window, single-file mode and the error
paths (missing argument, unknown argument, missing file). A DOCX fixture is
generated on the fly; a missing python-docx skips the document test only.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine  # noqa: E402


@pytest.fixture
def isolated_data_dir():
    """Point SagaAI at a fresh temp data dir and reload the engines."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_readdoc_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {
        attr: getattr(core.paths, attr, None)
        for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                     "SYSTEM_PROMPTS_DIR")
    }
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
    for attr, value in old_values.items():
        if value is not None:
            setattr(core.paths, attr, value)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def workspace(tmp_path, isolated_data_dir):
    """Point the agent at a temporary workspace and restore it afterwards."""
    from dev_agent import config as dagent_config
    old_root = dagent_config.PROJECT_ROOT
    old_target = dagent_config.TARGET_FILE
    dagent_config.PROJECT_ROOT = tmp_path
    dagent_config.TARGET_FILE = None
    yield tmp_path
    dagent_config.PROJECT_ROOT = old_root
    dagent_config.TARGET_FILE = old_target


def _agent():
    from dev_agent.universal_agent import UniversalDevAgent
    return UniversalDevAgent()


def _docx_bytes(paragraphs) -> bytes:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_catalog_advertises_read_document():
    names = {t["name"] for t in _agent().tool_catalog}
    assert "read_document" in names


def test_read_document_argument_spec():
    from dev_agent.universal_agent import WORKSPACE_TOOL_ARGS
    spec = WORKSPACE_TOOL_ARGS["read_document"]
    assert spec["required"] == {"path"}
    assert spec["optional"] == {"offset", "limit", "max_chars"}


def test_missing_path_is_rejected(isolated_data_dir):
    res = _agent().dispatch("read_document", {})
    assert res.get("ok") is False
    assert "path" in res.get("error", "")


def test_unknown_argument_is_rejected(workspace):
    res = _agent().dispatch("read_document", {"path": "a.txt", "file_name": "a.txt"})
    assert res.get("ok") is False
    assert res.get("unknown_args") == ["file_name"]


def test_absolute_path_returns_text(workspace):
    path = workspace / "notes.txt"
    path.write_text("строка 1\nстрока 2\n", encoding="utf-8")

    res = _agent().dispatch("read_document", {"path": str(path)})

    assert res.get("ok") is True
    assert "строка 1" in res["text"]
    assert res["requested_path"] == str(path)


def test_relative_path_resolves_against_workspace(workspace):
    (workspace / "docs").mkdir()
    (workspace / "docs" / "plan.md").write_text("# План", encoding="utf-8")

    res = _agent().dispatch("read_document", {"path": "docs/plan.md"})

    assert res.get("ok") is True
    assert "# План" in res["text"]
    assert res["path"].endswith(os.path.join("docs", "plan.md"))


def test_line_window_is_applied(workspace):
    path = workspace / "lines.txt"
    path.write_text("\n".join("line %d" % i for i in range(1, 11)), encoding="utf-8")

    res = _agent().dispatch(
        "read_document", {"path": str(path), "offset": "2", "limit": "3"},
    )

    assert res["offset"] == 2
    assert res["text"].split("\n") == ["line 3", "line 4", "line 5"]
    assert res["remaining"] == 5


def test_max_chars_truncates_the_window(workspace):
    path = workspace / "wide.txt"
    path.write_text("a" * 30 + "\n" + "b" * 30, encoding="utf-8")

    res = _agent().dispatch("read_document", {"path": str(path), "max_chars": 35})

    assert res["text"] == "a" * 30
    assert any("window cut" in w for w in res["warnings"])


def test_non_integer_window_rejected(workspace):
    path = workspace / "notes.txt"
    path.write_text("text", encoding="utf-8")
    res = _agent().dispatch("read_document", {"path": str(path), "offset": "abc"})
    assert res.get("ok") is False
    assert "offset" in res.get("error", "")


def test_missing_file_returns_error(workspace):
    res = _agent().dispatch("read_document", {"path": str(workspace / "nope.docx")})
    assert res.get("ok") is False
    assert "not found" in res.get("error", "")


def test_docx_is_extracted_without_run_code(workspace):
    path = workspace / "program.docx"
    path.write_bytes(_docx_bytes(["Балльно-рейтинговая система", "Модуль 1"]))

    res = _agent().dispatch("read_document", {"path": str(path)})

    assert res.get("ok") is True
    assert res["format"] == ".docx"
    assert "Балльно-рейтинговая система" in res["text"]
    assert "Модуль 1" in res["text"]


def test_single_file_mode_blocks_other_files(workspace):
    from dev_agent import config as dagent_config
    allowed = workspace / "target.py"
    allowed.write_text("print('ok')", encoding="utf-8")
    other = workspace / "other.txt"
    other.write_text("secret", encoding="utf-8")
    dagent_config.TARGET_FILE = str(allowed)

    blocked = _agent().dispatch("read_document", {"path": str(other)})
    allowed_res = _agent().dispatch("read_document", {"path": str(allowed)})

    assert blocked.get("ok") is False
    assert "Single-file mode" in blocked.get("error", "")
    assert allowed_res.get("ok") is True
    assert "print" in allowed_res["text"]


def test_unsupported_format_returns_a_warning(workspace):
    path = workspace / "image.bin"
    path.write_bytes(b"\x00\x01\x02")
    res = _agent().dispatch("read_document", {"path": str(path)})
    assert res.get("ok") is True
    assert res["text"] == ""
    assert any("unsupported" in w for w in res["warnings"])
