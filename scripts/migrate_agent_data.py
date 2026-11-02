#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Move legacy ``<workspace>/<slug>_data`` folders into the central data root.

Installations built before the central root existed kept one working-data
folder per employee inside the project. The central layout stores that data in
``DATA_DIR/agents_data/<slug>/`` instead, so one role has one memory no matter
which project a dialog works in.

The script is READ-ONLY by default: it prints the plan and copies nothing.
Pass ``--apply`` to copy. The legacy folder is never deleted here - check the
report, then remove it yourself once the central copy looks right.

Usage:
    python scripts/migrate_agent_data.py                     # plan only
    python scripts/migrate_agent_data.py --workspace <dir>    # plan another project
    python scripts/migrate_agent_data.py --apply              # copy the files
    python scripts/migrate_agent_data.py --slug marketer      # one employee
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _format_entry(entry: dict) -> str:
    """Render one plan entry as a single readable block."""
    lines = [
        f"[{entry.get('status')}] {entry.get('slug')}",
        f"    from: {entry.get('source')}",
        f"    to:   {entry.get('target')}",
        f"    files: {entry.get('files')} (target now: {entry.get('target_files')})",
        f"    note: {entry.get('reason')}",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    """Run the migration CLI. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description="Copy legacy working-data folders to DATA_DIR/agents_data/."
    )
    parser.add_argument(
        "--workspace", default="",
        help="project folder holding legacy <slug>_data folders "
             "(default: the active target)",
    )
    parser.add_argument(
        "--slug", action="append", default=None,
        help="limit the plan to one employee (repeatable)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="copy the files (without it nothing is written)",
    )
    args = parser.parse_args(argv)

    from core import memory_migration as mm

    workspace = args.workspace.strip()
    if not workspace:
        try:
            from dev_agent import config
            if getattr(config, "WORKSPACE_SELECTED", False):
                workspace = str(config.PROJECT_ROOT)
        except Exception:
            workspace = ""

    plan = mm.plan_migrations(workspace=workspace, slugs=args.slug)
    if not plan:
        print("No legacy working-data folders found.")
        return 0

    print(f"Legacy folders found: {len(plan)}")
    for entry in plan:
        print(_format_entry(entry))

    result = mm.apply_plan(plan, dry_run=not args.apply)
    print("")
    if not args.apply:
        print("Dry run: nothing was copied. Re-run with --apply to copy the files.")
        return 0

    print(f"Copied files: {result['total_copied']}")
    for outcome in result["applied"]:
        print(f"  {outcome.get('slug')}: ok={outcome.get('ok')} - "
              f"{outcome.get('reason')}")
    if result["failed"]:
        print("Some entries need attention:", file=sys.stderr)
        for outcome in result["failed"]:
            print(f"  {outcome.get('slug')}: {outcome.get('reason')}",
                  file=sys.stderr)
        return 1
    print("Legacy folders were kept. Remove them after checking the copies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
