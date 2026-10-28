"""
test_copyright_headers.py - NFR6: the copyright tail of every base file.

The platform ships a fixed two-line copyright notice at the end of every base
Python file. A new module that misses it, or a file whose tail was edited
away, silently breaks the requirement, and nothing else in the suite looks at
file endings. These tests pin the exact tail, the absence of a BOM and CRLF
line endings, and the inventory of base files published in AGENT.md.
"""
import os
import re
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))


COPYRIGHT_LINES = (
    "# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.",
    "# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0",
)
COPYRIGHT_TAIL = "\n".join(COPYRIGHT_LINES)

# Groups of base files as AGENT.md presents them: (label, paths).
ROOT_FILES = ("__init__.py", "app.py")
BASE_DIRS = ("core", "ui", "storage", "dev_agent")


def _walk_python(directory):
    """Return every .py file under directory, sorted, without __pycache__."""
    found = []
    for root, dirs, files in os.walk(PKG_ROOT / directory):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
                found.append(str(Path(root, name).relative_to(PKG_ROOT)))
    return sorted(p.replace(os.sep, "/") for p in found)


def base_files():
    """Return the base Python files of the platform (the NFR6 scope)."""
    files = [p for p in ROOT_FILES if (PKG_ROOT / p).is_file()]
    for directory in BASE_DIRS:
        files.extend(_walk_python(directory))
    return files


class TestCopyrightTail:
    def test_every_base_file_ends_with_the_notice(self):
        missing = []
        for rel in base_files():
            text = (PKG_ROOT / rel).read_text(encoding="utf-8")
            if not text.rstrip("\n").endswith(COPYRIGHT_TAIL):
                missing.append(rel)
        assert missing == [], "base files without the SPDX tail: %s" % missing

    @pytest.mark.parametrize("line", COPYRIGHT_LINES)
    def test_the_notice_keeps_its_exact_wording(self, line):
        # A reworded notice is a licence problem, not a style choice: the
        # constant above is the only place the wording may live.
        hits = 0
        for rel in base_files():
            if line in (PKG_ROOT / rel).read_text(encoding="utf-8"):
                hits += 1
        assert hits == len(base_files())

    def test_base_files_are_utf8_without_bom_or_crlf(self):
        broken = []
        for rel in base_files():
            raw = (PKG_ROOT / rel).read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                broken.append(rel + " [BOM]")
            if b"\r\n" in raw:
                broken.append(rel + " [CRLF]")
        assert broken == []


class TestAgentInventory:
    """AGENT.md lists the base files; the list must match the disk."""

    def _inventory(self):
        text = (PKG_ROOT / "AGENT.md").read_text(encoding="utf-8")
        head = re.search(r"- Список базовых файлов \((\d+)\):", text)
        assert head is not None, "AGENT.md lost the base-file inventory"
        declared_total = int(head.group(1))
        listed = {}
        rest_lines = text[head.end():].splitlines()
        # The remainder of the header line comes first and is empty.
        for raw in rest_lines[1:]:
            if not raw.startswith("  - "):
                break
            label, _, rest = raw[4:].partition(": ")
            count = int(re.search(r"\((\d+)\)", label).group(1))
            listed[label.split(" (")[0]] = (count, [p.strip() for p in rest.split(",")])
        return declared_total, listed

    def test_the_declared_total_matches_the_disk(self):
        declared_total, listed = self._inventory()
        on_disk = base_files()
        assert declared_total == len(on_disk)
        assert sum(count for count, _ in listed.values()) == len(on_disk)

    def test_every_group_matches_its_files(self):
        _, listed = self._inventory()
        groups = {
            "Корень": [p for p in ROOT_FILES if (PKG_ROOT / p).is_file()],
            "UI": _walk_python("ui"),
            "Storage": _walk_python("storage"),
            "DevAgent": _walk_python("dev_agent"),
        }
        # The core package is split across three groups so that each line
        # stays readable: top-level modules, the marketplace package and the
        # orchestration package.
        core = _walk_python("core")
        groups["Core"] = [p for p in core if p.count("/") == 1]
        groups["Marketplace"] = [p for p in core if p.startswith("core/marketplace/")]
        groups["Orchestration"] = [p for p in core if p.startswith("core/orchestration/")]

        assert set(listed) == set(groups), "unexpected inventory groups: %s" % sorted(listed)
        for label, expected in groups.items():
            count, paths = listed[label]
            assert count == len(expected), label
            assert sorted(paths) == sorted(expected), "%s group is stale" % label

    def test_every_listed_file_exists(self):
        _, listed = self._inventory()
        missing = [p for _, paths in listed.values() for p in paths if not (PKG_ROOT / p).is_file()]
        assert missing == []


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
