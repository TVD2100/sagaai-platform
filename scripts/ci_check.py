#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ci_check.py - every gate a change must pass, in one command.

The script is the single source of truth for the repository's checks: the
GitHub Actions workflow calls it exactly as a developer does, so "green
locally" and "green in CI" mean the same thing.

Checks, in order:

- ``ruff``     - lint with the repository's ``ruff.toml``;
- ``types``    - pyright, against the error budget in ``pyright_budget.json``;
- ``manifest`` - ``scripts/verify_manifest.py`` (schema, hashes, coverage);
- ``lock``     - the declared requirements and the lock file agree;
- ``tests``    - the whole suite in one sequential process.

Each check reports one of three outcomes: passed, failed, or an environment
error (a tool the check needs is missing). The exit code is 0 when every
check passed, 1 when any check failed, 2 when a check could not run.

Usage:
    python scripts/ci_check.py                 # every check
    python scripts/ci_check.py --list          # names, one per line
    python scripts/ci_check.py --only ruff     # a subset, comma-separated
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS_PATH = os.path.join(ROOT, "requirements.txt")
#: The optional API transport has its own constraint file. The suite runs its
#: tests whenever fastapi is installed, so the lock has to cover it as well -
#: a stale entry there would silently turn those tests into skips.
REQUIREMENTS_API_PATH = os.path.join(ROOT, "requirements-api.txt")
LOCK_PATH = os.path.join(ROOT, "requirements.lock")
MANIFEST_SCRIPT = os.path.join(ROOT, "scripts", "verify_manifest.py")
#: The type-check budget. Absent means the check is not in force yet, which
#: is reported as a pass with an explanation - the same contract the lock
#: check uses while no lock file exists.
PYRIGHT_BUDGET_PATH = os.path.join(ROOT, "pyright_budget.json")

OPERATORS = (">=", "<=", "==", "~=", "!=", ">", "<")

#: One check name -> function returning (status, detail). Status is "ok",
#: "fail" or "env" (the check could not run in this environment).
Status = Tuple[str, str]


