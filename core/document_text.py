# -*- coding: utf-8 -*-
"""
core.document_text - text extraction from office documents and PDFs.

A single place that turns a document file into plain text the model can
read: PDF, DOCX, DOC, PPTX, XLSX, XLSM, XLS, RTF, ODT, HTML and the plain
text formats. The module is pure (no network, no Streamlit) and every
extractor is optional-dependency tolerant: a missing package produces a
human-readable warning instead of an exception, so a caller always gets a
result it can report.

Public surface:

- ``extract_text(filename, data)`` -> ``(text, warnings)``: dispatch by
extension. Never raises on a malformed document.
- ``read_document_path(path, ...)`` -> dict: extract a file from disk and
return a line window with pagination metadata.
- ``is_supported(filename)``: True when the extension has an extractor.

Text layout conventions (stable, so callers may rely on them):

- PDF pages are separated by ``### Page N`` markers;
- DOCX paragraphs keep their order, tables are appended as CSV with a
  ``[TABLE n as CSV]`` label;
- PPTX slides get a ``### Slide N`` marker, tables are CSV;
- workbooks emit a ``### Sheet: <name>`` marker and CSV rows per sheet;
- RTF/ODT/HTML collapse to paragraphs without markup.
"""

from __future__ import annotations

import html
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

#: Hard cap on the source file size handled here. Dialog uploads are already
#: capped at 100 MB; the same figure keeps a project file from being slurped
#: into memory without a bound.
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024

#: Cap on the extracted text length kept in memory. A 100 MB PDF can expand
#: into far more text than any model context; the tail is dropped with a
#: warning instead of exhausting memory.
MAX_EXTRACTED_CHARS = 2_000_000

# Formats that are plain text: decoded, never parsed.
TEXT_EXTENSIONS = (
    '.txt', '.md', '.markdown', '.csv', '.tsv', '.log', '.json', '.xml',
    '.yml', '.yaml', '.ini', '.cfg', '.rst', '.py', '.sql',
)

# Formats that need a parser, one per extractor below.
DOCUMENT_EXTENSIONS = (
    '.pdf', '.docx', '.doc', '.pptx', '.xlsx', '.xlsm', '.xls', '.rtf',
    '.odt', '.html', '.htm',
)

SUPPORTED_DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS + DOCUMENT_EXTENSIONS


def detect_format(filename: str) -> str:
    """Return the lowercase extension of *filename* ('' when there is none)."""
    return os.path.splitext(str(filename or '').strip())[1].lower()


def is_supported(filename: str) -> bool:
    """Return True when this module has an extractor for *filename*."""
    return detect_format(filename) in SUPPORTED_DOCUMENT_EXTENSIONS


def _cap(text: str, warnings: List[str]) -> str:
    """Bound the extracted text, recording a warning when the tail is dropped."""
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text
    kept = text[:MAX_EXTRACTED_CHARS]
    warnings.append(
        'text truncated to %d characters; %d characters were dropped'
        % (MAX_EXTRACTED_CHARS, len(text) - MAX_EXTRACTED_CHARS)
    )
    return kept


def decode_text(data: bytes) -> str:
    """Decode plain-text bytes as utf-8, then cp1251, then with replacement."""
    if not data:
        return ''
    for encoding in ('utf-8-sig', 'utf-8', 'cp1251'):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode('utf-8', errors='replace')


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


# ─── PDF ────────────────────────────────────────────────────────────────────


def _pypdf_reader(data: bytes):
    """Return a pypdf reader for the bytes, or None when the package is absent."""
    try:
        import pypdf
        return pypdf.PdfReader(io.BytesIO(data))
    except Exception:
        return None


def _pdf_with_pypdf(data: bytes, warnings: List[str]) -> str:
    """Extract PDF text page by page through pypdf."""
    reader = _pypdf_reader(data)
    if reader is None:
        warnings.append('PDF not parsed: install pypdf')
        return ''
    parts: List[str] = []
    for page_num, page in enumerate(reader.pages, start=1):
        try:
            page_text = (page.extract_text() or '').strip()
        except Exception as exc:
            warnings.append('page %d: %s' % (page_num, exc))
            page_text = ''
        parts.append('### Page %d\n%s' % (page_num, page_text))
    return '\n\n'.join(parts)


def extract_text_from_pdf(data: bytes) -> Tuple[str, List[str]]:
    """Return the text of a PDF, one ``### Page N`` block per page.

    pypdf reads the text layer; a PDF without one (a scan) yields empty
    text and a warning. OCR is out of scope here - a caller that needs the
    page itself as a picture renders it through ``core.document_images``.
    """
    warnings: List[str] = []
    text = _pdf_with_pypdf(data, warnings)
    if not text.strip():
        warnings.append(
            'no text layer found: the PDF may be a scan (image) and OCR is '
            'not supported'
        )
    return text, warnings


