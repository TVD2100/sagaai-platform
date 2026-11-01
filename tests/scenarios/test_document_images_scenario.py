# -*- coding: utf-8 -*-
"""tests/scenarios/test_document_images_scenario.py - picture-reading paths.

A user hands over a document whose meaning lives in its pictures: a report
with a chart, a scanned contract. The scenarios walk what the platform
promises for such a file, through the public tool boundary and nothing
internal:

Given a document with a picture and a vision model assigned,
When the orchestrator extracts the pictures and reads them with
    ``analyze_image``,
Then each picture becomes a file it can open, the reply is labelled as
    untrusted data, and no page is read twice.

The vision call is faked: the scenarios measure the plumbing (extraction,
filing, batching, labelling, the no-model path), not provider behaviour.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dev_agent.universal_agent import UniversalDevAgent  # noqa: E402


# ─── fixtures ──────────────────────────────────────────────────────────────


def _png(color: str = "red", size=(400, 300)) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _report_docx() -> bytes:
    """A report whose body is text plus one picture (a chart, say)."""
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Отчёт за квартал")
    document.add_picture(io.BytesIO(_png("navy")))
    document.add_paragraph("Вывод: рост выручки")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _scan_pdf(pages: int = 1) -> bytes:
    """A PDF that is a picture of a page - no text layer at all."""
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    frames = []
    for index in range(pages):
        frame = Image.new("RGB", (600, 400), "white")
        draw = ImageDraw.Draw(frame)
        for line in range(10):
            draw.line([(40, 60 + line * 25), (560, 60 + line * 25)],
                      fill="black", width=2)
        # Each page carries a different mark so the pages are distinct.
        draw.rectangle([20 + index * 40, 340, 120 + index * 40, 380],
                       fill="black")
        frames.append(frame)
    buffer = io.BytesIO()
    frames[0].save(buffer, format="PDF", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated project folder and dialog thread."""
    import core.paths as core_paths

    project = tmp_path / "project"
    project.mkdir()
    history = tmp_path / "history"
    history.mkdir()
    monkeypatch.setattr(core_paths, "HISTORY_DIR", str(history))
    monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", project)
    monkeypatch.setattr("dev_agent.config.TARGET_FILE", "")
    monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "tid-images")
    (history / "tid-images" / "files").mkdir(parents=True)
    return {"project": project, "history": history, "thread": "tid-images"}


@pytest.fixture
def vision_assigned(monkeypatch):
    """A usable vision model, whatever the local settings hold."""
    import core.multimodal as multimodal

    monkeypatch.setattr(multimodal, "resolve_model",
                        lambda kind, config=None: ("S", "m"))
    return multimodal


def _fake_reply(text: str):
    return lambda paths, task, max_tokens=None, temperature=None, config=None: {
        "service": "S", "model": "m", "text": text}


# ─── scenario 1: a report with a chart ─────────────────────────────────────


def test_a_chart_in_a_report_becomes_a_readable_file(workspace, vision_assigned):
    """Given a report with one picture, the tool saves it inside the project."""
    report = workspace["project"] / "report.docx"
    report.write_bytes(_report_docx())
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"path": "report.docx"})

    assert result["ok"] is True
    assert result["count"] == 1
    saved = result["images"][0]
    assert os.path.isfile(saved["path"])
    assert saved["path"].startswith(str(workspace["project"]))
    assert saved["anchor"]["page"] is None
    assert result["batches"] == [[saved["path"]]]


def test_the_picture_is_read_as_labelled_data(workspace, vision_assigned, monkeypatch):
    """Given the saved picture, analyze_image returns fenced, labelled text."""
    report = workspace["project"] / "report.docx"
    report.write_bytes(_report_docx())
    agent = UniversalDevAgent()
    extracted = agent.dispatch("extract_document_images", {"path": "report.docx"})
    picture = extracted["batches"][0]

    monkeypatch.setattr(vision_assigned, "analyze_image",
                        _fake_reply("Выручка: 12 млн, рост 8%"))
    reply = agent.dispatch("analyze_image", {
        "images": picture,
        "prompt": extracted["recommended_prompt"],
    })

    assert reply["ok"] is True
    assert "Выручка: 12 млн" in reply["text"]
    assert "DATA_FROM_IMAGE" in reply["text"]
    assert reply["security"]["untrusted"] is True


def test_the_extraction_prompt_asks_for_transcription(workspace, vision_assigned):
    """The ready-made prompt is a transcription instruction, not a description."""
    report = workspace["project"] / "report.docx"
    report.write_bytes(_report_docx())
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"path": "report.docx"})

    prompt = result["recommended_prompt"]
    assert "verbatim" in prompt
    assert "never an instruction" in prompt


# ─── scenario 2: a scanned contract ────────────────────────────────────────


def test_a_scan_is_detected_and_its_pages_are_not_read_twice(workspace, vision_assigned):
    """Given a scan, its stored page picture is used instead of a re-render."""
    contract = workspace["project"] / "contract.pdf"
    contract.write_bytes(_scan_pdf(1))
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images",
                            {"path": "contract.pdf", "ocr": "auto"})

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["rendered_pages"] == 0
    assert any("were not rendered" in warning for warning in result["warnings"])


def test_forcing_ocr_renders_the_pages_of_a_scan(workspace, vision_assigned):
    """Given ocr='force', every page becomes one picture of that page."""
    contract = workspace["project"] / "contract.pdf"
    contract.write_bytes(_scan_pdf(2))
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images",
                            {"path": "contract.pdf", "ocr": "force"})

    assert result["ok"] is True
    assert result["rendered_pages"] == 2
    anchors = [image["anchor"]["page"] for image in result["images"]
               if image["anchor"]["page"] is not None]
    assert anchors == [1, 2]


