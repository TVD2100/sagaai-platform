"""Scenario-based tests for ToolExecutor.propose_file.

These tests focus on the reported production issue: propose_file sometimes
writes files that claim success (verified=True) but the on-disk content is
corrupted or missing, forcing the orchestrator to create the file via run_code.

The biggest suspect is _strip_line_numbers(), which runs on EVERY propose_file
call. Its regex r'^\s*\d+\|' matches any line that starts with optional
whitespace, digits, then a pipe -- including legitimate file content such as
markdown tables, numbered lists, log excerpts, or docstrings. When it strips
those prefixes, the on-disk content differs from what the LLM intended, yet
post-write verification compares against the already-stripped text and passes.

Other tested dimensions: Unicode/emoji, escape sequences, CRLF, empty files,
nested directories, JSON/Markdown/py content, large files, and rewrites.
"""

import json
import pytest

from dev_agent import config
from dev_agent.tool_executor import ToolExecutor


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect DevAgent state into a temp sandbox (same pattern as test_propose_file.py)."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(config, "PROTECTED_FILES", ())
    monkeypatch.setattr(config, "WORKING_ON_INSTALL", False)
    config.ensure_runtime_dirs()
    return root


def _read(rel, root):
    return (root / rel).read_text(encoding="utf-8")


# --- 1. Basic scenarios ---

def test_basic_create_and_readback(sandbox):
    te = ToolExecutor()
    content = "hello world\n"
    res = te.propose_file(path="hello.txt", content=content)
    assert res["ok"], res
    assert res["applied"] is True
    assert res["verified"] is True
    assert _read("hello.txt", sandbox) == content


def test_basic_overwrite_preserves_exact_content(sandbox):
    (sandbox / "data.txt").write_text("old\n", encoding="utf-8")
    te = ToolExecutor()
    content = "new line 1\nnew line 2\n"
    res = te.propose_file(path="data.txt", content=content)
    assert res["ok"], res
    assert _read("data.txt", sandbox) == content


def test_rewrite_twice(sandbox):
    te = ToolExecutor()
    c1 = "v1\n"
    c2 = "v2 with more text\n"
    assert te.propose_file(path="twice.txt", content=c1)["ok"]
    assert _read("twice.txt", sandbox) == c1
    assert te.propose_file(path="twice.txt", content=c2)["ok"]
    assert _read("twice.txt", sandbox) == c2


def test_empty_file(sandbox):
    te = ToolExecutor()
    content = ""
    res = te.propose_file(path="empty.txt", content=content)
    assert res["ok"], res
    assert _read("empty.txt", sandbox) == ""


def test_single_line_no_trailing_newline(sandbox):
    te = ToolExecutor()
    content = "just one line without newline"
    res = te.propose_file(path="single.txt", content=content)
    assert res["ok"], res
    assert _read("single.txt", sandbox) == content


# --- 2. Nested directories ---

def test_create_file_in_nested_directories(sandbox):
    te = ToolExecutor()
    content = "deep\n"
    res = te.propose_file(path="a/b/c/deep.txt", content=content)
    assert res["ok"], res
    assert res["is_new"] is True
    assert _read("a/b/c/deep.txt", sandbox) == content


# --- 3. Special characters / Unicode / escaping ---

def test_unicode_and_emoji_preserved(sandbox):
    te = ToolExecutor()
    content = (
        "\u041f\u0440\u0438\u0432\u0435\u0442, \u043c\u0438\u0440!\n"
        "\u65e5\u672c\u8a9e\u306e\u30c6\u30ad\u30b9\u30c8\n"
        "emoji: \U0001f680 \u2705 \u2764\ufe0f\n"
        "symbols: \u00a9 \u00ae \u20ac \u00a3 \u00a5\n"
    )
    res = te.propose_file(path="unicode.txt", content=content)
    assert res["ok"], res
    assert _read("unicode.txt", sandbox) == content


def test_escape_sequences_preserved_literally(sandbox):
    te = ToolExecutor()
    content = "\\n literal backslash-n\n" \
              "\\t literal tab\n" \
              "\\\\ double backslash\n" \
              "unicode escape \\u0041\n" \
              "hex escape \\x41\n"
    res = te.propose_file(path="escapes.txt", content=content)
    assert res["ok"], res
    assert _read("escapes.txt", sandbox) == content


def test_quotes_and_all_sorts_of_symbols_preserved(sandbox):
    te = ToolExecutor()
    content = (
        "double \"quotes\"\n"
        "single 'quotes'\n"
        "backtick `code`\n"
        "angle <brackets> & ampersand\n"
        "pipe | inside\n"
        "percent % placeholder\n"
        "dollar $var and {brace}\n"
        "semi;colon: colon\n"
        "equals= sign + plus - minus * star / slash\n"
        "tab:\tcolumn\n"
    )
    res = te.propose_file(path="symbols.txt", content=content)
    assert res["ok"], res
    assert _read("symbols.txt", sandbox) == content


# --- 4. JSON / Markdown / Python content ---

def test_json_content_preserved(sandbox):
    te = ToolExecutor()
    data = {
        "name": "test",
        "nested": {"a": [1, 2, 3], "b": "\u0041"},
        "emoji": "\U0001f680",
    }
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    res = te.propose_file(path="data.json", content=content)
    assert res["ok"], res
    on_disk = _read("data.json", sandbox)
    assert on_disk == content
    assert json.loads(on_disk) == data


