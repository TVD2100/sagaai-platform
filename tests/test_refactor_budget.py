"""The structural budget keeps the god files from growing back.

The budget file is the ratchet: any measurable property of a listed file may
fall, and must then be written down in the same commit, but it may never
rise. These tests pin the MECHANISM (measuring, comparing, the three-status
contract and the exit code), not the current numbers - the numbers live in
``refactor_budget.json`` and are checked by the gate itself.
"""
import json
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))
sys.path.insert(0, str(PKG_ROOT / "scripts"))

import refactor_budget as rb  # noqa: E402 - after sys.path preparation

# ── measuring ────────────────────────────────────────────────────────────────

def test_file_lines_does_not_count_the_trailing_newline(tmp_path):
    """A three-line file with a final newline is three lines, not four."""
    path = tmp_path / "m.py"
    path.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    assert rb.file_lines(str(path)) == 3


def test_longest_function_sees_nested_functions(tmp_path):
    """A nested function counts as a function and its length is measured."""
    path = tmp_path / "m.py"
    path.write_text(
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
        "\n"
        "def short():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    name, length = rb.longest_function(str(path))
    assert name == "outer"
    assert length == 4


def test_longest_function_of_a_file_without_functions(tmp_path):
    path = tmp_path / "m.py"
    path.write_text("CONSTANT = 1\n", encoding="utf-8")
    assert rb.longest_function(str(path)) == ("", 0)


def test_complexity_map_is_empty_for_an_absent_file():
    assert rb.complexity_map(["no/such/file.py"]) == ({}, "")


def test_complexity_is_measured_with_ruff(tmp_path):
    """A branching function has a complexity above one; a flat one does not."""
    path = tmp_path / "m.py"
    path.write_text(
        "def branchy(x):\n"
        "    if x == 1:\n"
        "        return 1\n"
        "    if x == 2:\n"
        "        return 2\n"
        "    return 0\n"
        "\n"
        "def flat():\n"
        "    return 0\n",
        encoding="utf-8",
    )
    values, problem = rb.complexity_map([str(path)])
    if problem:
        pytest.skip("ruff is not available: " + problem)
    branchy = [v for k, v in values.items() if k.endswith("::branchy")]
    flat = [v for k, v in values.items() if k.endswith("::flat")]
    assert branchy and branchy[0] >= 3
    assert flat == [] or flat[0] <= 1


# ── comparing ────────────────────────────────────────────────────────────────

def test_compare_is_empty_when_every_value_is_within_the_budget():
    budget = {"a.py": {"file_lines": 10, "function_lines": 5, "max_cc": 3}}
    measured = {"a.py": {"file_lines": 10, "function_lines": 5, "max_cc": 3}}
    assert rb.compare(budget, measured) == []


def test_compare_reports_a_value_that_grew():
    budget = {"a.py": {"file_lines": 10}}
    measured = {"a.py": {"file_lines": 11}}
    problems = rb.compare(budget, measured)
    assert len(problems) == 1 and "file_lines" in problems[0] and "11" in problems[0]


def test_compare_allows_a_value_to_fall():
    budget = {"a.py": {"file_lines": 10, "max_cc": 30}}
    measured = {"a.py": {"file_lines": 4, "max_cc": 2}}
    assert rb.compare(budget, measured) == []


def test_compare_reports_an_entry_that_is_no_longer_measured():
    """A budget row nobody measures is a hole, not a satisfied limit."""
    problems = rb.compare({"gone.py": {"file_lines": 1}}, {})
    assert problems and "not measured" in problems[0]


def test_compare_ignores_properties_the_budget_does_not_list():
    budget = {"a.py": {"file_lines": 10}}
    measured = {"a.py": {"file_lines": 10, "max_cc": 999}}
    assert rb.compare(budget, measured) == []


# ── the gate ─────────────────────────────────────────────────────────────────

def _budget(tmp_path, monkeypatch, document):
    path = tmp_path / "refactor_budget.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def test_a_missing_budget_passes_and_says_so(tmp_path):
    status, detail = rb.check(str(tmp_path / "refactor_budget.json"))
    assert status == "ok"
    assert "not in force" in detail


def test_an_unreadable_budget_is_an_environment_error(tmp_path):
    path = tmp_path / "refactor_budget.json"
    path.write_text("{not json", encoding="utf-8")
    assert rb.check(str(path))[0] == "env"


def test_a_budget_without_limits_is_an_environment_error(tmp_path):
    path = _budget(tmp_path, None, {"schema": 1})
    assert rb.check(path)[0] == "env"


def test_the_gate_fails_when_a_measured_value_exceeds_the_budget(tmp_path, monkeypatch):
    path = _budget(tmp_path, monkeypatch, {"limits": {"a.py": {"file_lines": 1}}})
    monkeypatch.setattr(rb, "measure", lambda paths: ({"a.py": {"file_lines": 99}}, ""))
    status, detail = rb.check(path)
    assert status == "fail"
    assert "file_lines" in detail


def test_the_gate_passes_when_every_value_holds(tmp_path, monkeypatch):
    path = _budget(tmp_path, monkeypatch, {"limits": {"a.py": {"file_lines": 10}}})
    monkeypatch.setattr(rb, "measure", lambda paths: ({"a.py": {"file_lines": 10}}, ""))
    assert rb.check(path)[0] == "ok"


def test_a_measurement_problem_is_an_environment_error(tmp_path, monkeypatch):
    path = _budget(tmp_path, monkeypatch, {"limits": {"a.py": {"file_lines": 10}}})
    monkeypatch.setattr(rb, "measure", lambda paths: ({}, "ruff: command not found"))
    status, detail = rb.check(path)
    assert status == "env"
    assert "ruff" in detail


# ── the command line ─────────────────────────────────────────────────────────

def test_main_returns_one_when_the_budget_is_exceeded(monkeypatch, capsys):
    monkeypatch.setattr(rb, "check", lambda: ("fail", "a.py: file_lines is 99"))
    assert rb.main([]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_returns_two_for_an_environment_error(monkeypatch):
    monkeypatch.setattr(rb, "check", lambda: ("env", "ruff is missing"))
    assert rb.main([]) == 2


def test_main_prints_the_result_as_json(monkeypatch, capsys):
    monkeypatch.setattr(rb, "check", lambda: ("ok", "fine"))
    assert rb.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"status": "ok", "detail": "fine"}


# ── the global ratchet ───────────────────────────────────────────────────────

def _tree(root, files):
    """Write {relative path: source} into *root* and return it as a string."""
    for rel, text in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return str(root)


def test_iter_source_files_skips_tests_and_runtime_data(tmp_path):
    """The tree-wide scan covers production code only."""
    root = _tree(tmp_path, {
        "core/mod.py": "def f():\n    return 1\n",
        "app.py": "x = 1\n",
        "tests/test_mod.py": "def t():\n    return 1\n",
        "data/cache.py": "y = 1\n",
        "skills/tool/run.py": "z = 1\n",
    })
    assert rb.iter_source_files(root) == ["app.py", "core/mod.py"]


def test_measure_global_names_the_longest_function_and_the_top_complexity(tmp_path):
    """Both tree-wide values carry the owner that produced them."""
    root = _tree(tmp_path, {
        "core/long.py": "def long_one():\n" + "    x = 1\n" * 20 + "    return x\n",
        "core/branchy.py": (
            "def branchy(v):\n"
            "    if v == 1:\n        return 1\n"
            "    if v == 2:\n        return 2\n"
            "    if v == 3:\n        return 3\n"
            "    return 0\n"
        ),
    })
    measured, problem = rb.measure_global(root)
    if problem:
        pytest.skip("ruff is not available: " + problem)
    assert measured["max_function_lines"] == 22
    assert measured["longest_function"] == "core/long.py::long_one"
    assert measured["max_cc"] >= 3
    assert measured["most_complex"] == "core/branchy.py::branchy"


def test_measure_global_of_a_tree_without_sources_is_a_problem(tmp_path):
    measured, problem = rb.measure_global(str(tmp_path))
    assert measured == {}
    assert "no Python sources" in problem


def test_compare_global_is_empty_when_every_value_is_within_the_budget():
    limits = {"max_function_lines": 100, "max_cc": 10}
    measured = {"max_function_lines": 100, "max_cc": 10}
    assert rb.compare_global(limits, measured) == []


def test_compare_global_reports_a_value_that_grew():
    problems = rb.compare_global({"max_cc": 5}, {"max_cc": 9, "most_complex": "a.py::f"})
    assert len(problems) == 1
    assert "max_cc" in problems[0] and "9" in problems[0] and "a.py::f" in problems[0]


def test_compare_global_allows_a_value_to_fall():
    assert rb.compare_global({"max_cc": 30}, {"max_cc": 2}) == []


def test_compare_global_ignores_properties_the_budget_does_not_list():
    limits = {"max_function_lines": 10}
    measured = {"max_function_lines": 10, "max_cc": 999}
    assert rb.compare_global(limits, measured) == []


def test_the_gate_fails_when_a_tree_wide_value_exceeds_the_budget(tmp_path, monkeypatch):
    path = _budget(tmp_path, None, {"limits": {"a.py": {"file_lines": 1}}, "global": {"max_cc": 2}})
    monkeypatch.setattr(rb, "measure", lambda paths: ({"a.py": {"file_lines": 1}}, ""))
    monkeypatch.setattr(
        rb, "measure_global", lambda root="": ({"max_cc": 30, "most_complex": "x.py::y"}, "")
    )
    status, detail = rb.check(path)
    assert status == "fail"
    assert "whole tree" in detail and "x.py::y" in detail


def test_the_gate_passes_when_the_tree_wide_values_hold(tmp_path, monkeypatch):
    path = _budget(tmp_path, None, {"limits": {"a.py": {"file_lines": 1}}, "global": {"max_cc": 5}})
    monkeypatch.setattr(rb, "measure", lambda paths: ({"a.py": {"file_lines": 1}}, ""))
    monkeypatch.setattr(rb, "measure_global", lambda root="": ({"max_cc": 5}, ""))
    assert rb.check(path)[0] == "ok"


def test_a_budget_without_the_global_section_skips_the_tree_wide_measurement(tmp_path, monkeypatch):
    """Backward compatibility: an older budget still passes and is not scanned twice."""
    path = _budget(tmp_path, None, {"limits": {"a.py": {"file_lines": 1}}})
    monkeypatch.setattr(rb, "measure", lambda paths: ({"a.py": {"file_lines": 1}}, ""))

    def boom(root=""):
        raise AssertionError("the tree-wide measurement must not run without a global section")

    monkeypatch.setattr(rb, "measure_global", boom)
    status, detail = rb.check(path)
    assert status == "ok"
    assert "whole tree" not in detail


# ── the committed budget describes the tree ──────────────────────────────────

def test_the_committed_budget_lists_only_existing_files():
    document = json.loads((PKG_ROOT / "refactor_budget.json").read_text(encoding="utf-8"))
    missing = [path for path in document["limits"] if not (PKG_ROOT / path).is_file()]
    assert missing == [], "budget rows without a file: %s" % missing


def test_the_committed_budget_holds_today():
    """The gate must be green on the tree it ships with."""
    status, detail = rb.check(str(PKG_ROOT / "refactor_budget.json"))
    assert status == "ok", detail


def test_the_committed_budget_guards_the_whole_tree():
    """The two tree-wide ceilings must exist and stay below the series targets."""
    document = json.loads((PKG_ROOT / "refactor_budget.json").read_text(encoding="utf-8"))
    globals_limits = document.get("global")
    assert isinstance(globals_limits, dict), "the budget must carry a global section"
    assert globals_limits["max_function_lines"] <= 200
    assert globals_limits["max_cc"] <= 30
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
