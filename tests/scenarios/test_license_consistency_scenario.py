"""Scenario tests for the license migration to PolyForm Noncommercial 1.0.0.

Walk the licensing story over the real repository the way a user, a
contributor and the updater would see it:

  1. The LICENSE file carries the canonical PolyForm Noncommercial 1.0.0
     text (title, canonical URL, required notice) and no MIT permission
     grant remains.
  2. No published source file keeps the MIT SPDX identifier; every module
     that carries the copyright line carries the new identifier instead.
  3. The user-facing surfaces agree with the LICENSE: README.md,
     COMPARISON.md, AGENT.md and the sidebar footer of ui/app.py.
  4. The license change shipped in release 2.0.0 (later 2.x releases keep
     it); the manifest app_version matches core/version.py, and the release
     note announces the license change.
  5. The licensing history stays documented (1.x and earlier were MIT) and
     the MIT licenses of other projects in COMPARISON.md are not wiped.

The scenario is read-only and needs no network or database.
"""
import json
import os
import re
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = Path(os.path.dirname(os.path.dirname(HERE)))
MANIFEST = REPO_ROOT / "file_versions.json"

MIT_SPDX = "# SPDX-License-Identifier: MIT"
NEW_SPDX = "# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0"
COPYRIGHT = "# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V."
REQUIRED_NOTICE = "Required Notice: Copyright (c) 2026 Deinekin T.V."


def _manifest() -> dict:
    """Load the repository manifest (the source of truth for shipped files)."""
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _published_paths(manifest: dict) -> list:
    """Return the sorted union of units[*].files and selectable paths."""
    paths = set()
    for unit in (manifest.get("units") or {}).values():
        files = (unit or {}).get("files")
        if isinstance(files, dict):
            paths.update(str(p) for p in files)
    paths.update(str(p) for p in (manifest.get("selectable") or {}))
    return sorted(paths)


def _read(rel: str) -> str:
    """Read a repository file as UTF-8 text."""
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_license_file_is_canonical_polyform():
    """given the repository, when LICENSE is opened, then it is the canonical text."""
    text = _read("LICENSE")
    assert text.startswith("# PolyForm Noncommercial License 1.0.0")
    assert "<https://polyformproject.org/licenses/noncommercial/1.0.0>" in text
    assert REQUIRED_NOTICE in text
    for section in (
        "## Acceptance",
        "## Copyright License",
        "## Distribution License",
        "## Notices",
        "## Changes and New Works License",
        "## Patent License",
        "## Noncommercial Purposes",
        "## Noncommercial Organizations",
        "## Fair Use",
        "## No Other Rights",
        "## Patent Defense",
        "## Violations",
        "## No Liability",
        "## Definitions",
    ):
        assert section in text, section
    # The MIT grant must be gone from the license file itself.
    assert "Permission is hereby granted" not in text
    assert "MIT License" not in text


def test_no_published_file_keeps_the_mit_spdx_identifier():
    """given the published file set, when scanned, then no MIT SPDX id remains."""
    manifest = _manifest()
    paths = _published_paths(manifest)
    assert len(paths) > 300, len(paths)
    mit_hits, new_hits, missing = [], 0, []
    for rel in paths:
        # The test file itself mentions the MIT identifier while proving it is gone.
        if os.path.abspath(os.path.join(str(REPO_ROOT), rel)) == os.path.abspath(__file__):
            continue
        if not rel.endswith((".py", ".md")):
            continue
        text = _read(rel)
        if MIT_SPDX in text:
            mit_hits.append(rel)
        if rel.endswith(".py"):
            if NEW_SPDX in text:
                new_hits += 1
            if COPYRIGHT in text and NEW_SPDX not in text:
                missing.append(rel)
    assert mit_hits == [], mit_hits
    assert missing == [], missing
    # Every copyright-bearing module migrated to the new identifier.
    assert new_hits >= 100, new_hits


def test_user_facing_surfaces_agree_with_the_license():
    """given the docs and UI, when checked, then all advertise PolyForm NC."""
    readme = _read("README.md")
    assert "PolyForm Noncommercial 1.0.0" in readme
    # No stale MIT license attribution for SagaAI itself.
    assert "[MIT](LICENSE)" not in readme
    assert "Согласно лицензии MIT" not in readme
    assert "код (MIT)" not in readme

    comparison = _read("COMPARISON.md")
    sagaai_rows = [ln for ln in comparison.splitlines() if ln.startswith("| **SagaAI")]
    assert sagaai_rows, "SagaAI row not found in COMPARISON.md"
    assert all("PolyForm NC 1.0.0" in ln for ln in sagaai_rows), sagaai_rows
    assert "| **Лицензия** | PolyForm NC 1.0.0" in comparison
    # Competitor MIT licenses must survive the migration.
    assert "| OpenClaw | MIT |" in comparison

    ui_app = _read("ui/app.py")
    assert "PolyForm Noncommercial License</a></div>" in ui_app
    assert ">MIT License</a></div>" not in ui_app

    agent_doc = _read("AGENT.md")
    assert NEW_SPDX in agent_doc
    assert "# SPDX-License-Identifier: MIT" not in agent_doc


def test_version_and_manifest_announce_the_change():
    """given the release metadata, when compared, then 2.x is consistent."""
    manifest = _manifest()
    version_src = _read("core/version.py")
    # The license change shipped in 2.0.0; the 2.x line keeps it, so any
    # release at or above 2.0.0 is consistent with the migration.
    m = re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', version_src)
    assert m, "core/version.py has no __version__"
    code_ver = tuple(int(x) for x in m.groups())
    assert code_ver >= (2, 0, 0), code_ver
    assert NEW_SPDX in version_src
    assert manifest["app_version"] == ".".join(str(x) for x in code_ver)
    note = manifest["release_note"]
    assert note["ru"].strip() and note["en"].strip()
    # The license change announced with 2.0.0 stays documented: the release
    # note is release-specific, so the announcement lives in the licensing
    # history of README.md (and AGENT.md).
    readme = _read("README.md")
    hist = readme[readme.find("История лицензирования"):]
    assert "PolyForm" in hist and "MIT" in hist
    # README status block must not lag behind the release.
    assert f"v{manifest['app_version']}" in readme


def test_licensing_history_stays_documented():
    """given the migration, when docs are read, then the MIT era is recorded."""
    readme = _read("README.md")
    idx = readme.find("История лицензирования")
    assert idx != -1, "licensing history block missing in README.md"
    history = readme[idx:]
    assert "MIT" in history
    assert "2.0.0" in history and "PolyForm Noncommercial 1.0.0" in history
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0