def test_markdown_content_preserved(sandbox):
    te = ToolExecutor()
    content = (
        "# Title\n\n"
        "| Col1 | Col2 |\n"
        "|------|------|\n"
        "| A    | B    |\n"
        "| 1    | 2    |\n\n"
        "1. First item\n"
        "2. Second item\n"
    )
    res = te.propose_file(path="doc.md", content=content)
    assert res["ok"], res
    assert _read("doc.md", sandbox) == content


def test_python_file_valid_syntax_preserved(sandbox):
    te = ToolExecutor()
    content = (
        "# -*- coding: utf-8 -*-\n"
        "import os\n\n"
        "def greet(name: str) -> str:\n"
        "    return f\"Hello, {name}! \U0001f680\"\n\n\n"
        "if __name__ == '__main__':\n"
        "    print(greet('\u043c\u0438\u0440'))\n"
    )
    res = te.propose_file(path="src/mod.py", content=content)
    assert res["ok"], res
    assert _read("src/mod.py", sandbox) == content


def test_python_multiline_string_with_pipe_numbers(sandbox):
    te = ToolExecutor()
    content = (
        "def describe():\n"
        "    return \"\"\"\n"
        "1| alpha\n"
        "2| beta\n"
        "  42| gamma\n"
        "\"\"\"\n"
    )
    res = te.propose_file(path="src/docstring.py", content=content)
    assert res["ok"], res
    assert _read("src/docstring.py", sandbox) == content


# --- 5. THE KEY REGRESSION: lines that look like read_file line numbers ---

def test_lines_looking_like_line_numbers_are_preserved(sandbox):
    """
    Regression test for _strip_line_numbers() removal.

    File content legitimately containing lines starting with digits+pipe
    must NOT be silently stripped. The old _strip_line_numbers would
    corrupt these while reporting verified=True.
    """
    te = ToolExecutor()
    content = (
        "Notes:\n"
        "1| first item\n"
        "2| second item\n"
        "  42| indented item\n"
        "\n"
        "Plain line after\n"
    )
    res = te.propose_file(path="notes.txt", content=content)
    assert res["ok"], res
    assert res["verified"] is True
    on_disk = _read("notes.txt", sandbox)
    assert on_disk == content, (
        f"Content corrupted! Expected: {content!r}\nGot: {on_disk!r}"
    )


def test_numbered_list_with_pipes_in_markdown(sandbox):
    te = ToolExecutor()
    content = (
        "# Log excerpt\n\n"
        "```\n"
        "  1| 2024-01-01 INFO start\n"
        "  2| 2024-01-01 DEBUG loop\n"
        "```\n"
    )
    res = te.propose_file(path="log.md", content=content)
    assert res["ok"], res
    assert _read("log.md", sandbox) == content


def test_pipe_number_lines_inside_json_string(sandbox):
    te = ToolExecutor()
    data = {"log": "1| start\n2| end\n"}
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    res = te.propose_file(path="log_data.json", content=content)
    assert res["ok"], res
    assert _read("log_data.json", sandbox) == content


# --- 6. Large file ---

def test_large_file_roundtrip(sandbox):
    te = ToolExecutor()
    line = "x" * 100 + "\n"
    content = line * 20000  # ~2 MB
    res = te.propose_file(path="large.txt", content=content)
    assert res["ok"], res
    on_disk = _read("large.txt", sandbox)
    assert on_disk == content
    assert len(on_disk) == len(content)


# --- 7. Unknown / edge paths ---

def test_protected_file_rejected(sandbox, monkeypatch):
    monkeypatch.setattr(config, "PROTECTED_FILES", ("secret.txt",))
    te = ToolExecutor()
    res = te.propose_file(path="secret.txt", content="nope\n")
    assert res["ok"] is False
    assert "PROTECTED" in (res.get("error") or "").upper() or \
           "PROTECTED" in " ".join(res.get("errors", [])).upper()
    assert not (sandbox / "secret.txt").exists()


def test_path_traversal_rejected(sandbox):
    te = ToolExecutor()
    res = te.propose_file(path="../outside.txt", content="nope\n")
    assert res["ok"] is False


def test_directory_path_rejected(sandbox):
    te = ToolExecutor()
    res = te.propose_file(path="src", content="nope\n")
    assert res["ok"] is False


# --- 8. Verification integration ---

def test_verified_text_matches_disk(sandbox):
    te = ToolExecutor()
    content = "some content with \U0001f680 and \\n\n"
    res = te.propose_file(path="check.txt", content=content)
    assert res["ok"], res
    assert res["verified_text"] == content


def test_changelog_created(sandbox):
    te = ToolExecutor()
    res = te.propose_file(path="logged.txt", content="data\n")
    assert res["ok"], res
    changelog = _read("CHANGELOG.md", sandbox)
    assert "logged.txt" in changelog


def test_draft_cleared_after_apply(sandbox):
    te = ToolExecutor()
    res = te.propose_file(path="gone.txt", content="data\n")
    assert res["ok"], res
    draft = config.WORKSPACE_DIR / "gone.txt"
    assert not draft.exists()
def test_propose_file_non_utf8_returns_clean_error(sandbox):
    """propose_file on a non-UTF-8 file must return a clean error, not raise."""
    raw = "name = \"\u041f\u0440\u0438\u0432\u0435\u0442\"\n".encode("cp1251")
    (sandbox / "src" / "win_file.py").write_bytes(raw)
    te = ToolExecutor()
    res = te.propose_file("src/win_file.py", "x = 1\n")
    assert res["ok"] is False
    assert "not valid UTF-8" in res.get("error", "")
    assert (sandbox / "src" / "win_file.py").read_bytes() == raw
