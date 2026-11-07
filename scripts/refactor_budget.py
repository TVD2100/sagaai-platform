# -*- coding: utf-8 -*-
"""refactor_budget.py - keep the structural debt of the god files from growing.

The budget is a ceiling, not a target. Every entry records the CURRENT worst
value of one measurable property of one file:

- ``file_lines``     - the length of the file itself;
- ``function_lines`` - the length of its longest function;
- ``max_cc``         - the highest cyclomatic complexity of any function.

A change may lower a number and must edit the budget in the same commit when
it does, so the file always describes the tree. A change that RAISES a number
fails: that is the ratchet. The point is not that the tree is clean today -
the god files exist - but that splitting them can never quietly slide back.

Complexity is measured with ``ruff`` and mccabe (``C901``) rather than a
separate dependency: the project already pins ruff, and ``max-complexity=1``
makes the tool report every function, so the maximum can be read off directly.

Usage::

    python scripts/refactor_budget.py            # check against the budget
    python scripts/refactor_budget.py --report   # print the measured values

The check reports ``ok``, ``fail`` or ``env`` (the measuring tool is absent),
the same three outcomes ``scripts/ci_check.py`` uses for its other gates.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUDGET_NAME = "refactor_budget.json"

#: ``E501`` is off in the repository config; the probe turns everything off
#: except mccabe so the output is one line per function and nothing else.
_PROBE_CONFIG = 'target-version = "py312"\nline-length = 120\n\n[lint]\nselect = ["C901"]\n\n[lint.mccabe]\nmax-complexity = 1\n'
_CC_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):\d+: C901 `(?P<name>[^`]+)` is too complex \((?P<cc>\d+) > \d+\)"
)


# ── Measurement ──────────────────────────────────────────────────────────────


def _function_rows(tree: ast.AST) -> List[Tuple[str, int, int]]:
    """Return ``(name, line, length)`` for every function, nested included."""
    rows: List[Tuple[str, int, int]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                length = (child.end_lineno or child.lineno) - child.lineno + 1
                rows.append((name, child.lineno, length))
                walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")

    walk(tree, "")
    return rows


def file_lines(path: str) -> int:
    """Return the number of lines in *path* (a trailing newline is not a line)."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def longest_function(path: str) -> Tuple[str, int]:
    """Return ``(name, length)`` of the longest function in *path*."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    rows = _function_rows(tree)
    if not rows:
        return "", 0
    name, _line, length = max(rows, key=lambda row: row[2])
    return name, length


def _ruff_command() -> List[str]:
    """Return the command prefix that runs ruff in this environment."""
    executable = shutil.which("ruff")
    return [executable] if executable else [sys.executable, "-m", "ruff"]


def complexity_map(paths: List[str], base: str = "") -> Tuple[Dict[str, int], str]:
    """Return ``{"path::function": cc}`` for *paths*, and an error message.

    Keys are relative to *base* (the repository root by default): the caller
    that measures another tree gets names it can compare against its own
    file list. The probe config lives in a temporary file so the repository's
    own ``ruff.toml`` is never touched. An empty error string means success.
    """
    anchor = base or ROOT
    existing = [p for p in paths if os.path.isfile(p)]
    if not existing:
        return {}, ""
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
    try:
        handle.write(_PROBE_CONFIG)
        handle.close()
        command = _ruff_command() + [
            "check", "--no-cache", "--config", handle.name,
            "--output-format", "concise", "--quiet",
        ] + existing
        try:
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=300)
        except FileNotFoundError:
            return {}, "ruff: command not found"
        except subprocess.TimeoutExpired:
            return {}, "ruff: timed out while measuring complexity"
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode not in (0, 1):
        return {}, "ruff could not measure complexity:\n" + tail(output)
    result: Dict[str, int] = {}
    for line in output.splitlines():
        match = _CC_LINE.match(line.strip())
        if not match:
            continue
        rel = os.path.relpath(match.group("path"), anchor).replace(os.sep, "/")
        key = rel + "::" + match.group("name")
        result[key] = max(result.get(key, 0), int(match.group("cc")))
    return result, ""


def measure(paths: List[str]) -> Tuple[Dict[str, Dict[str, object]], str]:
    """Return the measured properties of every path, and an error message."""
    measured: Dict[str, Dict[str, object]] = {}
    for path in paths:
        if not os.path.isfile(path):
            continue
        rel = path.replace(os.sep, "/")
        name, length = longest_function(path)
        measured[rel] = {
            "file_lines": file_lines(path),
            "function_lines": length,
            "longest_function": name,
        }
    cc, problem = complexity_map([p for p in paths if os.path.isfile(p)])
    if problem:
        return measured, problem
    for rel, entry in measured.items():
        best = 0
        best_name = ""
        for key, value in cc.items():
            if key.startswith(rel + "::") and value > best:
                best, best_name = value, key.split("::", 1)[1]
        entry["max_cc"] = best
        entry["most_complex"] = best_name
    return measured, ""


# ── Comparison ───────────────────────────────────────────────────────────────


def tail(text: str, limit: int = 20) -> str:
    """Return the last *limit* non-empty lines of *text*."""
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


#: The properties compared against the budget, in reporting order.
PROPERTIES = ("file_lines", "function_lines", "max_cc")

#: The global properties compared against the budget, in reporting order.
#: They describe the WHOLE production tree rather than one listed file, so
#: the gate cannot be dodged by adding a file the budget never listed.
GLOBAL_PROPERTIES = ("max_function_lines", "max_cc")

#: Directories the global scan skips. Runtime data lives in the same tree as
#: the sources, and tests are excluded on purpose: a test fixture is allowed
#: to be long, and a ceiling raised to fit one would stop guarding the code.
GLOBAL_SKIP_DIRS = {
    ".git", ".dev_agent", "__pycache__", ".pytest_cache", ".venv", "venv",
    ".ruff_cache", "env", "build", "dist", "apps", "connectors", "assistants",
    "history", "orchestrators", "rag_bases", "skills", "system_prompts",
    "threads", "data", "deliverables", "personal_assistant_data", "tests",
}


def iter_source_files(root: str) -> List[str]:
    """Return every production ``.py`` file under *root*, as relative paths.

    Runtime data directories live in the same tree as the sources, so the
    walk skips them explicitly; ``tests`` is skipped too (a fixture is
    allowed to be long). The list is sorted so two runs agree.
    """
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in GLOBAL_SKIP_DIRS)
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found)


def measure_global(root: str = "") -> Tuple[Dict[str, object], str]:
    """Measure the two tree-wide properties, and return them with a problem.

    The properties are the length of the longest function and the highest
    cyclomatic complexity anywhere in the production tree. Both are measured
    over the SAME file list, so a value always has a named owner. A file that
    cannot be read or parsed is skipped, exactly as the per-file measurement
    does - a broken file must not turn into a gate failure of its own.
    """
    base = root or ROOT
    paths = iter_source_files(base)
    if not paths:
        return {}, "no Python sources found under %s" % base
    longest_name, longest_len = "", 0
    for rel in paths:
        name, length = longest_function(os.path.join(base, rel))
        if length > longest_len:
            longest_name, longest_len = rel + "::" + name, length
    cc_map, problem = complexity_map([os.path.join(base, rel) for rel in paths], base)
    if problem:
        return {}, problem
    top_cc, top_cc_name = 0, ""
    for key, value in cc_map.items():
        if value > top_cc:
            top_cc, top_cc_name = value, key
    return {
        "max_function_lines": longest_len,
        "max_cc": top_cc,
        "longest_function": longest_name,
        "most_complex": top_cc_name,
    }, ""


def compare_global(limits: Dict[str, object], measured: Dict[str, object]) -> List[str]:
    """Return one message per tree-wide limit violation.

    The same rule as the per-file budget applies: a value may fall, it may
    not rise. A missing limit is simply not checked, so a budget written
    before the global section existed keeps working.
    """
    problems: List[str] = []
    for prop in GLOBAL_PROPERTIES:
        allowed = limits.get(prop)
        if allowed is None:
            continue
        actual = int(measured.get(prop) or 0)
        if actual > int(allowed):
            where = ""
            if prop == "max_cc":
                where = measured.get("most_complex") or ""
            elif prop == "max_function_lines":
                where = measured.get("longest_function") or ""
            suffix = " (in %s)" % where if where else ""
            problems.append(
                "whole tree: %s is %d%s, above the budget of %d" % (prop, actual, suffix, int(allowed))
            )
    return problems


def compare(budget: Dict[str, Dict[str, int]],
            measured: Dict[str, Dict[str, object]]) -> List[str]:
    """Return one message per budget violation, and one per vanished entry.

    A file that is no longer measured is reported too: a budget entry that
    can never be checked is a hole in the ratchet, not a satisfied limit.
    """
    problems: List[str] = []
    for path, limits in sorted(budget.items()):
        entry = measured.get(path)
        if entry is None:
            problems.append("%s: listed in the budget but not measured" % path)
            continue
        for prop in PROPERTIES:
            allowed = limits.get(prop)
            if allowed is None:
                continue
            actual = int(entry.get(prop) or 0)
            if actual > int(allowed):
                # Naming the function turns "this file got worse" into a place
                # to look; the measurement already knows it.
                where = ""
                if prop == "max_cc":
                    where = entry.get("most_complex") or ""
                elif prop == "function_lines":
                    where = entry.get("longest_function") or ""
                suffix = " (in %s)" % where if where else ""
                problems.append(
                    "%s: %s is %d%s, above the budget of %d" % (path, prop, actual, suffix, int(allowed))
                )
    return problems


def load_budget(path: str) -> Tuple[Optional[Dict[str, object]], str]:
    """Return (budget document, problem); an absent file means "not in force"."""
    if not os.path.isfile(path):
        return None, ""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, "the budget file could not be read: %s" % exc
    if not isinstance(data, dict):
        return None, "the budget file holds no object"
    limits = data.get("limits")
    if not isinstance(limits, dict) or not limits:
        return None, "the budget file lists no limits"
    return data, ""


def check(budget_path: str = "") -> Tuple[str, str]:
    """Return ``(status, detail)`` for the ratchet gate.

    ``status`` is one of ``ok``, ``fail``, ``env`` - the same vocabulary
    ``scripts/ci_check.py`` uses. A missing budget passes with an explanation
    (the gate is not in force yet); an unreadable one is an environment
    error, since the budget - not the code - is what cannot be read.
    """
    path = budget_path or os.path.join(ROOT, BUDGET_NAME)
    document, problem = load_budget(path)
    if problem:
        return "env", problem
    if document is None:
        return "ok", "%s is absent - the structural budget is not in force" % BUDGET_NAME
    limits = document.get("limits")
    assert isinstance(limits, dict)
    measured, problem = measure(sorted(limits))
    if problem:
        return "env", problem
    problems = compare(limits, measured)
    globals_limits = document.get("global")
    if isinstance(globals_limits, dict) and globals_limits:
        measured_globals, problem = measure_global()
        if problem:
            return "env", problem
        problems = problems + compare_global(globals_limits, measured_globals)
    if problems:
        return "fail", "structural budget exceeded:\n  " + "\n  ".join(problems)
    summary = "structural budget holds for %d files" % len(limits)
    if isinstance(globals_limits, dict) and globals_limits:
        summary += " and the whole tree"
    return "ok", summary


def report() -> int:
    """Print the measured values; return the process exit code."""
    path = os.path.join(ROOT, BUDGET_NAME)
    document, problem = load_budget(path)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    limits = (document or {}).get("limits") or {}
    measured, problem = measure(sorted(limits))
    if problem:
        print(problem, file=sys.stderr)
        return 2
    for name in sorted(measured):
        entry = measured[name]
        print("%s: file=%s function=%s (%s) cc=%s (%s)" % (
            name, entry.get("file_lines"), entry.get("function_lines"),
            entry.get("longest_function"), entry.get("max_cc"), entry.get("most_complex"),
        ))
    globals_limits = (document or {}).get("global")
    if isinstance(globals_limits, dict) and globals_limits:
        globals_measured, problem = measure_global()
        if problem:
            print(problem, file=sys.stderr)
            return 2
        print("whole tree: max_function_lines=%s (%s) max_cc=%s (%s)" % (
            globals_measured.get("max_function_lines"), globals_measured.get("longest_function"),
            globals_measured.get("max_cc"), globals_measured.get("most_complex"),
        ))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Run the ratchet check; return the process exit code."""
    parser = argparse.ArgumentParser(prog="refactor_budget.py", description="Check the structural budget.")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--report", action="store_true", help="print the measured values")
    args = parser.parse_args(argv)
    if args.report:
        return report()
    status, detail = check()
    if args.json:
        print(json.dumps({"status": status, "detail": detail}, ensure_ascii=False))
    else:
        print("[%s] budget\n%s" % ({"ok": "PASS", "fail": "FAIL", "env": "ENV"}.get(status, status.upper()), detail))
    return {"ok": 0, "fail": 1, "env": 2}.get(status, 2)


if __name__ == "__main__":
    sys.exit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