def read_lines(path: str) -> List[str]:
    """Return the lines of *path*, or an empty list when it does not exist."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def clean_name(raw: str) -> str:
    """Return a distribution name without extras, markers or a comment."""
    text = raw.split(";", 1)[0]
    text = text.split("[", 1)[0]
    text = text.split(" #", 1)[0]
    return text.strip().lower()


def parse_requirements(path: str) -> Dict[str, str]:
    """Return {distribution: specifier} for one requirements file.

    ``streamlit>=1.66.0`` becomes ``{"streamlit": ">=1.66.0"}``; a bare name
    yields an empty specifier. Comments, blank lines and pip options are
    skipped, so an ``-r`` include or a ``--find-links`` line cannot be
    mistaken for a requirement.
    """
    entries: Dict[str, str] = {}
    for raw in read_lines(path):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.split(" #", 1)[0].strip()
        line = line.split(";", 1)[0].strip()
        for operator in OPERATORS:
            if operator in line:
                name, _, version = line.partition(operator)
                entries[clean_name(name)] = operator + version.strip()
                break
        else:
            entries[clean_name(line)] = ""
    return entries


def parse_lock(path: str) -> Tuple[Dict[str, str], Dict[str, int]]:
    """Return ({distribution: pinned version}, {distribution: hash count}).

    The lock is what ``pip-compile --generate-hashes`` writes: one
    ``name==version`` line per distribution, optionally continued with
    ``--hash=`` lines. Option blocks and comments are ignored.
    """
    versions: Dict[str, str] = {}
    hashes: Dict[str, int] = {}
    current: Optional[str] = None
    for raw in read_lines(path):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash"):
            if current is not None:
                hashes[current] = hashes.get(current, 0) + 1
            continue
        if line.startswith("-"):
            current = None
            continue
        name, separator, rest = line.partition("==")
        if not separator:
            current = None
            continue
        current = clean_name(name)
        versions[current] = rest.strip().rstrip("\\").strip()
        hashes.setdefault(current, 0)
    return versions, hashes


def version_tuple(value: str) -> Tuple[int, ...]:
    """Return the leading numeric components of *value* as a tuple.

    ``"1.66.0"`` -> ``(1, 66, 0)``; anything after the first non-digit of a
    component ends the parse, so pre-release and build suffixes compare by
    their numeric core.
    """
    parts: List[int] = []
    for chunk in value.strip().split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _padded(left: Tuple[int, ...], right: Tuple[int, ...]) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    size = max(len(left), len(right))
    return left + (0,) * (size - len(left)), right + (0,) * (size - len(right))


def satisfies(pinned: str, specifier: str) -> bool:
    """Return True when *pinned* satisfies every clause of *specifier*.

    The comparison is numeric and tolerates a missing component
    (``1.66`` equals ``1.66.0``). An unparsable side makes the answer False:
    a lock entry the check cannot read must not be reported as agreeing.
    """
    if not specifier:
        return True
    have = version_tuple(pinned)
    if not have:
        return False
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        operator = ""
        for candidate in OPERATORS:
            if clause.startswith(candidate):
                operator = candidate
                break
        if not operator:
            operator = "=="
        want = version_tuple(clause[len(operator):].strip())
        if not want:
            return False
        left, right = _padded(have, want)
        if operator == ">=" and not left >= right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == "==" and not left == right:
            return False
        if operator == "!=" and not left != right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "~=":
            # Compatible release: same leading components, not older than the pin.
            if not left >= right:
                return False
            if len(right) > 1 and left[:len(right) - 1] != right[:len(right) - 1]:
                return False
    return True


def compare_lock(requirements: Dict[str, str],
                 versions: Dict[str, str],
                 hashes: Dict[str, int]) -> List[str]:
    """Return every disagreement between the declared requirements and the lock.

    Only DECLARED distributions are compared: the lock also carries their
    transitive dependencies, and an entry nobody declared is not a defect by
    itself.
    """
    problems: List[str] = []
    for name in sorted(requirements):
        specifier = requirements[name]
        if name not in versions:
            problems.append("%s: declared in requirements.txt, missing from the lock" % name)
            continue
        if not satisfies(versions[name], specifier):
            problems.append("%s: the lock pins %s, which does not satisfy %r"
                            % (name, versions[name], specifier))
        if not hashes.get(name):
            problems.append("%s: the lock entry carries no --hash lines" % name)
    return problems


def tail(text: str, limit: int = 60) -> str:
    """Return the last *limit* non-empty lines of *text*."""
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def run_process(command: List[str], timeout: int = 1800) -> Tuple[int, str]:
    """Run *command* from the repository root; return (exit code, output)."""
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "%s: command not found" % command[0]
    except subprocess.TimeoutExpired:
        return 124, "%s: timed out after %d s" % (command[0], timeout)
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return completed.returncode, output


def check_ruff() -> Status:
    """Lint the repository with the configuration in ``ruff.toml``."""
    executable = shutil.which("ruff")
    command = ([executable] if executable else [sys.executable, "-m", "ruff"])
    command = command + ["check", "--no-cache", "."]
    code, output = run_process(command, timeout=300)
    if code == 0:
        return "ok", "ruff: no findings"
    if "No module named" in output or code == 127:
        return "env", "ruff is not installed in this environment"
    return "fail", "ruff reported findings:\n" + tail(output)


def load_budget(path: str) -> Tuple[Optional[Dict[str, object]], str]:
    """Return (budget, problem) for one budget file.

    ``problem`` is an empty string when the file was read, an explanation
    otherwise. The distinction matters to the caller: a missing file means
    "the check is not in force" (a pass), while an unreadable one means the
    environment cannot run the check (an environment error). Confusing the
    two would either hide a broken budget or fail a build over nothing.
    """
    if not os.path.isfile(path):
        return None, ""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, "the budget file could not be read: %s" % exc
    if not isinstance(data, dict):
        return None, "the budget file holds no object"
    try:
        int(data.get("max_errors"))
    except (TypeError, ValueError):
        return None, "the budget file carries no numeric max_errors"
    return data, ""


def check_types() -> Status:
    """Check the pyright error count against the committed budget.

    A budget and not a zero: the codebase has findings that predate this
    check, and requiring zero would either block every change or invite
    silencing them wholesale. The check fails only when the count RISES, so
    a fix is always welcome and never punished by having to edit the file
    first - lowering ``max_errors`` is a separate, deliberate commit.

    A missing budget file passes with an explanation (the check is not in
    force yet); a broken one is an environment error, since the budget - not
    the code - is then what cannot be read.
    """
    budget, problem = load_budget(PYRIGHT_BUDGET_PATH)
    if problem:
        return "env", problem
    if budget is None:
        return "ok", "pyright_budget.json is absent - type budget not in force"

    packages = [str(item) for item in (budget.get("packages") or [])]
    if not packages:
        return "env", "pyright_budget.json lists no packages to analyse"
    executable = shutil.which("pyright")
    command = ([executable] if executable else [sys.executable, "-m", "pyright"])
    command = command + ["--outputjson"] + packages
    code, output = run_process(command, timeout=900)
    if "No module named" in output or code == 127:
        return "env", "pyright is not installed in this environment"

    try:
        report = json.loads(output[output.index("{"):])
    except (ValueError, IndexError):
        # A syntax error in a source file makes pyright exit non-zero without
        # a JSON document. That is a finding, not an environment problem.
        return "fail", "pyright produced no report:\n" + tail(output)
    if not isinstance(report, dict):
        return "fail", "pyright produced an unreadable report"

    summary = report.get("summary") or {}
    try:
        errors = int(summary.get("errorCount"))
    except (TypeError, ValueError):
        return "fail", "the pyright report carries no error count"
    allowed = int(budget.get("max_errors"))
    if errors > allowed:
        return "fail", ("pyright reports %d errors, above the budget of %d "
                         "- fix some, or raise the budget deliberately"
                         % (errors, allowed))
    return "ok", "pyright: %d errors, budget %d" % (errors, allowed)


def check_manifest() -> Status:
    """Validate the version manifest through the repository's own script."""
    if not os.path.isfile(MANIFEST_SCRIPT):
        return "env", "scripts/verify_manifest.py is missing"
    code, output = run_process([sys.executable, MANIFEST_SCRIPT], timeout=300)
    if code == 0:
        return "ok", tail(output, limit=2) or "manifest: valid"
    if code == 2:
        return "env", "manifest verification could not run:\n" + tail(output)
    return "fail", "manifest violations:\n" + tail(output)