# ─── DOCX ───────────────────────────────────────────────────────────────────


def extract_text_from_docx(data: bytes) -> Tuple[str, List[str]]:
    """Return DOCX paragraphs in order plus every table rendered as CSV."""
    from core.ooxml import extract_docx_text
    return extract_docx_text(data)


# ─── DOC (legacy) ───────────────────────────────────────────────────────────


def extract_text_from_doc(data: bytes) -> Tuple[str, List[str]]:
    """Return text of a legacy .doc through antiword, else a best-effort pass.

    antiword needs a real file on disk, so the payload is written to a
    temporary file that is always removed afterwards.
    """
    warnings: List[str] = []
    binary = shutil.which('antiword')
    if not binary:
        warnings.append(
            'antiword is not installed: legacy .doc read in a limited mode; '
            'convert the file to .docx for full text'
        )
        printable = ''.join(
            ch for ch in data.decode('latin-1', errors='replace')
            if ch in '\n\r\t' or ch.isprintable()
        )
        return printable, warnings
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.doc') as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        result = subprocess.run(
            [binary, tmp_path], capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout, warnings
        warnings.append('antiword could not parse the file; limited mode used')
        return '', warnings
    except Exception as exc:
        warnings.append('antiword: %s' % exc)
        return '', warnings
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ─── PPTX ───────────────────────────────────────────────────────────────────


def extract_text_from_pptx(data: bytes) -> Tuple[str, List[str]]:
    """Return PPTX slide text with a ``### Slide N`` marker per slide."""
    from core.ooxml import extract_pptx_text
    return extract_pptx_text(data)


# ─── XLSX / XLS ─────────────────────────────────────────────────────────────


def _xlsx_sheets(data: bytes) -> Tuple[str, List[str]]:
    """Read an .xlsx/.xlsm workbook, one CSV block per sheet."""
    from core.ooxml import extract_xlsx_text
    return extract_xlsx_text(data)


def _xls_sheets(data: bytes) -> Tuple[str, List[str]]:
    """Report that a legacy .xls workbook has no built-in reader.

    A .xls file is an OLE2 compound document wrapping a BIFF8 record stream,
    which no package in the project's dependency set parses. The message
    names the one-step remedy instead of failing silently.
    """
    if not data:
        return '', ['the file is empty']
    return '', [
        'XLS (Excel 97-2003) is not parsed: save the workbook as .xlsx '
        'and upload it again'
    ]


def extract_text_from_excel(data: bytes, filename: str) -> Tuple[str, List[str]]:
    """Dispatch a spreadsheet to openpyxl (.xlsx/.xlsm) or xlrd (.xls)."""
    if detect_format(filename) == '.xls':
        return _xls_sheets(data)
    return _xlsx_sheets(data)


# ─── RTF / ODT / HTML ───────────────────────────────────────────────────────


def extract_text_from_rtf(data: bytes) -> Tuple[str, List[str]]:
    """Return the plain text of an RTF document."""
    raw = decode_text(data)
    try:
        from striprtf.striprtf import rtf_to_text
    except Exception:
        stripped = re.sub(r'\\\'[0-9a-fA-F]{2}|\\[a-zA-Z]+-?[0-9]*\s?', ' ', raw)
        stripped = stripped.replace('{', ' ').replace('}', ' ')
        return re.sub(r'[ \t]{2,}', ' ', stripped).strip(), [
            'striprtf is not installed: RTF converted in a limited mode; '
            'install striprtf for accurate text'
        ]
    try:
        return rtf_to_text(raw), []
    except Exception as exc:
        return '', ['RTF read failed: %s' % exc]


