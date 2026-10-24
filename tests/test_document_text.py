# -*- coding: utf-8 -*-
"""Tests for core.document_text - text extraction from office documents.

Every fixture is generated in a temporary directory, so the tests do not
depend on binary assets and never touch the project tree. Optional parser
packages are imported at fixture creation; a missing package makes the
matching test skip instead of fail, because the module is designed to work
without them.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import document_text as dt  # noqa: E402


# ─── helpers ────────────────────────────────────────────────────────────────


def _write(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _docx_bytes(paragraphs, tables=None) -> bytes:
    docx = pytest.importorskip('docx')
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    for rows in tables or []:
        table = document.add_table(rows=len(rows), cols=len(rows[0]))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.cell(r, c).text = str(value)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes(sheets) -> bytes:
    openpyxl = pytest.importorskip('openpyxl')
    workbook = openpyxl.Workbook()
    default = workbook.active
    workbook.remove(default)
    for title, rows in sheets:
        sheet = workbook.create_sheet(title=title)
        for row in rows:
            sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pptx_bytes(slides) -> bytes:
    pptx = pytest.importorskip('pptx')
    presentation = pptx.Presentation()
    layout = presentation.slide_layouts[5]
    for index, lines in enumerate(slides, start=1):
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = 'Slide %d' % index
        body = slide.shapes.add_textbox(0, 0, 100, 100).text_frame
        for line in lines:
            body.text = line
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


# ─── dispatch and plain text ────────────────────────────────────────────────


def test_text_file_from_utf8_and_cp1251(tmp_path):
    utf8_path = _write(tmp_path, 'note.txt', 'Привет, мир'.encode('utf-8'))
    cp_path = _write(tmp_path, 'legacy.csv', 'значение;1'.encode('cp1251'))

    utf8_result = dt.read_document_path(utf8_path)
    cp_result = dt.read_document_path(cp_path)

    assert utf8_result['ok'] is True
    assert 'Привет, мир' in utf8_result['text']
    assert cp_result['ok'] is True
    assert 'значение;1' in cp_result['text']


def test_unsupported_format_reports_a_warning(tmp_path):
    path = _write(tmp_path, 'archive.bin', b'\x00\x01\x02')
    result = dt.read_document_path(path)

    assert result['ok'] is True
    assert result['text'] == ''
    assert any('unsupported format' in w for w in result['warnings'])


def test_empty_file_is_reported(tmp_path):
    path = _write(tmp_path, 'empty.txt', b'')
    result = dt.read_document_path(path)

    assert result['ok'] is True
    assert result['text'] == ''
    assert any('empty' in w for w in result['warnings'])


def test_missing_file_returns_an_error(tmp_path):
    result = dt.read_document_path(str(tmp_path / 'nope.docx'))

    assert result['ok'] is False
    assert 'not found' in result['error']


def test_is_supported_covers_documents_and_text():
    assert dt.is_supported('report.docx')
    assert dt.is_supported('notes.RTF')
    assert dt.is_supported('data.csv')
    assert not dt.is_supported('photo.png')


# ─── DOCX ───────────────────────────────────────────────────────────────────


def test_docx_keeps_paragraphs_and_renders_tables(tmp_path):
    data = _docx_bytes(
        ['Первый абзац', 'Второй абзац'],
        tables=[[['Раздел', 'Баллы'], ['Семинары', '40']]],
    )
    path = _write(tmp_path, 'program.docx', data)
    result = dt.read_document_path(path)

    assert result['ok'] is True
    assert result['format'] == '.docx'
    assert 'Первый абзац' in result['text']
    assert 'Второй абзац' in result['text']
    assert '[TABLE 1 as CSV]' in result['text']
    assert 'Семинары,40' in result['text']


def test_docx_stream_order_is_paragraphs_then_tables(tmp_path):
    data = _docx_bytes(['До таблицы'], tables=[[['A', 'B']]])
    path = _write(tmp_path, 'order.docx', data)
    text = dt.read_document_path(path)['text']

    assert text.index('До таблицы') < text.index('[TABLE 1 as CSV]')


# ─── XLSX ───────────────────────────────────────────────────────────────────


def test_xlsx_emits_every_sheet_as_csv(tmp_path):
    data = _xlsx_bytes([
        ('Оценки', [['Студент', 'Балл'], ['Иванов', 40]]),
        ('Пусто', []),
    ])
    path = _write(tmp_path, 'grades.xlsx', data)
    result = dt.read_document_path(path)

    assert result['ok'] is True
    assert '### Sheet: Оценки' in result['text']
    assert '### Sheet: Пусто' in result['text']
    assert 'Иванов,40' in result['text']


def test_xlsx_quotes_cells_with_commas(tmp_path):
    data = _xlsx_bytes([('Лист', [['Поле', 'Значение'], ['описание', 'a, b']])])
    path = _write(tmp_path, 'quoted.xlsx', data)
    text = dt.read_document_path(path)['text']

    assert '"a, b"' in text


# ─── PPTX ───────────────────────────────────────────────────────────────────


def test_pptx_marks_every_slide(tmp_path):
    data = _pptx_bytes([['Тезис один'], ['Тезис два']])
    path = _write(tmp_path, 'deck.pptx', data)
    result = dt.read_document_path(path)

    assert result['ok'] is True
    assert '### Slide 1' in result['text']
    assert '### Slide 2' in result['text']
    assert 'Тезис один' in result['text']
    assert 'Тезис два' in result['text']


# ─── RTF / ODT / HTML ───────────────────────────────────────────────────────


def test_rtf_extracts_plain_text(tmp_path):
    rtf = r'{\rtf1\ansi\deff0 {\fonttbl {\f0 Times;}} Курсовая \b работа\b0 .}'
    path = _write(tmp_path, 'legacy.rtf', rtf.encode('cp1251'))
    result = dt.read_document_path(path)

    assert result['ok'] is True
    assert 'Курсовая' in result['text']
    assert 'работа' in result['text']
    assert '\\rtf1' not in result['text']


def test_odt_reads_content_xml(tmp_path):
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content>'
        '<text:p>Первый параграф</text:p>'
        '<text:p>Второй параграф</text:p>'
        '</office:document-content>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('content.xml', content)
    path = _write(tmp_path, 'text.odt', buffer.getvalue())
    text = dt.read_document_path(path)['text']

    assert 'Первый параграф' in text
    assert 'Второй параграф' in text
    assert '/>' not in text


def test_html_drops_markup_and_keeps_lists(tmp_path):
    markup = (
        '<html><head><style>p{color:red}</style></head>'
        '<body><h1>Заголовок</h1><p>Текст &amp; знак</p>'
        '<ul><li>Первый</li><li>Второй</li></ul></body></html>'
    )
    path = _write(tmp_path, 'page.html', markup.encode('utf-8'))
    text = dt.read_document_path(path)['text']

    assert 'Заголовок' in text
    assert 'Текст & знак' in text
    assert '- Первый' in text
    assert 'color:red' not in text
    assert '<h1>' not in text


# ─── windowing ──────────────────────────────────────────────────────────────


def test_window_returns_offset_limit_and_remaining(tmp_path):
    lines = ['строка %d' % i for i in range(1, 11)]
    path = _write(tmp_path, 'lines.txt', '\n'.join(lines).encode('utf-8'))

    first = dt.read_document_path(path, offset=0, limit=4)
    second = dt.read_document_path(path, offset=4, limit=4)

    assert first['total_lines'] == 10
    assert 'строка 1' in first['text']
    assert 'строка 5' not in first['text']
    assert first['remaining'] == 6
    assert 'строка 5' in second['text']
    assert second['remaining'] == 2


def test_max_chars_cuts_at_a_line_boundary(tmp_path):
    lines = ['a' * 20, 'b' * 20, 'c' * 20]
    path = _write(tmp_path, 'wide.txt', '\n'.join(lines).encode('utf-8'))
    result = dt.read_document_path(path, max_chars=45)

    assert result['text'] == 'a' * 20 + '\n' + 'b' * 20
    assert result['remaining'] == 1
    assert any('window cut' in w for w in result['warnings'])


def test_offset_beyond_the_end_returns_empty_text(tmp_path):
    path = _write(tmp_path, 'short.txt', b'one line')
    result = dt.read_document_path(path, offset=99)

    assert result['ok'] is True
    assert result['text'] == ''
    assert result['remaining'] == 0


# ─── malformed payloads ─────────────────────────────────────────────────────


def test_broken_docx_degrades_to_a_warning(tmp_path):
    path = _write(tmp_path, 'broken.docx', b'not a zip at all')
    result = dt.read_document_path(path)

    assert result['ok'] is True
    assert result['text'] == ''
    assert any('DOCX read failed' in w for w in result['warnings'])


def test_broken_pdf_degrades_to_a_warning(tmp_path):
    path = _write(tmp_path, 'broken.pdf', b'%PDF-1.4 nonsense')
    result = dt.read_document_path(path)

    assert result['ok'] is True
    assert any('PDF' in w or 'pdf' in w for w in result['warnings'])


def test_extract_text_never_raises_on_unknown_bytes():
    text, warnings = dt.extract_text('file.docx', b'\x00\x01\x02')

    assert text == ''
    assert warnings


# ─── CSV rendering ──────────────────────────────────────────────────────────


def test_table_to_csv_skips_blank_rows_and_quotes():
    rows = [['header', 'value'], [], [None, 'a"b'], ['x,y', '']]
    rendered = dt.table_to_csv(rows)

    assert rendered.split('\n') == ['header,value', ',"a""b"', '"x,y",']
