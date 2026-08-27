# -*- coding: utf-8 -*-
"""test_apply_patch.py - regression tests for the surgical apply_patch tool.

The tool replaces exact 'old' snippets with 'new' text and fails loudly
(without touching the file) when an anchor is missing or ambiguous. These
behaviours were introduced after a self-reflection that identified full-file
rewrites as a source of truncation and syntax errors.
"""

import pytest

from dev_agent import config
from dev_agent.tool_executor import ToolExecutor


@pytest.fixture

def sandbox(tmp_path, monkeypatch):
    """Redirect DevAgent state into a temp sandbox."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(config, "PROTECTED_FILES", ())
    config.ensure_runtime_dirs()
    (root / "src" / "module.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    return root


def test_apply_patch_single_replacement(sandbox):
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "return a + b", "new": "return a + b + 0"}],
    )
    assert res["ok"], res
    assert res["replacements"] == 1
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "return a + b + 0" in on_disk
    assert "return a - b" in on_disk


def test_apply_patch_missing_anchor_leaves_file_untouched(sandbox):
    te = ToolExecutor()
    before = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    res = te.apply_patch("src/module.py", [{"old": "def mul", "new": "def mul2"}])
    assert not res["ok"]
    after = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert after == before


def test_apply_patch_ambiguous_anchor_rejected(sandbox):
    te = ToolExecutor()
    res = te.apply_patch("src/module.py", [{"old": "return", "new": "return #x"}])
    assert not res["ok"]
    # both occurrences are reported with their line numbers
    lines = [o["line"] for o in res.get("occurrences", [])]
    assert len(lines) == 2


def test_apply_patch_occurrence_argument(sandbox):
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "return", "new": "return #second", "occurrence": 2}],
    )
    assert res["ok"], res
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "return #second a - b" in on_disk
    # the first occurrence must stay untouched
    assert "return a + b\n" in on_disk


def test_apply_patch_multi_edit_atomic_on_error(sandbox):
    """When a later edit fails, the whole batch fails and the file is untouched."""
    te = ToolExecutor()
    before = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    res = te.apply_patch(
        "src/module.py",
        [
            {"old": "return a + b", "new": "return a + b + 0"},
            {"old": "def mul", "new": "def mul2"},  # missing -> fail
        ],
    )
    assert not res["ok"]
    after = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert after == before


def test_apply_patch_reports_applied_true_on_success(sandbox):
    """A successful patch reports applied=True so callers can rely on the flag."""
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "return a + b", "new": "return a + b + 42"}],
    )
    assert res["ok"], res
    assert res["applied"] is True
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "return a + b + 42" in on_disk


def test_apply_patch_reports_applied_false_when_only_staged(sandbox, monkeypatch):
    """In manual mode propose_file only stages; apply_patch must surface applied=False."""
    te = ToolExecutor()

    def _staged_propose(path, content, note="", auto_apply=True):
        return {"ok": True, "applied": False, "backup_version": None,
                "error": None, "new_text": content}

    monkeypatch.setattr(te, "propose_file", _staged_propose)
    before = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    res = te.apply_patch(
        "src/module.py",
        [{"old": "return a + b", "new": "return a + b + 1"}],
    )
    assert res["ok"], res
    assert res["applied"] is False
    assert res.get("staged") is True
    assert res.get("verified") is False
    after = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert after == before  # file must stay untouched when only staged


def test_apply_patch_append_mode(sandbox):
    """old='<END>' appends new text at the end of the file,
    inserting a trailing newline when missing."""
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "<END>", "new": "def mul(a, b):\n    return a * b\n"}],
    )
    assert res["ok"], res
    assert res["applied"] is True
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "def mul(a, b):" in on_disk
    assert on_disk.endswith("return a * b\n")


def test_apply_patch_append_mode_no_trailing_newline(sandbox):
    """Appending to a file without trailing newline inserts one first."""
    (sandbox / "src" / "module.py").write_text("line1", encoding="utf-8")
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "<END>", "new": "line2"}],
    )
    assert res["ok"], res
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert on_disk == "line1\nline2"


def test_apply_patch_append_empty_file(sandbox):
    """Appending to an empty file works without inserting a stray newline."""
    (sandbox / "src" / "module.py").write_text("", encoding="utf-8")
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "<END>", "new": "x"}],
    )
    assert res["ok"], res
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert on_disk == "x"


def test_apply_patch_append_plus_replace_in_one_batch(sandbox):
    """An append edit and a normal replacement work sequentially in one batch."""
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [
            {"old": "return a + b", "new": "return a + b + 7"},
            {"old": "<END>", "new": "def mul(a, b):\n    return a * b\n"},
        ],
    )
    assert res["ok"], res
    assert res["replacements"] == 2
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "return a + b + 7" in on_disk
    assert on_disk.endswith("return a * b\n")


def test_apply_patch_missing_anchor_error_has_edit_index_and_snippet(sandbox):
    """The missing-anchor error reports edit index i/N and the searched snippet."""
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [
            {"old": "return a + b", "new": "return a + b + 1"},
            {"old": "def mul(a, b):\n    return a * b", "new": "def mul2(a, b):"},
        ],
    )
    assert not res["ok"]
    err = res["error"]
    assert "edits[2/2]" in err
    assert "def mul(a, b):" in err


def test_apply_patch_non_dict_error_has_edit_index(sandbox):
    """A non-dict edit reports which edit (i/N) is invalid."""
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [
            {"old": "return a + b", "new": "return a + b + 1"},
            "not-a-dict",
        ],
    )
    assert not res["ok"]
    assert "edits[2/2]" in res["error"]


def test_apply_patch_ambiguous_error_has_edit_index(sandbox):
    """The ambiguous-anchor error reports the exact edit (i/N)."""
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [
            {"old": "return a + b", "new": "return a + b + 1"},
            {"old": "return", "new": "return #x"},  # ambiguous
        ],
    )
    assert not res["ok"]
    assert "edits[2/2]" in res["error"]
    assert len(res.get("occurrences", [])) == 2


def test_apply_patch_via_dispatch(sandbox):
    te = ToolExecutor()
    res = te.dispatch("apply_patch", {"path": "src/module.py", "edits": [{"old": "return a + b", "new": "return a + b + 100"}]})
    assert res["ok"], res
    assert res["applied"] is True
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "return a + b + 100" in on_disk


def test_apply_patch_via_dispatch_json(sandbox):
    te = ToolExecutor()
    call = {"tool": "apply_patch", "args": {"path": "src/module.py", "edits": [{"old": "return a - b", "new": "return a - b + 1"}]}}
    res = te.dispatch_json(call)
    assert res["ok"], res
    assert res["applied"] is True
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "return a - b + 1" in on_disk


def test_dispatch_rejects_unknown_args_structured(sandbox):
    """ToolExecutor.dispatch must reject unknown argument names BEFORE the
    call with a structured error: unknown_args + a usage suggestion pointing
    at the system-prompt documentation."""
    te = ToolExecutor()
    res = te.dispatch("read_file", {"path": "src/module.py", "bogus": 1})
    assert res.get("ok") is False
    assert res.get("unknown_args") == ["bogus"]
    assert "bogus" in res.get("error", "")
    assert "system prompt" in res.get("error", "")
    assert "Usage:" in res.get("suggestion", "")


def test_dispatch_known_args_still_work(sandbox):
    te = ToolExecutor()
    res = te.dispatch("read_file", {"path": "src/module.py"})
    assert res.get("ok") is True
    assert "def" in res.get("content", "")


def test_apply_patch_unicode_and_quotes(sandbox):
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "def add(a, b):", "new": "def add(a, b):  # «сумма» \"quoted\" \\ backslash"}],
    )
    assert res["ok"], res
    assert res["applied"] is True
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "def add(a, b):  # «сумма» \"quoted\" \\ backslash" in on_disk


def test_apply_patch_occurrence_out_of_range(sandbox):
    te = ToolExecutor()
    res = te.apply_patch("src/module.py", [{"old": "return", "new": "return #x", "occurrence": 5}])
    assert not res["ok"]
    assert "occurrence out of range" in res["error"]


def test_apply_patch_append_unicode(sandbox):
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "<END>", "new": "def mul(a, b):\n    return a * b  # умножение\n"}],
    )
    assert res["ok"], res
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "def mul(a, b):" in on_disk
    assert "# умножение" in on_disk


def test_apply_patch_multi_edit_with_occurrence(sandbox):
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [
            {"old": "return", "new": "return #first", "occurrence": 1},
            {"old": "return", "new": "return #second", "occurrence": 2},
        ],
    )
    assert res["ok"], res
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "return #first a + b" in on_disk
    assert "return #second a - b" in on_disk


def test_apply_patch_large_file_roundtrip(sandbox):
    big = "\n".join(f"line_{i}" for i in range(600))
    (sandbox / "src" / "module.py").write_text(big, encoding="utf-8")
    te = ToolExecutor()
    res = te.apply_patch("src/module.py", [{"old": "line_300", "new": "line_300_edited"}])
    assert res["ok"], res
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "line_300_edited" in on_disk
    assert "line_599" in on_disk
    assert len(on_disk.split("\n")) == 600


def test_apply_patch_empty_old_rejected(sandbox):
    te = ToolExecutor()
    res = te.apply_patch("src/module.py", [{"old": "", "new": "x"}])
    assert not res["ok"]
    assert "old must be a non-empty string" in res["error"]


def test_apply_patch_non_string_new_rejected(sandbox):
    te = ToolExecutor()
    res = te.apply_patch("src/module.py", [{"old": "return a + b", "new": 123}])
    assert not res["ok"]
    assert "new must be a string" in res["error"]


def test_apply_patch_edits_as_json_string(sandbox):
    """LLM sometimes sends 'edits' as a JSON string instead of a list."""
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        '[{"old": "return a + b", "new": "return a + b + 999"}]',
    )
    assert res["ok"], res
    assert res["applied"] is True
    on_disk = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert "return a + b + 999" in on_disk


def test_apply_patch_edits_invalid_type(sandbox):
    te = ToolExecutor()
    res = te.apply_patch("src/module.py", {"old": "return", "new": "x"})
    assert not res["ok"]
    assert "edits must be a list" in res["error"]


def test_apply_patch_reports_new_text_and_diff_when_staged(sandbox, monkeypatch):
    """Staged mode returns new_text and diff so the UI can render Apply/Discard."""
    te = ToolExecutor()

    def _staged_propose(path, content, note="", auto_apply=True):
        return {"ok": True, "applied": False, "backup_version": None,
                "error": None, "new_text": content, "diff": "--- a\n+++ b\n"}

    monkeypatch.setattr(te, "propose_file", _staged_propose)
    res = te.apply_patch(
        "src/module.py",
        [{"old": "return a + b", "new": "return a + b + 1"}],
    )
    assert res["ok"], res
    assert res["applied"] is False
    assert res["staged"] is True
    assert "return a + b + 1" in res.get("new_text", "")
    assert res.get("diff") == "--- a\n+++ b\n"


def test_ap_syntax_error_keeps_file_untouched_and_flags_propagated(sandbox):
    """A syntax-error patch is rejected before write; structured flags
    (syntax_error/errors/verified/wrote_file) must reach the caller."""
    te = ToolExecutor()
    before = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    res = te.apply_patch(
        "src/module.py",
        [{"old": "return a + b", "new": "return a + b (("}],
    )
    assert not res["ok"]
    assert res.get("syntax_error") is True
    assert res.get("verified") is False
    assert res.get("wrote_file") is False
    assert isinstance(res.get("errors"), list) and len(res["errors"]) >= 1
    assert "Python syntax error" in res["error"]
    after = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert after == before


def test_ap_syntax_error_rejects_whole_batch_atomically(sandbox):
    """A syntax error in ANY edit rejects the whole batch before write."""
    te = ToolExecutor()
    before = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    res = te.apply_patch(
        "src/module.py",
        [
            {"old": "return a + b", "new": "return a + b + 1"},
            {"old": "return a - b", "new": "return a - b (("},
        ],
    )
    assert not res["ok"]
    assert res.get("syntax_error") is True
    assert res.get("wrote_file") is False
    after = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert after == before


def test_ap_refuses_to_wipe_file(sandbox):
    """A patch that empties the whole file is refused and flags propagate."""
    te = ToolExecutor()
    before = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    res = te.apply_patch(
        "src/module.py",
        [{"old": before, "new": ""}],
    )
    assert not res["ok"]
    assert "Refusing to wipe" in res["error"]
    assert res.get("wrote_file") is False
    after = (sandbox / "src" / "module.py").read_text(encoding="utf-8")
    assert after == before


def test_ap_success_reports_verified_true(sandbox):
    """A successful auto-applied patch reports verified=True like propose_file."""
    te = ToolExecutor()
    res = te.apply_patch(
        "src/module.py",
        [{"old": "return a + b", "new": "return a + b + 11"}],
    )
    assert res["ok"], res
    assert res["applied"] is True
    assert res.get("verified") is True


def test_ap_fuzzy_indent(sandbox):
    te = ToolExecutor()
    res = te.apply_patch('src/module.py', [{'old': 'def sub(a, b):\n   return a - b', 'new': 'def sub(a, b):\n    return a - b + 1'}])
    assert res['ok'], res
    assert res['fuzzy_count'] == 1
    on_disk = (sandbox / 'src' / 'module.py').read_text(encoding='utf-8')
    assert 'return a - b + 1' in on_disk

def test_ap_fuzzy_spaces(sandbox):
    te = ToolExecutor()
    res = te.apply_patch('src/module.py', [{'old': 'return    a + b', 'new': 'return a + b + 5'}])
    assert res['ok'], res
    assert res['fuzzy_count'] == 1
    on_disk = (sandbox / 'src' / 'module.py').read_text(encoding='utf-8')
    assert 'return a + b + 5' in on_disk

def test_ap_crlf(sandbox):
    crlf = 'def add(a, b):\r\n    return a + b\r\n'
    (sandbox / 'src' / 'module.py').write_text(crlf, encoding='utf-8')
    te = ToolExecutor()
    res = te.apply_patch('src/module.py', [{'old': 'return a + b', 'new': 'return a + b + 9'}])
    assert res['ok'], res
    assert res['normalized_line_endings'] is True
    on_disk = (sandbox / 'src' / 'module.py').read_text(encoding='utf-8')
    assert 'return a + b + 9' in on_disk

def test_ap_suggestions(sandbox):
    te = ToolExecutor()
    res = te.apply_patch('src/module.py', [{'old': 'return a + bbb', 'new': 'x'}])
    assert not res['ok']
    assert 'suggestions' in res, res
    for s in res['suggestions']:
        assert 'line' in s and 'text' in s
    texts = ' '.join(s['text'] for s in res['suggestions'])
    assert ('a + b' in texts) or ('a - b' in texts)

def test_ap_fuzzy_disabled(sandbox):
    te = ToolExecutor()
    res = te.apply_patch('src/module.py', [{'old': 'return    a + b', 'new': 'return a + b'}], fuzzy=False)
    assert not res['ok']
    assert 'not found in file' in res['error']

def test_ap_fuzzy_ambiguous(sandbox):
    (sandbox / 'src' / 'module.py').write_text('def f1():\n      return 1\n\ndef f2():\n      return 2\n', encoding='utf-8')
    te = ToolExecutor()
    res = te.apply_patch('src/module.py', [{'old': 'return', 'new': 'return #x'}])
    assert not res['ok']
    assert 'matches 2 time(s)' in res['error']
    assert len(res.get('occurrences', [])) == 2

def test_ap_utf8_bom_py_file_is_accepted(sandbox):
    """A .py file with a UTF-8 BOM is valid for Python (utf-8-sig) and
    must not be rejected with a fake syntax error."""
    (sandbox / "src" / "bom.py").write_text(
        "\ufeffdef f():\n    return 1\n", encoding="utf-8"
    )
    te = ToolExecutor()
    res = te.apply_patch("src/bom.py", [{"old": "def f():", "new": "def f2():"}])
    assert res["ok"], res
    on_disk = (sandbox / "src" / "bom.py").read_text(encoding="utf-8")
    assert "def f2():" in on_disk


def test_ap_non_utf8_file_clean_error_and_untouched(sandbox):
    """A non-UTF-8 file returns a clean, actionable error and stays untouched."""
    raw = "name = \"\u041f\u0440\u0438\u0432\u0435\u0442\"\n".encode("cp1251")
    (sandbox / "src" / "win.py").write_bytes(raw)
    te = ToolExecutor()
    res = te.apply_patch("src/win.py", [{"old": "name", "new": "title"}])
    assert not res["ok"]
    assert "not valid UTF-8" in res["error"]
    assert (sandbox / "src" / "win.py").read_bytes() == raw


def test_verify_file_directory_returns_clean_error(sandbox):
    """verify_file on a directory must return a clean error, not raise."""
    (sandbox / "src" / "adir").mkdir()
    te = ToolExecutor()
    res = te.verify_file("src/adir")
    assert res["ok"] is False
    assert "Not a regular file" in res["error"]
def test_verify_file_non_utf8_clean_error(sandbox):
    """verify_file on a non-UTF-8 file returns a clean error, not a raise."""
    (sandbox / "src" / "bad.bin").write_bytes(b"\xff\xfe\x00")
    te = ToolExecutor()
    res = te.verify_file("src/bad.bin")
    assert res["ok"] is False
    assert "not valid UTF-8" in res["error"]


def test_verify_file_same_substring_in_both_lists(sandbox):
    """A substring listed in both lists must fail as unexpected (and not be
    reported as missing)."""
    (sandbox / "src" / "v.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    te = ToolExecutor()
    res = te.verify_file(
        "src/v.txt", expected_substrings=["beta"], unexpected_substrings=["beta"]
    )
    assert res["ok"] is False
    assert res["missing_expected"] == []
    assert res["present_unexpected"] == ["beta"]
