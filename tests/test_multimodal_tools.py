"""
tests/test_multimodal_tools.py - tool-level tests for the platform tools
analyze_image / generate_image (dev_agent.tool_executor).

Provider calls are mocked: these tests cover the tool contract (image path
resolution, error mapping, save flow, catalog wiring). The provider
transports themselves are covered by tests/test_multimodal.py.
"""
import base64
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths as core_paths
from core.api_errors import ProviderHTTPError
from dev_agent.tool_executor import TOOL_CATALOG, ToolExecutor

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
JPEG_B64 = base64.b64encode(JPEG_BYTES).decode("ascii")


def _write_image(directory: Path, name: str = "img.jpeg") -> Path:
    """Create a tiny JPEG-like file and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(JPEG_BYTES)
    return path


class TestCatalogWiring:
    def test_tools_are_in_catalog(self):
        names = [t["name"] for t in TOOL_CATALOG]
        assert "analyze_image" in names
        assert "generate_image" in names

    def test_unknown_args_rejected(self):
        te = ToolExecutor()
        result = te.dispatch("analyze_image", {"files": ["a.jpeg"]})
        assert result["ok"] is False
        assert result.get("unknown_args") == ["files"]


class TestAnalyzeImageTool:
    def test_no_images(self):
        te = ToolExecutor()
        result = te.dispatch("analyze_image", {})
        assert result["ok"] is False
        assert "images" in result["error"]

    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "")
        te = ToolExecutor()
        result = te.dispatch("analyze_image", {"images": ["nope.jpeg"]})
        assert result["ok"] is False
        assert "Image not found" in result["error"]

    def test_workspace_image_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "")
        img = _write_image(tmp_path, "photo.jpeg")
        te = ToolExecutor()
        fake = {"service": "S", "model": "m", "text": "A cat", "images": 1}
        with patch("core.multimodal.analyze_image", return_value=fake) as mocked:
            result = te.dispatch(
                "analyze_image",
                {"images": ["photo.jpeg"], "prompt": "What?", "max_tokens": 100})
        assert result["ok"] is True
        assert result["text"] == "A cat"
        assert result["images"] == [str(img)]
        args, kwargs = mocked.call_args
        assert args[0] == [str(img)]
        assert args[1] == "What?"
        assert kwargs["max_tokens"] == 100
        assert kwargs["temperature"] is None

    def test_dialog_upload_by_name(self, tmp_path, monkeypatch):
        history = tmp_path / "history"
        monkeypatch.setattr(core_paths, "HISTORY_DIR", str(history))
        monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", tmp_path / "project")
        monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "tid1")
        img = _write_image(history / "tid1" / "files", "photo.jpeg")
        te = ToolExecutor()
        fake = {"service": "S", "model": "m", "text": "cat", "images": 1}
        with patch("core.multimodal.analyze_image", return_value=fake) as mocked:
            result = te.dispatch("analyze_image", {"images": "photo.jpeg"})
        assert result["ok"] is True
        args, _kwargs = mocked.call_args
        assert args[0] == [str(img)]
        assert args[1] == "Describe the image(s) in detail."

    def test_not_assigned(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "")
        _write_image(tmp_path, "photo.jpeg")
        te = ToolExecutor()
        with patch("core.multimodal.load_devagent_config", return_value={}):
            result = te.dispatch("analyze_image", {"images": ["photo.jpeg"]})
        assert result["ok"] is False
        assert result["code"] == "not_assigned"
        assert "DevAgent" in result["error"]

    def test_provider_error_mapped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", tmp_path)
        monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "")
        _write_image(tmp_path, "photo.jpeg")
        te = ToolExecutor()
        error = ProviderHTTPError(500, "boom", service="S")
        with patch("core.multimodal.analyze_image", side_effect=error):
            result = te.dispatch("analyze_image", {"images": ["photo.jpeg"]})
        assert result["ok"] is False
        assert "boom" in result["error"]


class TestGenerateImageTool:
    def test_empty_prompt(self):
        te = ToolExecutor()
        result = te.dispatch("generate_image", {"prompt": "   "})
        assert result["ok"] is False
        assert "prompt" in result["error"]

    def test_no_thread_and_no_output_path(self, monkeypatch):
        monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "")
        te = ToolExecutor()
        with patch("core.multimodal.generate_image") as mocked:
            result = te.dispatch("generate_image", {"prompt": "cat"})
        assert result["ok"] is False
        assert "output_path" in result["error"]
        mocked.assert_not_called()

    def test_saved_to_dialog_folder(self, tmp_path, monkeypatch):
        history = tmp_path / "history"
        monkeypatch.setattr(core_paths, "HISTORY_DIR", str(history))
        monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "tid1")
        te = ToolExecutor()
        fake = {"service": "YandexAI", "model": "aliceai-image-art-3.0",
                "mime": "image/jpeg", "data": JPEG_B64}
        with patch("core.multimodal.generate_image", return_value=fake) as mocked:
            result = te.dispatch("generate_image", {"prompt": "A red circle"})
        assert result["ok"] is True
        assert "data" not in result
        saved = Path(result["path"])
        assert saved.parent == history / "tid1" / "files"
        assert saved.name.startswith("generated_image_")
        assert saved.read_bytes() == JPEG_BYTES
        mocked.assert_called_once_with("A red circle")

    def test_output_path_inside_project(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", project)
        monkeypatch.setattr("dev_agent.config.ACTIVE_THREAD_ID", "")
        te = ToolExecutor()
        fake = {"service": "S", "model": "m",
                "mime": "image/jpeg", "data": JPEG_B64}
        with patch("core.multimodal.generate_image", return_value=fake):
            result = te.dispatch("generate_image",
                                 {"prompt": "cat", "output_path": "out/pic"})
        assert result["ok"] is True
        saved = project / "out" / "pic.jpeg"
        assert Path(result["path"]) == saved
        assert saved.read_bytes() == JPEG_BYTES

    def test_output_path_escape_rejected(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", project)
        te = ToolExecutor()
        with patch("core.multimodal.generate_image") as mocked:
            result = te.dispatch("generate_image",
                                 {"prompt": "cat", "output_path": "../evil.jpeg"})
        assert result["ok"] is False
        assert "output_path" in result["error"]
        mocked.assert_not_called()

    def test_not_assigned(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setattr("dev_agent.config.PROJECT_ROOT", project)
        te = ToolExecutor()
        with patch("core.multimodal.load_devagent_config", return_value={}):
            result = te.dispatch("generate_image",
                                 {"prompt": "cat", "output_path": "pic.jpeg"})
        assert result["ok"] is False
        assert result["code"] == "not_assigned"
