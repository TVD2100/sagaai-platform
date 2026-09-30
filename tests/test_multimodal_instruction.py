"""
tests/test_multimodal_instruction.py - the bundled multimodal_mode global
instruction and its seeding into the global instructions store.

The instruction (defaults/instructions/multimodal_mode.md) documents the
analyze_image / generate_image platform tools: usage rules, unassigned-model
handling, provider errors, dependency-install consent and image-borne
prompt-injection safety. These tests cover the file format, the seeding
statuses of ensure_global_instructions and idempotency (user edits are
preserved).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
INSTRUCTION_FILE = ROOT / "defaults" / "instructions" / "multimodal_mode.md"


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point core.paths.DATA_DIR at a temp dir (global instructions are files)."""
    import core.paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_DIR", str(tmp_path))
    return tmp_path


def _load_instruction():
    """Return (meta, body) of the bundled multimodal instruction file."""
    from core.defaults import parse_front_matter
    raw = INSTRUCTION_FILE.read_text(encoding="utf-8")
    return parse_front_matter(raw, default_id="multimodal_mode")


class TestInstructionFile:
    def test_file_exists_with_front_matter(self):
        assert INSTRUCTION_FILE.is_file()
        meta, body = _load_instruction()
        assert meta["id"] == "multimodal_mode"
        assert "Multimodal" in meta["name"]
        # The description is what the model sees in the instructions list.
        assert "analyze_image" in meta["description"]
        assert "generate_image" in meta["description"]
        assert body.strip()

    def test_body_names_tools_and_settings_sections(self):
        _meta, body = _load_instruction()
        assert "analyze_image" in body
        assert "generate_image" in body
        assert "Модель для распознавания изображений" in body
        assert "Модель для генерации изображений" in body

    def test_body_covers_failure_modes_and_safety(self):
        _meta, body = _load_instruction()
        assert "not_assigned" in body
        assert "consent" in body.lower()
        assert "untrusted" in body.lower()
        assert "prompt-injection" in body.lower()


class TestSeeding:
    def test_seeds_into_empty_store(self, isolated_data_dir):
        from core.instructions import ensure_global_instructions, get_instruction

        status = ensure_global_instructions()
        assert status.get("multimodal_mode") == "created"
        inst = get_instruction("multimodal_mode")
        assert inst is not None
        assert inst["name"]
        assert "analyze_image" in inst["text"]

    def test_idempotent_and_preserves_user_edits(self, isolated_data_dir):
        from core.instructions import (
            ensure_global_instructions,
            get_instruction,
            update_instruction,
        )

        ensure_global_instructions()
        assert update_instruction(
            "multimodal_mode", name="My Name", description="edited",
            prompt_text="edited body",
        )
        status = ensure_global_instructions()
        assert status.get("multimodal_mode") == "exists"
        inst = get_instruction("multimodal_mode")
        assert inst["name"] == "My Name"
        assert inst["text"].strip() == "edited body"

    def test_available_to_devagent_without_connections(self, isolated_data_dir,
                                                       monkeypatch):
        from core.instructions import (
            ensure_global_instructions,
            list_instructions_for,
        )
        import core.orchestrators as orch_mod

        ensure_global_instructions()
        # DevAgent with no enabled connections: non-connector global
        # instructions stay available (connector ones would be filtered out).
        monkeypatch.setattr(orch_mod, "get_enabled_connections", lambda slug: [])
        ids = [i["id"] for i in list_instructions_for("dev_agent")]
        assert "multimodal_mode" in ids
