# -*- coding: utf-8 -*-
"""tests/test_task_scout_default.py - the bundled Task Scout agent.

Three things are pinned here, because each one can silently rot:

1. The bundled folder is complete and importable - a default that cannot be
   imported is invisible to new installations.
2. The prompt keeps the contract the role depends on: ask the agents first,
   never invent a task, and state the delegation notice verbatim when the
   roster is empty.
3. Both read-only helper functions enforce the SAME consent boundary as
   ``spawn_agent``: the delegation list. A helper that reads any slug would
   quietly widen what the user ticked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.db import reset_devagent_engine, reset_engine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "defaults" / "orchestrators" / "task_scout"
SLUG = "task_scout"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """A fresh data dir so orchestrator rows and memory folders are isolated."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import core.paths as paths_mod
    for attr, value in (("DATA_DIR", str(tmp_path)),
                        ("DB_PATH", str(tmp_path / "sagaai.db")),
                        ("DEVAGENT_DB_PATH", str(tmp_path / "devagent.db")),
                        ("HISTORY_DIR", str(tmp_path / "history")),
                        ("SYSTEM_PROMPTS_DIR", str(tmp_path / "system_prompts")),
                        ("AGENTS_DATA_DIR", str(tmp_path / "agents_data"))):
        monkeypatch.setattr(paths_mod, attr, value, raising=False)

    reset_engine()
    reset_devagent_engine()
    yield tmp_path
    reset_engine()
    reset_devagent_engine()


@pytest.fixture
def scout(platform):
    """Register the bundled Task Scout in the isolated store."""
    from core.defaults import load_default_orchestrator
    from core.orchestrators import import_orchestrator

    data = load_default_orchestrator(SLUG)
    assert data is not None, "the bundled Task Scout folder is unreadable"
    result = import_orchestrator(data, overwrite=False)
    assert result.get("ok") is True, result
    return SLUG


@pytest.fixture
def delegated(scout):
    """Task Scout with two employees ticked in the Delegation section."""
    from core.orchestrators import create_orchestrator, set_allowed_agents

    assert create_orchestrator("helper_one", "Helper One", "first") is not None
    assert create_orchestrator("helper_two", "Helper Two", "second") is not None
    assert set_allowed_agents(scout, ["helper_one", "helper_two"])
    return scout


# ─── the bundled folder ────────────────────────────────────────────────────


class TestBundledFolder:
    def test_the_folder_ships_the_three_required_files(self):
        assert (AGENT_DIR / "orchestrator.json").is_file()
        assert (AGENT_DIR / "system_prompt.md").is_file()
        assert (AGENT_DIR / "functions" / "agent_memory_reader.py").is_file()
        assert (AGENT_DIR / "functions" / "agent_thread_reader.py").is_file()

    def test_the_configuration_is_conservative_by_default(self):
        """No delegation, no bundled executor: the roster is the user's choice."""
        cfg = json.loads((AGENT_DIR / "orchestrator.json").read_text(encoding="utf-8"))
        config = cfg["config"]
        assert config["allowed_agents"] == []
        assert config["universal_agent_enabled"] is False
        assert config["max_active_subagents"] >= 5
        assert config["strong_reasoning_effort"] == "high"
        # The role describes work; it never edits or executes anything.
        assert "propose_file" in cfg["tools"]
        assert "write_doc" not in cfg["tools"]

    def test_the_prompt_keeps_the_role_contract(self):
        prompt = (AGENT_DIR / "system_prompt.md").read_text(encoding="utf-8")
        assert prompt.startswith("# Task Scout - System Prompt (v")
        # Ask first, read second - the defining rule of the role: the numbered
        # principle names asking before reading, and the loop launches the
        # swarm (Stage 2) before the enrichment step (Stage 4).
        assert "ASK FIRST, READ SECOND" in prompt
        assert prompt.index("**Ask the agents.**") < prompt.index("**Read only where")
        assert prompt.index("### Stage 2 - Launch the swarm") < prompt.index(
            "### Stage 4 - Targeted enrichment")
        # The three task kinds and the anti-hallucination rule.
        for kind in ("**open**", "**planned**", "**forecast**"):
            assert kind in prompt
        assert "Never invent a task" in prompt
        # The delegation notice is quoted verbatim, so the runtime text and the
        # prompt text cannot drift apart unnoticed.
        assert "«Делегирование»" in prompt
        assert "мне некого опрашивать" in prompt
        assert "{\"loop_status\"" in prompt or "loop_status" in prompt

    def test_the_default_import_creates_the_agent_and_its_functions(self, platform):
        """A fresh installation must receive Task Scout without any manual step."""
        from core.default_imports import ensure_default_orchestrators
        from core.orchestrator_folders import list_orchestrator_functions
        from core.orchestrators import get_orchestrator

        result = ensure_default_orchestrators()
        assert result.get(SLUG) == "created", result

        record = get_orchestrator(SLUG)
        assert record is not None
        assert record["name_user"] == ""
        assert record["name_en"] == "Task Scout"
        assert record["name_ru"] == "Разведчик задач"
        # The interface language of a test install is Russian.
        assert record["name"] == "Разведчик задач"
        assert record["config"]["allowed_agents"] == []

        names = {item["name"] for item in list_orchestrator_functions(SLUG)}
        assert {"agent_memory_reader", "agent_thread_reader"} <= names

    def test_the_bundled_copy_is_adoptable_by_the_catalog(self):
        """The catalog must recognise an installed default, not offer a second copy.

        Adoption compares the declared files against the folder byte for byte,
        so the shipped folder has to keep exactly the packaged files.
        """
        declared = {"orchestrator.json", "system_prompt.md",
                    "functions/agent_memory_reader.py",
                    "functions/agent_thread_reader.py"}
        present = {
            str(path.relative_to(AGENT_DIR)).replace("\\", "/")
            for path in AGENT_DIR.rglob("*")
            if path.is_file()
            and not path.name.startswith(".")
            and "__pycache__" not in path.parts
        }
        assert present == declared


