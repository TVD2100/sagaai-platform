# -*- coding: utf-8 -*-
"""
core.ooxml - text extraction from OOXML packages with the standard library.

DOCX, PPTX and XLSX are ZIP containers holding XML parts, so reading their
text needs no third-party document library: :mod:`zipfile` opens the package
and :mod:`xml.etree.ElementTree` walks the markup. This module exposes the
three extractors plus the shared :func:`table_to_csv` renderer.

Public surface:

- ``extract_docx_text(data)`` -> ``(text, warnings)``: paragraphs in order,
  then every table as CSV under a ``[TABLE n as CSV]`` label;
- ``extract_pptx_text(data)`` -> ``(text, warnings)``: one ``### Slide N``
  block per slide, tables as CSV;
- ``extract_xlsx_text(data)`` -> ``(text, warnings)``: one ``### Sheet:``
  block per worksheet, rows as CSV;
- ``table_to_csv(rows)``: RFC 4180 quoting for a row matrix.

Every extractor is tolerant: a malformed package, a missing member or an XML
parse error yields empty text plus a human-readable warning, never an
exception, so the caller can always report what happened.

Known limits, deliberate because the readers are format-structural rather
than rendered: a workbook cell returns its stored value (number, shared
string, boolean) instead of a formatted date or a formula result, and a
DOCX table cell returns its text without merged-cell expansion.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Dict, List, Optional, Tuple

#: A package larger than this is refused before any member is read.
MAX_PACKAGE_BYTES = 100 * 1024 * 1024

#: Cap on the XML members read from one package.
MAX_PACKAGE_MEMBERS = 4000


def _local(tag: str) -> str:
    """Return the local part of a namespaced XML tag (``{ns}p`` -> ``p``)."""
    if not isinstance(tag, str):
        return ''
    return tag.rsplit('}', 1)[-1]


def table_to_csv(rows: List[List[Any]]) -> str:
    """Render a table (rows of cells) as CSV text.

    Cells are stringified and stripped; a cell containing a comma, a quote or
    a newline is quoted the way RFC 4180 expects. Empty rows are dropped so a
    spreadsheet with trailing blanks does not flood the output.
    """
    out_lines: List[str] = []

    def _cell(value: Any) -> str:
        text = '' if value is None else str(value)
        text = text.strip()
        if any(ch in text for ch in ('"', ',', '\n', '\r')):
            return '"' + text.replace('"', '""') + '"'
        return text

    for row in rows or []:
        cells = [_cell(c) for c in (row or [])]
        if not any(cells):
            continue
        out_lines.append(','.join(cells))
    return '\n'.join(out_lines)


def _open_package(data: bytes, warnings: List[str],
                  label: str = 'OOXML') -> Optional[zipfile.ZipFile]:
    """Open an OOXML package, refusing an oversized or exotic container.

    *label* names the concrete format in the warnings, so a caller reading a
    warning can tell which extractor produced it.
    """
    if not data:
        warnings.append('the file is empty')
        return None
    if len(data) > MAX_PACKAGE_BYTES:
        warnings.append('file is larger than %d bytes' % MAX_PACKAGE_BYTES)
        return None
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:
        warnings.append('%s read failed: %s' % (label, exc))
        return None
    try:
        if len(archive.infolist()) > MAX_PACKAGE_MEMBERS:
            warnings.append(
                'package has more than %d members; only the first ones were '
                'read' % MAX_PACKAGE_MEMBERS
            )
    except Exception:
        pass
    return archive


def _read_xml(archive: zipfile.ZipFile, member: str,
              warnings: List[str]) -> Optional[ET.Element]:
    """Parse one XML member, returning None (with a warning) on any failure."""
    try:
        payload = archive.read(member)
    except KeyError:
        return None
    except Exception as exc:
        warnings.append('%s: %s' % (member, exc))
        return None
    try:
        return ET.fromstring(payload)
    except Exception as exc:
        warnings.append('%s: %s' % (member, exc))
        return None


def _run_text(node: ET.Element) -> str:
    """Collect the visible text of a run container, honouring breaks and tabs."""
    chunks: List[str] = []
    for child in node.iter():
        name = _local(child.tag)
        if name == 't' and child.text:
            chunks.append(child.text)
        elif name == 'tab':
            chunks.append('\t')
        elif name in ('br', 'cr'):
            chunks.append('\n')
    return ''.join(chunks)


# ─── DOCX ───────────────────────────────────────────────────────────────────


def _docx_table_rows(node: ET.Element) -> List[List[str]]:
    """Return the rows of one ``w:tbl`` element as plain strings."""
    rows: List[List[str]] = []
    for tr in node:
        if _local(tr.tag) != 'tr':
            continue
        cells: List[str] = []
        for tc in tr:
            if _local(tc.tag) != 'tc':
                continue
            cells.append(_run_text(tc).strip())
        if any(cells):
            rows.append(cells)
    return rows


def extract_docx_text(data: bytes) -> Tuple[str, List[str]]:
    """Return DOCX paragraphs in order plus every table rendered as CSV."""
    warnings: List[str] = []
    archive = _open_package(data, warnings, 'DOCX')
    if archive is None:
        return '', warnings
    root = _read_xml(archive, 'word/document.xml', warnings)
    if root is None:
        return '', warnings or ['DOCX read failed: word/document.xml is missing']

    body = None
    for node in root.iter():
        if _local(node.tag) == 'body':
            body = node
            break
    if body is None:
        return '', ['DOCX read failed: no document body']

    parts: List[str] = []
    table_number = 0
    for child in body:
        name = _local(child.tag)
        if name == 'p':
            text = _run_text(child).strip()
            if text:
                parts.append(text)
        elif name == 'tbl':
            rows = _docx_table_rows(child)
            if not rows:
                continue
            table_number += 1
            parts.append('[TABLE %d as CSV]\n%s' % (table_number, table_to_csv(rows)))
    return '\n'.join(parts), warnings


# ─── PPTX ───────────────────────────────────────────────────────────────────


def _pptx_slide_members(names: List[str]) -> List[str]:
    """Return the slide members ordered by their numeric suffix."""
    def _key(name: str) -> int:
        match = re.search(r'slide(\d+)\.xml$', name)
        return int(match.group(1)) if match else 0

    slides = [n for n in names if re.match(r'ppt/slides/slide\d+\.xml$', n)]
    return sorted(slides, key=_key)


def _pptx_table_rows(node: ET.Element) -> List[List[str]]:
    """Return the rows of one ``a:tbl`` element as plain strings."""
    rows: List[List[str]] = []
    for tr in node:
        if _local(tr.tag) != 'tr':
            continue
        cells: List[str] = []
        for tc in tr:
            if _local(tc.tag) != 'tc':
                continue
            cells.append(_run_text(tc).strip())
        if any(cells):
            rows.append(cells)
    return rows


def _pptx_slide_blocks(root: ET.Element) -> Tuple[List[str], List[ET.Element]]:
    """Split one slide into its text paragraphs and its tables.

    A paragraph inside a table belongs to the table, so the parent chain is
    walked to keep those out of the free-text stream.
    """
    parents: Dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[child] = parent

    def _inside_table(node: ET.Element) -> bool:
        current = parents.get(node)
        while current is not None:
            if _local(current.tag) == 'tbl':
                return True
            current = parents.get(current)
        return False

    paragraphs: List[str] = []
    tables: List[ET.Element] = []
    for node in root.iter():
        name = _local(node.tag)
        if name == 'tbl':
            tables.append(node)
        elif name == 'p' and not _inside_table(node):
            text = _run_text(node).strip()
            if text:
                paragraphs.append(text)
    return paragraphs, tables


def extract_pptx_text(data: bytes) -> Tuple[str, List[str]]:
    """Return PPTX slide text with a ``### Slide N`` marker per slide."""
    warnings: List[str] = []
    archive = _open_package(data, warnings, 'PPTX')
    if archive is None:
        return '', warnings
    try:
        names = archive.namelist()
    except Exception as exc:
        return '', ['PPTX read failed: %s' % exc]
    slides = _pptx_slide_members(names)
    if not slides:
        return '', ['PPTX read failed: no slides found']

    parts: List[str] = []
    for slide_number, member in enumerate(slides, start=1):
        root = _read_xml(archive, member, warnings)
        if root is None:
            parts.append('### Slide %d\n' % slide_number)
            continue
        paragraphs, tables = _pptx_slide_blocks(root)
        block = list(paragraphs)
        for table_node in tables:
            rows = _pptx_table_rows(table_node)
            if rows:
                block.append('[TABLE on slide %d as CSV]\n%s'
                             % (slide_number, table_to_csv(rows)))
        parts.append('### Slide %d\n%s' % (slide_number, '\n'.join(block)))
    return '\n\n'.join(parts), warnings


