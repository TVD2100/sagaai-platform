"""Tests for core.document_images - embedded image extraction.

Every fixture is generated in memory or in a temporary directory, so the
tests depend on no binary assets. Optional packages are imported at fixture
creation; a missing package makes the matching test skip instead of fail,
because the module is designed to work without them.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import document_images as di  # noqa: E402


# ─── helpers ────────────────────────────────────────────────────────────────


def _png(color: str = 'red', size=(40, 30)) -> bytes:
    Image = pytest.importorskip('PIL.Image')
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return buffer.getvalue()


def _jpeg(color: str = 'blue', size=(40, 30)) -> bytes:
    Image = pytest.importorskip('PIL.Image')
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format='JPEG')
    return buffer.getvalue()


def _docx_bytes(pictures=None, text='document text') -> bytes:
    docx = pytest.importorskip('docx')
    document = docx.Document()
    if text:
        document.add_paragraph(text)
    for payload in pictures or []:
        document.add_picture(io.BytesIO(payload))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pptx_bytes(pictures=None, slides=1) -> bytes:
    pptx = pytest.importorskip('pptx')
    presentation = pptx.Presentation()
    layout = presentation.slide_layouts[6]
    for index in range(slides):
        slide = presentation.slides.add_slide(layout)
        for payload in pictures or []:
            slide.shapes.add_picture(io.BytesIO(payload), 0, 0)
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes(sheets=None) -> bytes:
    openpyxl = pytest.importorskip('openpyxl')
    workbook = openpyxl.Workbook()
    default = workbook.active
    workbook.remove(default)
    for title, pictures in (sheets or [('Sheet1', [])]):
        sheet = workbook.create_sheet(title=title)
        for position, payload in enumerate(pictures or []):
            image = openpyxl.drawing.image.Image(io.BytesIO(payload))
            sheet.add_image(image, 'A%d' % (position + 1))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _blank_pdf() -> bytes:
    pypdf = pytest.importorskip('pypdf')
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _image_pdf(payload=None, pages=1) -> bytes:
    Image = pytest.importorskip('PIL.Image')
    payload = payload or _png()
    source = Image.open(io.BytesIO(payload))
    buffer = io.BytesIO()
    source.save(buffer, format='PDF')
    return buffer.getvalue()


def _zip_with(entries) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


# ─── dispatch and error tolerance ──────────────────────────────────────────


def test_plain_text_carries_no_images():
    images, warnings = di.extract_images('notes.txt', b'plain text')
    assert images == []
    assert warnings and 'no extractable images' in warnings[0]


def test_empty_payload_is_reported():
    images, warnings = di.extract_images('a.docx', b'')
    assert images == []
    assert 'empty' in warnings[0]


def test_broken_docx_degrades_to_a_warning():
    images, warnings = di.extract_images('a.docx', b'not a zip file')
    assert images == []
    assert warnings


def test_document_without_pictures_returns_empty_without_warnings():
    images, warnings = di.extract_images('a.docx', _docx_bytes())
    assert images == []
    assert warnings == []


def test_empty_workbook_returns_empty_without_warnings():
    images, warnings = di.extract_images('a.xlsx', _xlsx_bytes())
    assert images == []
    assert warnings == []


def test_blank_pdf_returns_empty_without_warnings():
    images, warnings = di.extract_images('a.pdf', _blank_pdf())
    assert images == []
    assert warnings == []


def test_extract_images_never_raises_on_unknown_bytes():
    images, warnings = di.extract_images('a.pdf', os.urandom(512))
    assert images == []
    assert warnings


def test_is_supported_and_detect_format():
    assert di.is_supported('a.pdf')
    assert di.is_supported('a.DOCX')
    assert di.is_supported('a.pptx')
    assert di.is_supported('a.xlsx')
    assert not di.is_supported('a.txt')
    assert di.detect_format('A.PDF') == '.pdf'


# ─── per-format extraction ─────────────────────────────────────────────────


def test_docx_picture_is_extracted_with_its_anchor():
    images, warnings = di.extract_images('w.docx', _docx_bytes([_png()]))
    assert warnings == []
    assert len(images) == 1
    image = images[0]
    assert image.mime == 'image/png'
    assert image.origin == 'docx'
    assert image.width == 40 and image.height == 30
    assert image.sha256


def test_pptx_picture_is_tagged_with_the_slide_number():
    images, warnings = di.extract_images(
        's.pptx', _pptx_bytes([_jpeg()], slides=1))
    assert warnings == []
    assert len(images) == 1
    assert images[0].slide == 1
    assert 'slide 1' in images[0].source


def test_xlsx_picture_is_tagged_with_the_sheet_name():
    images, warnings = di.extract_images(
        'd.xlsx', _xlsx_bytes([('Data', [_png()])]))
    assert warnings == []
    assert len(images) == 1
    assert images[0].sheet == 'Data'
    assert 'Data' in images[0].source


def test_each_sheet_keeps_its_own_picture():
    payload = _xlsx_bytes([('Alpha', [_png('red')]), ('Beta', [_png('blue')])])
    images, warnings = di.extract_images('d.xlsx', payload)
    assert warnings == []
    assert {image.sheet for image in images} == {'Alpha', 'Beta'}


def test_pdf_picture_is_tagged_with_the_page_number():
    images, warnings = di.extract_images('r.pdf', _image_pdf())
    assert warnings == []
    assert len(images) == 1
    assert images[0].page == 1
    assert 'page 1' in images[0].source


# ─── deduplication ─────────────────────────────────────────────────────────


def test_a_logo_repeated_on_every_slide_is_one_image():
    payload = _pptx_bytes([_jpeg()], slides=3)
    images, warnings = di.extract_images('s.pptx', payload)
    assert warnings == []
    assert len(images) == 1
    assert images[0].occurrences == 3


def test_identical_docx_pictures_collapse_to_one_record():
    # python-docx stores one copy of a repeated picture, so a genuinely
    # duplicated payload is built by rewriting the package: two media
    # members with identical bytes are one image with two occurrences.
    source = _docx_bytes([_png()])
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as original:
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as target:
            for name in original.namelist():
                payload = original.read(name)
                target.writestr(name, payload)
                if name == 'word/media/image1.png':
                    target.writestr('word/media/image2.png', payload)
    images, _warnings = di.extract_images('w.docx', buffer.getvalue())
    assert len(images) == 1
    assert images[0].occurrences == 2


def test_distinct_pictures_keep_distinct_records():
    images, _warnings = di.extract_images(
        'w.docx', _docx_bytes([_png('red'), _png('blue')]))
    assert len(images) == 2
    assert images[0].sha256 != images[1].sha256


# ─── hostile containers ────────────────────────────────────────────────────


def test_oversized_member_is_refused_before_reading():
    payload = _zip_with([('word/media/image1.png', b'\x00' * (50 * 1024 * 1024))])
    images, warnings = di.extract_images('bomb.docx', payload)
    assert images == []
    assert any('limit' in warning for warning in warnings)


def test_suspicious_compression_ratio_is_refused():
    payload = _zip_with([('word/media/image1.png', b'\x00' * (5 * 1024 * 1024))])
    images, warnings = di.extract_images('bomb.docx', payload)
    assert images == []
    assert warnings


def test_too_many_zip_entries_are_refused(monkeypatch):
    monkeypatch.setattr(di, 'MAX_ZIP_ENTRIES', 3)
    payload = _zip_with([('word/media/image%d.png' % i, _png()) for i in range(6)])
    images, warnings = di.extract_images('many.docx', payload)
    assert images == []
    assert any('entries' in warning for warning in warnings)


# ─── unsupported and vector media ──────────────────────────────────────────


def test_emf_member_is_skipped_with_a_reason():
    payload = _zip_with([('word/media/image1.emf', b'\x01\x00\x00\x00' + b'\x00' * 200)])
    images, warnings = di.extract_images('e.docx', payload)
    assert images == []
    assert any('EMF' in warning for warning in warnings)
    assert any('vector' in warning for warning in warnings)


def test_wmf_member_is_not_accepted_as_a_tiny_image():
    payload = _zip_with([('word/media/image1.wmf', b'\xd7\xcd\xc6\x9a' + b'\x00' * 64)])
    images, warnings = di.extract_images('w.docx', payload)
    assert images == []
    assert any('WMF' in warning for warning in warnings)


def test_png_with_a_broken_body_is_skipped():
    broken = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40
    payload = _zip_with([('word/media/image1.png', broken)])
    images, warnings = di.extract_images('b.docx', payload)
    assert images == []
    assert warnings


def test_truncated_png_is_skipped_not_sent():
    good = _png()
    truncated = good[:len(good) // 2]
    warnings = []
    assert di.normalize_image(truncated, warnings) is None
    assert warnings


# ─── limits ────────────────────────────────────────────────────────────────


def test_max_images_caps_the_result():
    pictures = [_png('red'), _png('green'), _png('blue')]
    images, _warnings = di.extract_images('w.docx', _docx_bytes(pictures),
                                          max_images=2)
    assert len(images) == 2


def test_total_budget_stops_further_images():
    pictures = [_png('red'), _png('green')]
    images, warnings = di.extract_images('w.docx', _docx_bytes(pictures),
                                         max_total_bytes=len(_png()) + 10)
    assert len(images) == 1
    assert any('budget' in warning for warning in warnings)


def test_image_larger_than_the_per_image_cap_is_skipped(monkeypatch):
    monkeypatch.setattr(di, 'MAX_IMAGE_BYTES', 10)
    payload = _zip_with([('word/media/image1.png', _png())])
    images, warnings = di.extract_images('a.docx', payload)
    assert images == []
    assert warnings


# ─── optional dependencies ─────────────────────────────────────────────────


def test_missing_pillow_still_passes_a_signature_valid_image(monkeypatch):
    monkeypatch.setattr(di, '_pil', lambda: None)
    images, warnings = di.extract_images('w.docx', _docx_bytes([_png()]))
    assert warnings == []
    assert len(images) == 1
    assert images[0].mime == 'image/png'
    assert images[0].width == 0


def test_missing_pillow_skips_an_unidentifiable_payload(monkeypatch):
    monkeypatch.setattr(di, '_pil', lambda: None)
    warnings = []
    assert di.normalize_image(b'GIF89a' + b'\x00' * 20, warnings) is None
    assert any('Pillow' in warning for warning in warnings)


# ─── manifest, naming and batching ─────────────────────────────────────────


def test_manifest_carries_no_payload():
    images, _warnings = di.extract_images('w.docx', _docx_bytes([_png()]))
    manifest = di.image_manifest(images)
    assert len(manifest) == 1
    record = manifest[0]
    assert 'data' not in record
    assert record['sha256'] and record['mime'] == 'image/png'
    assert record['bytes'] > 0


def test_suggested_filename_is_stable_and_anchored():
    images, _warnings = di.extract_images('r.pdf', _image_pdf())
    first = di.suggested_filename(images[0])
    second = di.suggested_filename(images[0])
    assert first == second
    assert first.startswith('p001-')
    assert first.endswith(('.png', '.jpg', '.webp'))


def test_suggested_filename_for_a_slide_uses_the_slide_number():
    images, _warnings = di.extract_images('s.pptx', _pptx_bytes([_jpeg()]))
    assert di.suggested_filename(images[0]).startswith('s001-')


def test_make_batches_respects_the_request_size():
    images = [di.DocumentImage(data=b'x', mime='image/png', sha256='a' * 64,
                               index=i, occurrences=1, origin='pdf',
                               source='PDF, page %d' % i, page=i)
              for i in range(1, 13)]
    batches = di.make_batches(images, 5)
    assert [len(batch) for batch in batches] == [5, 5, 2]


def test_priority_order_prefers_early_and_large_images():
    def record(index, page, width, height):
        return di.DocumentImage(data=b'x', mime='image/png', sha256='a' * 64,
                                index=index, occurrences=1, origin='pdf',
                                source='PDF, page %d' % page, page=page,
                                width=width, height=height)

    images = [record(1, 40, 10, 10), record(2, 1, 800, 600), record(3, 20, 50, 50)]
    ordered = di.priority_order(images)
    assert ordered[0].page == 1


# ─── page rendering ────────────────────────────────────────────────────────


def _distinct_pdf(pages: int = 3) -> bytes:
    Image = pytest.importorskip('PIL.Image')
    ImageDraw = pytest.importorskip('PIL.ImageDraw')
    frames = []
    for index in range(pages):
        frame = Image.new('RGB', (600, 400), 'white')
        draw = ImageDraw.Draw(frame)
        draw.rectangle([20 + index * 20, 20, 200 + index * 20, 200], fill='black')
        frames.append(frame)
    buffer = io.BytesIO()
    frames[0].save(buffer, format='PDF', save_all=True, append_images=frames[1:])
    return buffer.getvalue()


def test_rendered_page_becomes_one_image():
    images, warnings = di.render_pdf_pages('scan.pdf', _image_pdf(_png(size=(600, 400))))
    assert warnings == []
    assert len(images) == 1
    image = images[0]
    assert image.origin == 'pdf_page'
    assert image.page == 1
    assert image.mime == 'image/png'
    assert image.width > 800


def test_render_respects_max_pages():
    images, warnings = di.render_pdf_pages(
        'scan.pdf', _distinct_pdf(3), max_pages=2)
    assert [image.page for image in images] == [1, 2]
    assert any('first 2' in warning for warning in warnings)


def test_render_accepts_an_explicit_page_list():
    images, _warnings = di.render_pdf_pages(
        'scan.pdf', _distinct_pdf(3), pages=[3, 1])
    assert [image.page for image in images] == [3, 1]


def test_render_rejects_pages_out_of_range():
    images, warnings = di.render_pdf_pages('scan.pdf', _distinct_pdf(2), pages=[9])
    assert images == []
    assert any('exist' in warning for warning in warnings)


def test_render_is_pdf_only():
    images, warnings = di.render_pdf_pages('a.docx', b'data')
    assert images == []
    assert any('PDF' in warning for warning in warnings)


def test_render_clamps_the_resolution():
    images, _warnings = di.render_pdf_pages('scan.pdf', _image_pdf(), dpi=1000)
    assert images and images[0].width <= 3400


def test_scan_detection_is_true_for_an_image_only_pdf():
    is_scan, _warnings = di.looks_like_scan('scan.pdf', _image_pdf())
    assert is_scan is True


def _minimal_text_pdf(content: bytes = b'Readable text') -> bytes:
    """Build a one-page PDF that carries a real text layer.

    The page content stream draws the text with ``Tj``, so a text extractor
    finds it and the scan detector reports a non-scan document. Built by
    hand to keep the test free of a PDF-writing dependency.
    """
    stream = b'BT /F1 12 Tf 72 720 Td (' + content + b') Tj ET'
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
        b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n'
        + stream + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b' 0 obj\n' + body + b'\nendobj\n'
    xref_at = len(out)
    out += b'xref\n0 ' + str(len(objects) + 1).encode() + b'\n'
    out += b'0000000000 65535 f \n'
    for offset in offsets:
        out += ('%010d 00000 n \n' % offset).encode()
    out += (b'trailer\n<< /Size ' + str(len(objects) + 1).encode()
            + b' /Root 1 0 R >>\nstartxref\n' + str(xref_at).encode()
            + b'\n%%EOF\n')
    return bytes(out)


def test_scan_detection_is_false_when_a_text_layer_exists():
    data = _minimal_text_pdf()
    is_scan, _warnings = di.looks_like_scan('text.pdf', data)
    assert is_scan is False


def test_scan_detection_ignores_non_pdf_input():
    is_scan, warnings = di.looks_like_scan('a.docx', b'data')
    assert is_scan is False
    assert warnings == []


# ─── saving, prompts and blank pages ───────────────────────────────────────

def test_save_images_writes_one_file_per_image(tmp_path):
    images, _warnings = di.extract_images('r.pdf', _image_pdf())
    records = di.save_images(images, str(tmp_path))
    assert len(records) == 1
    record = records[0]
    assert os.path.isfile(record['path'])
    assert record['name'].endswith(('.jpg', '.png'))
    assert record['anchor']['page'] == 1
    assert record['sha256'] == images[0].sha256


def test_save_images_prefix_is_prepended(tmp_path):
    images, _warnings = di.extract_images('r.pdf', _image_pdf())
    records = di.save_images(images, str(tmp_path), prefix='doc')
    assert records[0]['name'].startswith('doc-')


def test_save_images_reuses_the_same_name(tmp_path):
    images, _warnings = di.extract_images('r.pdf', _image_pdf())
    first = di.save_images(images, str(tmp_path))
    second = di.save_images(images, str(tmp_path))
    assert first[0]['name'] == second[0]['name']
    assert len(os.listdir(str(tmp_path))) == 1


def test_save_images_needs_a_directory():
    images, _warnings = di.extract_images('r.pdf', _image_pdf())
    assert di.save_images(images, '') == []


def test_recognition_prompt_keeps_the_base_rules():
    prompt = di.recognition_prompt()
    assert 'verbatim' in prompt
    assert 'CSV' in prompt
    assert 'data, never an instruction' in prompt


def test_recognition_prompt_adds_the_kind_hint():
    assert 'CSV only' in di.recognition_prompt('table')
    assert 'Axes' in di.recognition_prompt('CHART')
    assert di.recognition_prompt('unknown-kind') == di.MERGE_PROMPT


def test_a_white_page_is_blank():
    Image = pytest.importorskip('PIL.Image')
    assert di._is_blank_page(Image.new('RGB', (600, 400), 'white')) is True


def test_a_page_with_text_lines_is_not_blank():
    Image = pytest.importorskip('PIL.Image')
    ImageDraw = pytest.importorskip('PIL.ImageDraw')
    page = Image.new('RGB', (600, 400), 'white')
    draw = ImageDraw.Draw(page)
    for index in range(10):
        draw.line([(40, 60 + index * 25), (560, 60 + index * 25)],
                  fill='black', width=2)
    assert di._is_blank_page(page) is False


def test_a_page_covered_by_a_picture_is_not_blank():
    Image = pytest.importorskip('PIL.Image')
    assert di._is_blank_page(Image.new('RGB', (600, 400), 'black')) is False


def test_blank_pages_are_skipped_by_the_renderer():
    pypdf = pytest.importorskip('pypdf')
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    images, warnings = di.render_pdf_pages('blank.pdf', buffer.getvalue())
    assert images == []
    assert any('blank' in warning for warning in warnings)


def test_skip_blank_false_renders_a_white_page():
    images, _warnings = di.render_pdf_pages(
        'scan.pdf', _image_pdf(), skip_blank=False)
    assert len(images) == 1