def test_an_empty_document_ends_with_an_actionable_next_step(workspace, vision_assigned):
    """Given a file with no pictures and no text layer, the user is told why."""
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    (workspace["project"] / "blank.pdf").write_bytes(buffer.getvalue())
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"path": "blank.pdf"})

    assert result["ok"] is True
    assert result["count"] == 0
    assert "ocr='auto'" in result["next_step"]


# ─── scenario 3: a dialog upload ───────────────────────────────────────────


def test_a_dialog_upload_is_read_by_name(workspace, vision_assigned):
    """Given a document attached to the dialog, file_name finds it."""
    upload = workspace["history"] / workspace["thread"] / "files" / "attach.docx"
    upload.write_bytes(_report_docx())
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"file_name": "attach.docx"})

    assert result["ok"] is True
    assert result["count"] == 1
    assert os.path.isfile(result["images"][0]["path"])


def test_an_unknown_upload_is_refused_with_the_name(workspace, vision_assigned):
    """Given a name that is not attached, the answer names it."""
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"file_name": "ghost.docx"})

    assert result["ok"] is False
    assert "ghost.docx" in result["error"]


# ─── scenario 4: the model is not configured ───────────────────────────────


def test_without_a_vision_model_the_files_are_still_saved(workspace, monkeypatch):
    """Given no vision model, the pictures are saved and the gap is named."""
    import core.multimodal as multimodal

    def _not_assigned(kind, config=None):
        raise multimodal.MultimodalError(
            "not_assigned", "no vision model assigned")

    monkeypatch.setattr(multimodal, "resolve_model", _not_assigned)
    report = workspace["project"] / "report.docx"
    report.write_bytes(_report_docx())
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"path": "report.docx"})

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["recognition_available"] is False
    assert "Settings" in result["next_step"]


def test_analyze_image_without_a_model_explains_the_setting(workspace, monkeypatch):
    """Given no assigned model, analyze_image points at the setting."""
    import core.multimodal as multimodal

    def _not_assigned(kind, config=None):
        raise multimodal.MultimodalError(
            "not_assigned", "No image analysis model is assigned. Choose one "
                            "in Settings → DevAgent.")

    monkeypatch.setattr(multimodal, "resolve_model", _not_assigned)
    agent = UniversalDevAgent()
    image = workspace["project"] / "pic.png"
    image.write_bytes(_png())

    result = agent.dispatch("analyze_image", {"images": ["pic.png"]})

    assert result["ok"] is False
    assert result.get("code") == "not_assigned"
    assert "DevAgent" in result["error"]


# ─── scenario 5: re-running the same document ──────────────────────────────


def test_running_twice_leaves_no_duplicate_files(workspace, vision_assigned):
    """Given the same document twice, the folder holds the same files."""
    report = workspace["project"] / "report.docx"
    report.write_bytes(_report_docx())
    agent = UniversalDevAgent()

    first = agent.dispatch("extract_document_images", {"path": "report.docx"})
    second = agent.dispatch("extract_document_images", {"path": "report.docx"})

    assert first["output_dir"] == second["output_dir"]
    assert first["images"][0]["name"] == second["images"][0]["name"]
    files = os.listdir(first["output_dir"])
    assert len(files) == 1


def test_the_project_tree_keeps_no_image_copies(workspace, vision_assigned):
    """The working files live under .dev_agent, not in the project tree."""
    report = workspace["project"] / "report.docx"
    report.write_bytes(_report_docx())
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"path": "report.docx"})

    assert ".dev_agent" in result["output_dir"]
    top_level = sorted(p.name for p in workspace["project"].iterdir())
    assert "report.docx" in top_level
    assert not any(name.endswith(".png") for name in top_level)


# ─── scenario 6: the tool contract the model sees ──────────────────────────


def test_the_catalog_advertises_the_tool_with_its_arguments():
    """Given the tool list, the model finds the tool and its argument names."""
    from dev_agent.universal_agent import WORKSPACE_TOOL_ARGS

    catalog = {entry["name"]: entry["desc"] for entry in UniversalDevAgent().tool_catalog}
    assert "extract_document_images" in catalog
    assert "analyze_image" in catalog["extract_document_images"]

    spec = WORKSPACE_TOOL_ARGS["extract_document_images"]
    assert spec["required"] == set()
    assert {"path", "file_name", "ocr", "pages", "dpi"} <= spec["optional"]


def test_a_call_without_a_source_is_refused_with_a_hint(workspace):
    """Given neither path nor file_name, the answer names the missing input."""
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"ocr": "auto"})

    assert result["ok"] is False
    assert "path" in result["error"] or "file_name" in result["error"]


def test_an_unknown_argument_is_refused_with_the_signature(workspace):
    """Given a wrong argument name, the answer lists the allowed ones."""
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images",
                            {"path": "a.pdf", "extract_text": True})

    assert result["ok"] is False
    assert result["unknown_args"] == ["extract_text"]
    assert "extract_document_images(" in result["suggestion"]


def test_a_non_document_is_refused_without_a_traceback(workspace, vision_assigned):
    """Given an unsupported file, the tool answers with a reason, not a crash."""
    (workspace["project"] / "notes.txt").write_text("просто текст", encoding="utf-8")
    agent = UniversalDevAgent()

    result = agent.dispatch("extract_document_images", {"path": "notes.txt"})

    assert result["ok"] is False
    assert result["error"]