# ─── XLSX ───────────────────────────────────────────────────────────────────


def _column_index(reference: str) -> int:
    """Return the 0-based column of a cell reference such as ``B7``."""
    letters = ''
    for ch in str(reference or ''):
        if ch.isalpha():
            letters += ch.upper()
        else:
            break
    if not letters:
        return -1
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord('A') + 1)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    """Return the workbook shared-string table (an empty list when absent)."""
    warnings: List[str] = []
    root = _read_xml(archive, 'xl/sharedStrings.xml', warnings)
    if root is None:
        return []
    strings: List[str] = []
    for item in root:
        if _local(item.tag) != 'si':
            continue
        strings.append(''.join(
            node.text or '' for node in item.iter() if _local(node.tag) == 't'
        ))
    return strings


def _cell_value(cell: ET.Element, shared: List[str]) -> str:
    """Return one cell's stored value as text."""
    kind = (cell.get('t') or '').lower()
    raw = ''
    inline: List[str] = []
    for node in cell:
        name = _local(node.tag)
        if name == 'v':
            raw = node.text or ''
        elif name == 'is':
            inline.append(''.join(
                part.text or '' for part in node.iter() if _local(part.tag) == 't'
            ))
    if kind == 's':
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ''
    if kind == 'inlinestr':
        return ''.join(inline)
    if kind == 'b':
        return 'TRUE' if raw.strip() == '1' else 'FALSE'
    if kind == 'e':
        return ''
    return raw