# ─── the delegation boundary ──────────────────────────────────────────────


class TestMemoryReader:
    def _invoke(self, **kwargs):
        from core.orchestrator_folders import load_all_orchestrator_functions
        functions = load_all_orchestrator_functions(SLUG)
        assert "agent_memory_reader" in functions
        return functions["agent_memory_reader"](**kwargs)

    def test_an_empty_roster_refuses_the_read(self, scout):
        result = self._invoke(slug="helper_one")
        assert result["ok"] is False
        assert result["error_code"] == "no_delegation"

    def test_an_employee_outside_the_roster_is_refused(self, delegated):
        result = self._invoke(slug="stranger")
        assert result["ok"] is False
        assert result["error_code"] == "not_delegated"

    def test_an_empty_folder_is_reported_not_failed(self, delegated, platform):
        result = self._invoke(slug="helper_one", action="list")
        assert result["ok"] is True
        assert result["exists"] is False

    def test_list_and_read_work_inside_the_folder(self, delegated, platform):
        folder = Path(platform) / "agents_data" / "helper_one"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "registry.md").write_text("| id | status |\n| T-1 | open |\n",
                                            encoding="utf-8")

        listing = self._invoke(slug="helper_one", action="list")
        assert listing["ok"] is True
        assert [item["path"] for item in listing["files"]] == ["registry.md"]

        read = self._invoke(slug="helper_one", action="read", path="registry.md")
        assert read["ok"] is True
        assert "T-1" in read["text"]
        assert "Untrusted" in read["note"]

    def test_path_traversal_is_refused(self, delegated, platform):
        folder = Path(platform) / "agents_data" / "helper_one"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "ok.md").write_text("fine", encoding="utf-8")

        for path in ("../helper_two/secret.md", "/etc/passwd", "../../sagaai.db"):
            result = self._invoke(slug="helper_one", action="read", path=path)
            assert result["ok"] is False, path
            assert result["error_code"] == "unsafe_path", path

    def test_a_binary_file_is_not_opened(self, delegated, platform):
        folder = Path(platform) / "agents_data" / "helper_one"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "data.bin").write_bytes(b"\x00\x01\x02")

        result = self._invoke(slug="helper_one", action="read", path="data.bin")
        assert result["ok"] is False
        assert result["error_code"] == "not_text"


class TestThreadReader:
    def _invoke(self, **kwargs):
        from core.orchestrator_folders import load_all_orchestrator_functions
        functions = load_all_orchestrator_functions(SLUG)
        assert "agent_thread_reader" in functions
        return functions["agent_thread_reader"](**kwargs)

    def _thread(self, slug: str, title: str = "dialog") -> str:
        from core.threads_devagent import create_devagent_thread
        from storage.repository_devagent import repo_devagent_append_message

        tid = create_devagent_thread(title=title, orchestrator_slug=slug)
        repo_devagent_append_message(tid, "user", "Сделай отчёт, потом напомни про остаток")
        repo_devagent_append_message(tid, "assistant", "Отчёт готов, остаток проверю позже")
        return tid

    def test_an_empty_roster_refuses_the_read(self, scout):
        result = self._invoke(slug="helper_one")
        assert result["ok"] is False
        assert result["error_code"] == "no_delegation"

    def test_dialogs_of_a_delegated_employee_are_listed(self, delegated):
        tid = self._thread("helper_one", "отчёт по проекту")

        result = self._invoke(slug="helper_one", action="list")
        assert result["ok"] is True
        assert [item["thread_id"] for item in result["threads"]] == [tid]
        assert result["threads"][0]["message_count"] == 2

    def test_search_finds_the_open_phrase(self, delegated):
        self._thread("helper_one")

        result = self._invoke(slug="helper_one", action="search", query="позже")
        assert result["ok"] is True
        assert result["count"] == 1
        assert "позже" in result["hits"][0]["snippet"]

    def test_a_dialog_of_another_employee_is_refused(self, delegated):
        tid = self._thread("helper_two")

        result = self._invoke(slug="helper_one", action="read", thread_id=tid)
        assert result["ok"] is False
        assert result["error_code"] == "not_owner"

    def test_read_returns_the_window_of_the_owner(self, delegated):
        tid = self._thread("helper_one")

        result = self._invoke(slug="helper_one", action="read", thread_id=tid)
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["messages"][0]["role"] == "user"
        assert result["has_more"] is False

    def test_an_unknown_dialog_is_not_found(self, delegated):
        result = self._invoke(slug="helper_one", action="read", thread_id="nope")
        assert result["ok"] is False
        assert result["error_code"] == "not_found"
