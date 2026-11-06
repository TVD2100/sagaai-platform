# -*- coding: utf-8 -*-
"""capture_loop_golden.py - refresh the golden snapshots of the agent loop.

The snapshots live in ``tests/fixtures/loop_golden/`` and are compared by
``tests/test_loop_golden_payloads.py``. They pin what the loop sends to the
model and what it publishes to the UI, so a step split can be checked for
behaviour-neutrality instead of trusted.

This script is the deliberate way to refresh them: it re-records every
scenario, then runs the suite again WITHOUT the update flag. A snapshot that
cannot be reproduced fails that second run, which is exactly the mistake this
script exists to prevent - re-recording and never verifying would leave a
snapshot nobody knows how to satisfy.

Usage::

    python scripts/capture_loop_golden.py              # re-record and verify
    python scripts/capture_loop_golden.py --only read_tool

The equivalent manual form is ``UPDATE_GOLDEN=1 pytest tests/test_loop_golden_payloads.py``
followed by a plain run; the script only keeps the two steps together.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = os.path.join("tests", "test_loop_golden_payloads.py")


def _run(pytest_args, update):
    """Run pytest from the repository root; return (exit code, output)."""
    environment = dict(os.environ)
    if update:
        environment["UPDATE_GOLDEN"] = "1"
    else:
        environment.pop("UPDATE_GOLDEN", None)
    command = [sys.executable, "-m", "pytest", "-q", TEST_FILE] + list(pytest_args)
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               timeout=600, env=environment)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def main(argv=None):
    """Re-record the snapshots and verify they are reproducible."""
    parser = argparse.ArgumentParser(prog="capture_loop_golden.py",
                                     description="Refresh the agent-loop golden snapshots.")
    parser.add_argument("--only", action="append", default=[],
                        help="re-record one scenario by name (repeatable)")
    args = parser.parse_args(argv)

    select = []
    for name in args.only:
        select.extend(["-k", name])

    code, output = _run(select, update=True)
    print("recording:")
    print(output.strip())
    if code != 0:
        # An update run that fails means a scenario itself broke; the
        # snapshots were not refreshed, so there is nothing to verify.
        print("recording failed - the snapshots were not refreshed", file=sys.stderr)
        return code

    code, output = _run(select, update=False)
    print("verifying:")
    print(output.strip())
    if code != 0:
        print("the refreshed snapshots do not reproduce", file=sys.stderr)
        return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