def extract_text_from_odt(data: bytes) -> Tuple[str, List[str]]:
    """Return the text of an OpenDocument file (content.xml of the ZIP)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read('content.xml').decode('utf-8', errors='replace')
    except Exception as exc:
        return '', ['ODT read failed: %s' % exc]
    return _markup_to_text(xml), []


def _markup_to_text(markup: str) -> str:
    """Collapse HTML-like markup into readable plain text.

    Block-level closing tags become newlines, list items become ``- ``
    bullets, table cells are separated by `` | `` and entities are decoded.
    Script and style bodies are dropped before the tag pass.
    """
    text = str(markup or '')
    text = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', text)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)<li[^>]*>', '\n- ', text)
    text = re.sub(r'(?i)</(td|th)>\s*', ' | ', text)
    text = re.sub(r'(?i)</(p|div|h[1-6]|tr|blockquote|section|table|ul|ol)>\s*', '\n\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    lines = [line.rstrip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def extract_text_from_html(data: bytes) -> Tuple[str, List[str]]:
    """Return the visible text of an HTML document."""
    return _markup_to_text(decode_text(data)), []


# ─── Dispatch ───────────────────────────────────────────────────────────────


def extract_text(filename: str, data: bytes) -> Tuple[str, List[str]]:
    """Return ``(text, warnings)`` for a document payload.

    Dispatch is by extension. Unknown formats and unreadable payloads come
    back as empty text with a warning; the function never raises, so a caller
    can always report what happened.
    """
    fmt = detect_format(filename)
    if not data:
        return '', ['the file is empty']
    if len(data) > MAX_DOCUMENT_BYTES:
        return '', ['file is larger than %d bytes' % MAX_DOCUMENT_BYTES]
    if fmt in TEXT_EXTENSIONS:
        return decode_text(data), []
    if fmt == '.pdf':
        text, warnings = extract_text_from_pdf(data)
    elif fmt == '.docx':
        text, warnings = extract_text_from_docx(data)
    elif fmt == '.doc':
        text, warnings = extract_text_from_doc(data)
    elif fmt == '.pptx':
        text, warnings = extract_text_from_pptx(data)
    elif fmt in ('.xlsx', '.xlsm', '.xls'):
        text, warnings = extract_text_from_excel(data, filename)
    elif fmt == '.rtf':
        text, warnings = extract_text_from_rtf(data)
    elif fmt == '.odt':
        text, warnings = extract_text_from_odt(data)
    elif fmt in ('.html', '.htm'):
        text, warnings = extract_text_from_html(data)
    else:
        return '', [
            'unsupported format %r: supported are %s'
            % (fmt or '<none>', ', '.join(SUPPORTED_DOCUMENT_EXTENSIONS))
        ]
    return _cap(text or '', warnings), warnings


def read_document_path(
    path: str,
    offset: int = 0,
    limit: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Extract a document from disk and return a line window of its text.

    Args:
        path: absolute path of the document.
        offset: 0-based first line of the returned window.
        limit: number of lines to return; None returns everything.
        max_chars: optional character cap applied to the window, cut at a
            line boundary.

    Returns a dict with ``ok``, ``path``, ``name``, ``format``, ``bytes``,
    ``text`` (the window), ``total_lines``, ``offset``, ``remaining`` and
    ``warnings``. A missing or unreadable file returns ``ok: False`` with an
    ``error``.
    """
    raw_path = str(path or '').strip()
    if not raw_path:
        return {'ok': False, 'error': 'path must not be empty'}
    resolved = os.path.abspath(os.path.expanduser(raw_path))
    if not os.path.isfile(resolved):
        return {'ok': False, 'error': 'file not found: %s' % resolved,
                'path': resolved}
    try:
        size = os.path.getsize(resolved)
        with open(resolved, 'rb') as handle:
            data = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        return {'ok': False, 'error': 'cannot read file: %s' % exc,
                'path': resolved}
    if size > MAX_DOCUMENT_BYTES:
        return {'ok': False, 'path': resolved, 'bytes': size,
                'error': 'file is larger than %d bytes' % MAX_DOCUMENT_BYTES}

    text, warnings = extract_text(resolved, data)
    lines = text.split('\n') if text else []
    total_lines = len(lines)
    start = max(0, int(offset or 0))
    if start > total_lines:
        start = total_lines
    window = lines[start:] if limit is None else lines[start:start + max(1, int(limit))]
    window_text = '\n'.join(window)
    truncated_chars = False
    if max_chars is not None and max_chars > 0 and len(window_text) > max_chars:
        cut = window_text[:int(max_chars)]
        cut = cut.rsplit('\n', 1)[0] if '\n' in cut else cut
        window_text = cut
        truncated_chars = True
    consumed = len(window_text.split('\n')) if window_text else 0
    remaining = max(0, total_lines - (start + consumed))
    if truncated_chars:
        warnings = list(warnings) + [
            'window cut at %d characters; continue with offset=%d'
            % (int(max_chars or 0), start + consumed)
        ]
    return {
        'ok': True,
        'path': resolved,
        'name': os.path.basename(resolved),
        'format': detect_format(resolved) or '<none>',
        'bytes': size,
        'text': window_text,
        'total_lines': total_lines,
        'offset': start,
        'remaining': remaining,
        'empty': not bool(window_text.strip()),
        'warnings': warnings,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