def check_lock() -> Status:
    """Cross-check the constraint files against ``requirements.lock``.

    Both ``requirements.txt`` and ``requirements-api.txt`` are inputs of the
    lock, so both are compared. While the lock file does not exist the check
    reports that the cross-check is not in force yet and passes. As soon as
    the lock is committed, a declared distribution that is absent from it,
    pinned outside its declared range, or missing its hashes fails the run.
    """
    if not os.path.isfile(LOCK_PATH):
        return "ok", "requirements.lock is not in force yet - cross-check skipped"
    requirements = parse_requirements(REQUIREMENTS_PATH)
    if not requirements:
        return "fail", "requirements.txt declares no distributions"
    api_requirements = parse_requirements(REQUIREMENTS_API_PATH)
    declared = dict(requirements)
    declared.update(api_requirements)
    versions, hashes = parse_lock(LOCK_PATH)
    if not versions:
        return "fail", "requirements.lock lists no pinned distribution"
    problems = compare_lock(declared, versions, hashes)
    if problems:
        return "fail", "lock disagrees with the constraint files:\n  " + "\n  ".join(problems)
    return "ok", "lock agrees with the constraint files (%d declared distributions)" % len(declared)


def check_tests() -> Status:
    """Run the whole suite in one sequential process.

    A single process is deliberate: the modules keep process-global state, so
    splitting the suite across parallel workers produces failures that do not
    exist when the files run in one interpreter.
    """
    code, output = run_process([sys.executable, "-m", "pytest", "-q"], timeout=1800)
    if code == 0:
        return "ok", tail(output, limit=2) or "tests: passed"
    if code == 5:
        return "fail", "pytest collected no tests"
    return "fail", "test failures:\n" + tail(output)


#: The checks in execution order.
CHECKS: List[Tuple[str, Callable[[], Status]]] = [
    ("ruff", check_ruff),
    ("types", check_types),
    ("manifest", check_manifest),
    ("lock", check_lock),
    ("tests", check_tests),
]


def main(argv: Optional[List[str]] = None) -> int:
    """Run the selected checks; return the process exit code."""
    parser = argparse.ArgumentParser(
        prog="ci_check.py",
        description="Run the repository checks locally or in CI.",
    )
    parser.add_argument("--only", metavar="NAMES",
                        help="comma-separated check names to run")
    parser.add_argument("--list", action="store_true", dest="list_checks",
                        help="print the check names and exit")
    args = parser.parse_args(argv)

    if args.list_checks:
        for name, _ in CHECKS:
            print(name)
        return 0

    requested = [item.strip() for item in (args.only or "").split(",") if item.strip()]
    known = dict(CHECKS)
    unknown = [name for name in requested if name not in known]
    if unknown:
        print("unknown check(s): %s" % ", ".join(unknown), file=sys.stderr)
        print("available: %s" % ", ".join(known), file=sys.stderr)
        return 2
    selected = [(name, function) for name, function in CHECKS
                if not requested or name in requested]

    worst = 0
    for name, function in selected:
        print("==> %s" % name, flush=True)
        status, detail = function()
        marker = {"ok": "PASS", "fail": "FAIL", "env": "ENV"}.get(status, status.upper())
        print("[%s] %s\n%s" % (marker, name, detail), flush=True)
        if status == "fail":
            worst = max(worst, 1)
        elif status == "env":
            worst = max(worst, 2)

    print("\nresult: %s" % {0: "all checks passed", 1: "a check failed",
                            2: "a check could not run"}[worst], flush=True)
    return worst


if __name__ == "__main__":
    sys.exit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
