"""
Regression tests for the search_in_files workspace tool.

Covers:
  - literal and regex search
  - case sensitivity
  - subdir scoping and extensions filtering
  - non-TEXT_EXTENSIONS files (e.g. .csv) when extensions is provided
  - non-UTF-8 (cp1251) text files
  - large-file skipping (MAX_FILE_SIZE_BYTES)
  - validation errors and empty results
  - context_before / context_after windows
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent import config as dev_config
from dev_agent import workspace_tools as wt


@pytest.fixture
def ws(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    (folder / "app.py").write_text(
        "import helper\n\ndef main():\n    return helper.run()\n", encoding="utf-8"
    )
    (folder / "notes.md").write_text(
        "# Notes\n\nTODO: fix search\n", encoding="utf-8"
    )
    (folder / "data.csv").write_text(
        "name,value\nTODO,1\n", encoding="utf-8"
    )
    (folder / "legacy.txt").write_bytes(
        "Привет TODO мир".encode("cp1251")
    )
    res = wt.set_workspace(str(folder))
    assert res["ok"]
    yield folder
    dev_config.set_target_root(dev_config.INSTALL_ROOT)


def test_literal_search(ws):
    res = wt.search_in_files("TODO")
    assert res["ok"] is True
    # "TODO" appears in notes.md AND in the cp1251 legacy.txt (decoding fix).
    paths = {r["path"] for r in res["results"]}
    assert "notes.md" in paths
    assert "legacy.txt" in paths
    assert "data.csv" not in paths  # .csv not scanned without extensions


def test_regex_search(ws):
    res = wt.search_in_files(r"def (main|run)\(", regex=True)
    assert res["ok"] is True
    assert res["match_count"] >= 1
    assert any(r["path"] == "app.py" for r in res["results"])


def test_case_sensitive(ws):
    res = wt.search_in_files("todo", case_sensitive=True)
    assert res["ok"] is True
    assert res["match_count"] == 0


def test_subdir_scoping(ws):
    (ws / "sub").mkdir(exist_ok=True)
    (ws / "sub" / "inner.txt").write_text("needle\n", encoding="utf-8")
    res = wt.search_in_files("needle", subdir="sub")
    assert res["ok"] is True
    assert res["match_count"] == 1
    assert res["results"][0]["path"] == "sub/inner.txt"


# --- path argument (single-file / directory targeting) ---------------------

def test_path_file_targets_single_file(ws):
    """path pointing to a FILE scans exactly that file, ignoring extensions."""
    # data.csv is outside TEXT_EXTENSIONS and contains TODO.
    res = wt.search_in_files("TODO", path="data.csv")
    assert res["ok"] is True
    assert [r["path"] for r in res["results"]] == ["data.csv"]
    assert res["match_count"] == 1


def test_path_directory_acts_like_subdir(ws):
    """path pointing to a DIRECTORY scopes the scan like subdir."""
    (ws / "sub").mkdir(exist_ok=True)
    (ws / "sub" / "inner.txt").write_text("needle\n", encoding="utf-8")
    res = wt.search_in_files("needle", path="sub")
    assert res["ok"] is True
    assert res["match_count"] == 1
    assert res["results"][0]["path"] == "sub/inner.txt"


def test_path_takes_precedence_over_subdir(ws):
    """When both path and subdir are given, path wins."""
    (ws / "sub").mkdir(exist_ok=True)
    (ws / "sub" / "inner.txt").write_text("needle\n", encoding="utf-8")
    res = wt.search_in_files("needle", path="sub/inner.txt")
    assert res["ok"] is True
    assert res["match_count"] == 1


def test_path_missing_rejected(ws):
    res = wt.search_in_files("x", path="nope.txt")
    assert res["ok"] is False
    assert "not found" in res["error"].lower()


def test_path_escape_rejected(ws):
    res = wt.search_in_files("x", path="../outside.txt")
    assert res["ok"] is False
    assert "escapes" in res["error"].lower()


def test_extensions_as_string(ws):
    res = wt.search_in_files("TODO", extensions="md")
    assert res["ok"] is True
    paths = {r["path"] for r in res["results"]}
    assert paths == {"notes.md"}


def test_extensions_as_list(ws):
    res = wt.search_in_files("TODO", extensions=["py"])
    assert res["ok"] is True
    paths = {r["path"] for r in res["results"]}
    assert "app.py" not in paths
    assert res["match_count"] == 0


def test_csv_file_searchable_with_extensions(ws):
    # .csv is not in TEXT_EXTENSIONS; passing extensions=["csv"] must search it.
    res = wt.search_in_files("TODO", extensions=["csv"])
    assert res["ok"] is True
    assert res["match_count"] == 1
    assert res["results"][0]["path"] == "data.csv"


def test_legacy_encoding_file_searchable(ws):
    # cp1251 text should be decoded and searched.
    res = wt.search_in_files("Привет")
    assert res["ok"] is True
    assert any(r["path"] == "legacy.txt" for r in res["results"])


def test_empty_query_rejected(ws):
    res = wt.search_in_files("")
    assert res["ok"] is False


def test_invalid_regex_rejected(ws):
    res = wt.search_in_files("(", regex=True)
    assert res["ok"] is False


def test_missing_subdir_rejected(ws):
    res = wt.search_in_files("x", subdir="nope")
    assert res["ok"] is False


def test_no_matches_returns_ok(ws):
    res = wt.search_in_files("does-not-exist-anywhere")
    assert res["ok"] is True
    assert res["match_count"] == 0


def test_max_results_truncates(ws):
    for i in range(10):
        (ws / f"f{i}.txt").write_text("common\n", encoding="utf-8")
    res = wt.search_in_files("common", max_results=3)
    assert res["ok"] is True
    assert len(res["results"]) == 3
    assert res["truncated"] is True


def test_large_file_skipped_with_counter(ws, monkeypatch):
    # Temporarily lower the max file size so the .md file is "large".
    monkeypatch.setattr(dev_config, "MAX_FILE_SIZE_BYTES", 10)
    res = wt.search_in_files("TODO")
    assert res["ok"] is True
    # notes.md is >10 bytes and should be skipped, not read.
    assert res.get("files_skipped_large", 0) >= 1


# ─── context_before / context_after windows ──────────────────────────────

def test_context_windows_present(ws):
    """context_before/context_after > 0 attach surrounding trimmed lines."""
    # notes.md lines: ["# Notes", "", "TODO: fix search"]
    res = wt.search_in_files("TODO", extensions=["md"],
                             context_before=2, context_after=1)
    assert res["ok"] is True and res["match_count"] == 1
    hit = res["results"][0]
    assert hit["path"] == "notes.md"
    assert hit["before"] == ["# Notes", ""]
    assert hit["after"] == []


def test_context_default_absent(ws):
    """Without context args results must NOT carry before/after keys."""
    res = wt.search_in_files("TODO")
    assert res["ok"] is True
    for r in res["results"]:
        assert "before" not in r and "after" not in r


def test_context_invalid_values_coerced_to_zero(ws):
    """Non-int context values fall back to 0 (no keys attached)."""
    res = wt.search_in_files("TODO", context_before="abc", context_after=None)
    assert res["ok"] is True
    for r in res["results"]:
        assert "before" not in r and "after" not in r


def test_context_lines_trimmed(ws):
    """before/after lines are trimmed to CONTEXT_LINE_CHARS (120)."""
    (ws / "wide.txt").write_text(
        "x" * 300 + "\nneedle\n" + "y" * 300 + "\n", encoding="utf-8")
    res = wt.search_in_files("needle", extensions=["txt"],
                             context_before=1, context_after=1)
    assert res["ok"] is True
    hit = res["results"][0]
    assert hit["before"] == ["x" * 120]
    assert hit["after"] == ["y" * 120]


def test_context_multiline_window(ws):
    """Multi-line before/after windows collect the requested number of lines."""
    (ws / "multiline.txt").write_text(
        "l1\nl2\nneedle\nl4\nl5\n", encoding="utf-8")
    res = wt.search_in_files("needle", extensions=["txt"],
                             context_before=2, context_after=2)
    assert res["ok"] is True
    hit = res["results"][0]
    assert hit["before"] == ["l1", "l2"]
    assert hit["after"] == ["l4", "l5"]


# ─── files argument (explicit file list) ─────────────────────────────────

def test_files_scans_only_listed_files(ws):
    """files=[...] returns matches only from the listed files."""
    res = wt.search_in_files("TODO", files=["notes.md", "data.csv"])
    assert res["ok"] is True
    paths = {r["path"] for r in res["results"]}
    assert paths == {"notes.md", "data.csv"}
    # legacy.txt also contains TODO but is not listed -> excluded.
    assert "legacy.txt" not in paths


def test_files_ignores_extension_filter(ws):
    """Extension filtering does not apply to explicitly listed files."""
    # data.csv is outside TEXT_EXTENSIONS; being listed makes it searchable.
    res = wt.search_in_files("TODO", files=["data.csv"])
    assert res["ok"] is True
    assert [r["path"] for r in res["results"]] == ["data.csv"]


def test_files_missing_file_rejected(ws):
    res = wt.search_in_files("x", files=["nope.txt"])
    assert res["ok"] is False
    assert "not found" in res["error"].lower()


def test_files_directory_rejected(ws):
    (ws / "sub").mkdir(exist_ok=True)
    res = wt.search_in_files("x", files=["sub"])
    assert res["ok"] is False
    assert "not a file" in res["error"].lower()


def test_files_escape_rejected(ws):
    res = wt.search_in_files("x", files=["../outside.txt"])
    assert res["ok"] is False
    assert "escapes" in res["error"].lower()


def test_files_takes_precedence_over_path_and_subdir(ws):
    """When files is given, path/subdir are ignored entirely."""
    res = wt.search_in_files("TODO", files=["notes.md"], path="data.csv", subdir="nope")
    assert res["ok"] is True
    assert [r["path"] for r in res["results"]] == ["notes.md"]


def test_files_string_coerced_to_list(ws):
    res = wt.search_in_files("TODO", files="notes.md")
    assert res["ok"] is True
    assert [r["path"] for r in res["results"]] == ["notes.md"]


def test_files_empty_list_rejected(ws):
    res = wt.search_in_files("x", files=[])
    assert res["ok"] is False
    assert "non-empty" in res["error"]


def test_files_non_string_entry_rejected(ws):
    res = wt.search_in_files("x", files=["notes.md", 42])
    assert res["ok"] is False
    assert "non-empty file paths" in res["error"]


def test_files_duplicates_do_not_crash(ws):
    """Listing the same file twice must not crash; duplicates are acceptable."""
    res = wt.search_in_files("TODO", files=["notes.md", "notes.md"])
    assert res["ok"] is True
    assert res["match_count"] >= 1