def _sheet_rows(archive: zipfile.ZipFile, path: str, shared: List[str],
                warnings: List[str]) -> List[List[str]]:
    """Return the rows of one worksheet as lists of cell strings."""
    root = _read_xml(archive, path, warnings)
    if root is None:
        return []
    rows: List[List[str]] = []
    for row_node in root.iter():
        if _local(row_node.tag) != 'row':
            continue
        cells: Dict[int, str] = {}
        for position, cell in enumerate(row_node):
            if _local(cell.tag) != 'c':
                continue
            index = _column_index(cell.get('r') or '')
            if index < 0:
                index = position
            cells[index] = _cell_value(cell, shared)
        if not cells:
            continue
        rows.append([cells.get(i, '') for i in range(max(cells) + 1)])
    return rows


def _workbook_sheets(archive: zipfile.ZipFile,
                     warnings: List[str]) -> List[Tuple[str, str]]:
    """Return ``(sheet name, member path)`` pairs in workbook order."""
    targets: Dict[str, str] = {}
    rels_root = _read_xml(archive, 'xl/_rels/workbook.xml.rels', warnings)
    if rels_root is not None:
        for node in rels_root.iter():
            if _local(node.tag) != 'Relationship':
                continue
            rel_id = node.get('Id') or ''
            target = node.get('Target') or ''
            if rel_id and target:
                targets[rel_id] = target

    sheets: List[Tuple[str, str]] = []
    workbook_root = _read_xml(archive, 'xl/workbook.xml', warnings)
    if workbook_root is not None:
        for node in workbook_root.iter():
            if _local(node.tag) != 'sheet':
                continue
            name = node.get('name') or ''
            rel_id = ''
            for key, value in node.attrib.items():
                if _local(key) == 'id':
                    rel_id = value
                    break
            target = targets.get(rel_id, '')
            if target.startswith('/'):
                path = target.lstrip('/')
            elif target:
                path = 'xl/' + target.lstrip('./')
            else:
                path = ''
            sheets.append((name, path))

    if not sheets:
        try:
            names = archive.namelist()
        except Exception:
            names = []
        sheets = [(name.rsplit('/', 1)[-1], name) for name in sorted(names)
                  if name.startswith('xl/worksheets/') and name.endswith('.xml')]
        if sheets:
            warnings.append('workbook order unavailable: worksheets read in name order')
    return sheets


def extract_xlsx_text(data: bytes) -> Tuple[str, List[str]]:
    """Read an .xlsx/.xlsm workbook, one CSV block per sheet."""
    warnings: List[str] = []
    archive = _open_package(data, warnings, 'XLSX')
    if archive is None:
        return '', warnings
    shared = _shared_strings(archive)
    sheets = _workbook_sheets(archive, warnings)
    if not sheets:
        return '', warnings or ['XLSX read failed: no worksheets found']

    parts: List[str] = []
    for name, path in sheets:
        rows = _sheet_rows(archive, path, shared, warnings) if path else []
        parts.append('### Sheet: %s\n%s' % (name, table_to_csv(rows)))
    return '\n\n'.join(parts), warnings
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
