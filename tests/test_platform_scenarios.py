# -*- coding: utf-8 -*-
"""tests.test_platform_scenarios - end-to-end user scenarios of the SagaAI platform.

Each test replays a real user workflow against the platform layers
(core / storage / dev_agent) with every LLM/HTTP call mocked. Scenario
coverage is mapped to the SPEC functional requirements:

    SC1  assistant lifecycle ................. FR2, FR4
    SC2  chat with a model + attachments ..... FR1
    SC3  employee orchestrator ............... FR6
    SC4  DevAgent edit cycle ................. FR7
    SC5  universal developer ................. FR8
    SC6  RAG knowledge base .................. FR10
    SC7  i18n and fallbacks .................. FR3
    SC8  settings, secrets, connection ....... FR5
    SC9  prompt improvement .................. FR2
    SC10 API round trips ..................... FR1

Isolation: every scenario uses a fresh temporary DATA_DIR (SQLite + folders)
so real user data is never touched.
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from dev_agent import config as dev_config
from dev_agent.backup_manager import BackupManager
from dev_agent.tool_executor import ToolExecutor
from dev_agent.universal_agent import UniversalDevAgent


@pytest.fixture
def isolated_data(monkeypatch):
    """Temporary DATA_DIR isolating the DBs, folders and recent workspaces.

    Reloads core.paths / storage.db under isolation so import-time constants
    always come from the temporary folder. Real provider env-keys are removed
    so scenarios control every credential via mocks.
    """
    tmp = tempfile.mkdtemp(prefix="sagaai_test_scenarios_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    for var in ("SAGAAI_DEEPSEEK_KEY", "SAGAAI_YANDEXAI_KEY",
                "SAGAAI_YANDEXAI_KEY2", "SAGAAI_GIGACHAT_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SAGAAI_DATA_DIR", tmp)

    from tests._test_isolation import isolated_app_modules as _iso_app_modules
    with _iso_app_modules():
        import core.paths as paths_mod
        importlib.reload(paths_mod)
        import storage.db as db_mod
        importlib.reload(db_mod)
        db_mod.reset_engine()
        db_mod.reset_devagent_engine()

        yield tmp

        db_mod.reset_engine()
        db_mod.reset_devagent_engine()

    if old_env:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def devagent_sandbox(tmp_path, monkeypatch):
    """Redirect DevAgent runtime state into a temp project folder."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    monkeypatch.setattr(dev_config, "PROJECT_ROOT", root)
    monkeypatch.setattr(dev_config, "BACKUPS_DIR", root / "dev_agent" / "backups")
    monkeypatch.setattr(dev_config, "WORKSPACE_DIR", root / "dev_agent" / "workspace")
    monkeypatch.setattr(dev_config, "TASK_STATES_DIR", root / "dev_agent" / "task_states")
    monkeypatch.setattr(dev_config, "CHANGELOG_FILE", root / "CHANGELOG.md")
    monkeypatch.setattr(dev_config, "PROTECTED_FILES", ())
    monkeypatch.setattr(dev_config, "WORKING_ON_INSTALL", False)
    dev_config.ensure_runtime_dirs()
    (root / "src" / "module.py").write_text(
        "def greet(name):\n    return 'Hi ' + name\n", encoding="utf-8"
    )
    return root


# ──────────────────────────────────────────────────────────────────────────────
# SC1 - assistant lifecycle (FR2, FR4)
# ──────────────────────────────────────────────────────────────────────────────


def test_assistant_full_lifecycle_scenario(isolated_data):
    """Create -> files/prompt on disk+DB -> chat thread -> edit -> delete."""
    import core.assistant_folders as af
    from core.assistants import (
        create_assistant, get_assistant_by_id, update_assistant,
        delete_assistant, load_assistant_files_context,
    )
    from core.threads import (
        create_thread, append_thread_message, load_thread_messages,
        list_chat_threads, delete_thread, sum_thread_tokens,
        messages_to_api_history,
    )

    # 1. Create an assistant.
    pid = create_assistant(
        "Редактор текста", "DeepSeek", "deepseek-v4-flash", 0.6,
        "## Роль\nТы - редактор текста.",
        description="Проверяет стиль и ошибки",
        tools=["read_file"], max_tool_calls=3, max_tokens=2048,
    )
    assert pid is not None

    # 2. The DB record and the folder/slug are consistent.
    full = get_assistant_by_id(pid)
    assert full["text"] == "## Роль\nТы - редактор текста."
    slug = full["slug"]
    # Cyrillic names are transliterated into a readable Latin slug.
    assert slug == "redaktor_teksta"
    assert af.assistant_folder_exists(slug)
    assert af.load_assistant_prompt(slug) == "## Роль\nТы - редактор текста."
    manifest = af.load_assistant_bundle(slug)
    assert manifest["tools"] == ["read_file"]
    assert manifest["max_tool_calls"] == 3

    # 3. Attachment files live in the folder and build chat context.
    assert af.save_assistant_file(slug, "правила", "Используй живой, ясный язык.")
    assert af.list_assistant_files(slug) == ["правила.txt"]
    ctx = load_assistant_files_context(pid)
    assert "### File: правила" in ctx
    assert "Используй живой, ясный язык." in ctx

    # 4. A chat thread records messages, tokens and appears in history.
    tid = create_thread(pid, full["name"], title="Проверь текст")
    append_thread_message(tid, "user", "Проверь этот текст",
                          tokens={"in": 120, "out": 0})
    append_thread_message(tid, "assistant", "Всё хорошо",
                          tokens={"in": 0, "out": 80})
    msgs = load_thread_messages(tid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert sum_thread_tokens(msgs)[:2] == (120, 80)
    api_hist = messages_to_api_history(msgs)
    assert api_hist[0]["role"] == "user"
    assert any(t["thread_id"] == tid for t in list_chat_threads())

    # 5. Editing resyncs the folder copy.
    assert update_assistant(
        pid, "Редактор текста", "DeepSeek", "deepseek-v4-flash", 0.7,
        "## Роль\nТы - строгий редактор.",
        tools=["read_file"], max_tool_calls=4,
    )
    assert af.load_assistant_prompt(slug) == "## Роль\nТы - строгий редактор."
    assert af.load_assistant_bundle(slug)["max_tool_calls"] == 4

    # 6. Delete thread, then the assistant; disk and DB are cleaned up.
    delete_thread(tid)
    assert not any(t["thread_id"] == tid for t in list_chat_threads())
    assert delete_assistant(pid) is True
    assert not af.assistant_folder_exists(slug)
    assert get_assistant_by_id(pid) is None


# ──────────────────────────────────────────────────────────────────────────────
# SC2 - chat with a model + attachments + token budgeting (FR1)
# ──────────────────────────────────────────────────────────────────────────────


def test_chat_with_model_and_files_scenario(isolated_data):
    """A full chat round trip: file context is forwarded to the provider."""
    from unittest.mock import MagicMock, patch

    from core.files import (
        estimate_tokens, check_upload_tokens, build_attachments_context,
    )
    from core.api_layer import send_request

    # Token helpers used by the chat page before sending.
    att, names = build_attachments_context([
        {"name": "data.txt", "content": "цифры продаж за квартал"},
    ])
    assert "data.txt" in att and "цифры продаж" in att
    assert names == "data.txt"
    assert estimate_tokens("hello") > 0
    assert check_upload_tokens("Привет")[0] is True
    assert check_upload_tokens("x" * 600_000, max_tokens=100)[0] is False

    assistant = {
        "id": "x", "text": "Ты - аналитик.",
        "service": "YandexAI", "model": "yandexgpt-5-lite",
        "temperature": 0.3, "tools": [],
    }
    svc = {"YandexAI": {
        "auth_type": "yandex_iam",
        "base_url": "https://ai.api.cloud.yandex.net/v1",
        "config_key": "yandex_iam_token",
        "config_key2": "yandex_cloud_id",
        "temp_default": 0.3,
    }}
    cfg = {"yandex_iam_token": "token", "yandex_cloud_id": "folder"}

    with patch("core.api_layer.get_services", return_value=svc), \
         patch("core.api_layer.load_config", return_value=cfg), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "output_text": "## Ответ\n```python\nprint(42)\n```",
        }
        mock_post.return_value = mock_resp

        result = send_request(
            "Напиши код для отчёта", assistant,
            file_context="**Файл: data.txt**\nцифры продаж за квартал",
        )
        assert result == "## Ответ\n```python\nprint(42)\n```"
        # The attached file content must reach the provider payload.
        sent = json.dumps(mock_post.call_args[1]["json"], ensure_ascii=False)
        assert "data.txt" in sent
        assert "цифры продаж" in sent


# ──────────────────────────────────────────────────────────────────────────────
# SC3 - employee orchestrator (FR6)
# ──────────────────────────────────────────────────────────────────────────────


def test_employee_orchestrator_scenario(isolated_data):
    """Create an employee, attach instructions/functions/RAG, run agent views."""
    from core import rag
    from core.orchestrators import (
        create_orchestrator, build_assistant_dicts, get_economy_config,
        set_orchestrator_rag_bases, get_orchestrator_rag_bases,
        export_orchestrator, import_orchestrator, delete_orchestrator,
    )
    import core.orchestrator_folders as of

    base = rag.create_base(
        "Platform Docs", description="Справочник платформы",
        provider="YandexAI", embedding_model="text-search-doc",
        chunk_size=200, chunk_overlap=20,
    )
    base_slug = base["slug"]
    assert base_slug == "platform_docs"

    oid = create_orchestrator(
        "review_bot", "Review Bot", "Проверяет код и документацию",
        prompt_text="Ты - код-ревьюер.",
        config={
            "strong_service": "DeepSeek", "strong_model": "deepseek-v4-pro",
            "strong_temperature": 0.2,
            "weak_service": "DeepSeek", "weak_model": "deepseek-v4-flash",
            "weak_temperature": 0.4,
            "search_service": "YandexAI", "search_model": "aliceai-llm-flash",
            "search_temperature": 0.3, "search_max_tool_calls": 1,
            "economy_tail_messages": 12,
            "economy_cache_enabled": True,
            "economy_cache_multiplier": 4,
        },
        tools=["read_file", "list_files", "current_workspace"],
        max_steps=25, auto_apply=True,
    )
    assert oid is not None
    assert of.orchestrator_folder_exists("review_bot")
    bundle = of.load_orchestrator_bundle("review_bot")
    assert bundle["tools"] == ["read_file", "list_files", "current_workspace"]
    assert "код-ревьюер" in of.load_orchestrator_prompt_file("review_bot")

    # Attach an orchestrator-specific instruction and a custom function.
    assert of.save_orchestrator_instruction(
        "review_bot", "style_guide", "Style Guide",
        "Правила стиля", "Используй стандарт PEP8.",
    )
    instrs = of.list_orchestrator_instructions("review_bot")
    assert any(i["id"] == "style_guide" for i in instrs)

    code = "def invoke(**kwargs):\n    return {\"status\": \"ok\"}\n"
    assert of.save_orchestrator_function("review_bot", "status_check", code)
    fn = of.load_orchestrator_function_module("review_bot", "status_check")
    assert fn is not None and fn() == {"status": "ok"}

    # Assign a RAG base and verify the agent views carry everything.
    assert set_orchestrator_rag_bases("review_bot", [base_slug])
    assert get_orchestrator_rag_bases("review_bot") == [base_slug]

    strong, weak = build_assistant_dicts("review_bot")
    assert strong["service"] == "DeepSeek"
    assert strong["model"] == "deepseek-v4-pro"
    assert weak["model"] == "deepseek-v4-flash"
    assert "код-ревьюер" in strong["text"]
    assert "Style Guide" in strong["text"]
    assert base_slug in strong["text"]

    # Economy settings and DevAgent-thread persistence for the employee.
    assert get_economy_config("review_bot") == {
        "tail_messages": 12, "cache_enabled": True, "cache_multiplier": 4,
    }

    from core.threads_devagent import (
        create_devagent_thread, append_thread_message, save_thread_workspace,
        load_thread_meta, load_thread_messages, list_orchestrator_threads,
        delete_thread,
    )
    tid = create_devagent_thread(
        "Проверь проект", orchestrator_slug="review_bot",
        orchestrator_name="Review Bot", workspace="/tmp/project",
    )
    append_thread_message(
        tid, "user", "Привет",
        events=[{"type": "tool_call", "tool": "read_file", "args": {"path": "a.py"}}],
        tokens={"in": 10, "out": 0},
    )
    assert save_thread_workspace(tid, "/tmp/project2") is True
    assert load_thread_meta(tid)["workspace"] == "/tmp/project2"
    msgs = load_thread_messages(tid)
    assert msgs[0]["_events"][0]["tool"] == "read_file"
    threads = list_orchestrator_threads("review_bot")
    assert threads and threads[0]["thread_id"] == tid
    delete_thread(tid)

    # Export/import round trip.
    data = export_orchestrator("review_bot")
    assert data["format"] == "sagaai_orchestrator/v1"
    assert any(i["id"] == "style_guide" for i in data["instructions"])
    assert "status_check" in data["functions"]
    imported = import_orchestrator(data, overwrite=False)
    assert imported["ok"] is True
    assert imported["slug"] == "review_bot_2"

    # Cleanup (built-in dev_agent is protected from deletion, this one is not).
    assert delete_orchestrator("review_bot_2") is True
    assert delete_orchestrator("review_bot") is True
    assert not of.orchestrator_folder_exists("review_bot")
    assert rag.delete_base(base_slug) is True
    assert rag.get_base(base_slug) == {}


# ──────────────────────────────────────────────────────────────────────────────
# SC4 - DevAgent edit cycle (FR7)
# ──────────────────────────────────────────────────────────────────────────────


def test_devagent_edit_cycle_scenario(devagent_sandbox):
    """propose_file -> verify -> apply_patch fallback -> backups -> task state."""
    te = ToolExecutor()

    # 1. Full-file rewrite lands on disk immediately with a backup.
    new_content = "def greet(name):\n    return 'Hello ' + name\n"
    create = te.propose_file(path="src/module.py", content=new_content)
    assert create["ok"], create
    assert create["applied"] is True
    assert create["verified"] is True
    assert create["backup_version"] == 1
    assert (devagent_sandbox / "src" / "module.py").read_text(
        encoding="utf-8") == new_content

    # 2. Verify the applied change.
    ok = te.verify_file(
        path="src/module.py",
        expected_substrings=["Hello"],
        unexpected_substrings=["'Hi '"],
    )
    assert ok["ok"] is True
    assert ok["missing_expected"] == [] and ok["present_unexpected"] == []

    # 3. Surgical patch: a missing anchor fails loudly without touching the file.
    fail = te.apply_patch("src/module.py", [{"old": "def goodbye", "new": "def bye"}])
    assert fail["ok"] is False
    assert (devagent_sandbox / "src" / "module.py").read_text(
        encoding="utf-8") == new_content

    # 4. The corrected anchor applies.
    patched = te.apply_patch(
        "src/module.py",
        [{"old": "return 'Hello ' + name", "new": "return 'Hello, ' + name"}],
    )
    assert patched["ok"] is True and patched["applied"] is True
    assert patched["replacements"] == 1

    # 5. Every write produced a backup version; rollback works.
    bm = BackupManager()
    hist = bm.history_summary("src/module.py")
    assert hist["total_versions"] >= 2
    original_content = "def greet(name):\n    return 'Hi ' + name\n"
    restored = bm.restore_backup("src/module.py", version=1)
    assert restored.version == 1
    # v1 is the pre-first-edit snapshot: restoring brings back the original.
    assert (devagent_sandbox / "src" / "module.py").read_text(
        encoding="utf-8") == original_content

    # 6. External task memory lifecycle.
    init = te.task_state_init(
        task="Сценарий", plan="### Step 1 - Первый\n- verification: none",
    )
    assert init["ok"] and init["step_ids"] == ["step_1"]
    marked = te.task_state_mark_step("step_1", status="done", verification="ok")
    assert marked["ok"]
    read = te.task_state_read()
    assert read["sections"]["task"] == "Сценарий"
    assert "- [x] Step 1 - Первый" in read["sections"]["progress"]
    cleared = te.task_state_clear()
    assert cleared["ok"] and cleared["archived"]
    read_after = te.task_state_read()
    assert read_after["exists"], "journal file must never be deleted"
    assert len(read_after["history"]) == 1
    assert read_after["history"][0]["task"] == "Сценарий"


# ──────────────────────────────────────────────────────────────────────────────
# SC5 - universal developer on an external project (FR8)
# ──────────────────────────────────────────────────────────────────────────────


def test_universal_developer_external_project_scenario(tmp_path, isolated_data):
    """Analyze an external folder, write docs, snapshot/restore, single-file mode."""
    import dev_agent.workspace_tools as wt

    ext = tmp_path / "external_proj"
    (ext / "subpkg").mkdir(parents=True)
    (ext / "main.py").write_text(
        "import helper\n\n\ndef main():\n    return helper.answer()\n",
        encoding="utf-8",
    )
    (ext / "helper.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8"
    )

    agent = UniversalDevAgent()
    old_root = dev_config.PROJECT_ROOT
    try:
        # Switch to the external project.
        res = agent.dispatch("set_workspace", {"path": str(ext)})
        assert res["ok"]
        assert dev_config.PROJECT_ROOT.resolve() == ext.resolve()

        assess = agent.dispatch("assess_workspace", {})
        assert assess["state"] == "software_without_docs"
        assert assess["code_files"] == 2

        pm = agent.dispatch("build_project_map", {})
        assert pm["file_count"] == 2
        entry = next(e for e in pm["entries"] if e["path"] == "main.py")
        assert entry["depends_on"] == ["helper"]

        wrote_map = agent.dispatch(
            "write_project_map",
            {"responsibilities": {"main.py": "Entry point", "helper.py": "Helper"}},
        )
        assert wrote_map["ok"] and (ext / "PROJECT_MAP.md").exists()
        assert agent.dispatch(
            "write_doc", {"doc": "spec", "content": "# Spec\n\nExternal project.\n"}
        )["ok"]
        assert agent.dispatch("assess_workspace", {})["state"] == "software_with_docs"

        # Editing and whole-project snapshot -> restore.
        created = agent.dispatch(
            "propose_file", {"path": "subpkg/note.txt", "content": "hello"}
        )
        assert created.get("ok")
        snap = agent.dispatch("snapshot_all", {"note": "baseline"})
        assert snap["ok"]
        (ext / "subpkg" / "note.txt").write_text("MUTATED", encoding="utf-8")
        restored = agent.dispatch("restore_all", {"snapshot_id": snap["snapshot_id"]})
        assert restored["ok"]
        assert (ext / "subpkg" / "note.txt").read_text(encoding="utf-8") == "hello"

        # Single-file mode narrows the view to one file.
        agent.dispatch("set_workspace", {"path": str(ext)})
        sf = agent.dispatch("set_target_file", {"file_path": str(ext / "helper.py")})
        assert sf["ok"] and sf["single_file_mode"] is True
        cur = agent.dispatch("current_workspace", {})
        assert cur["target_file"] == str(ext / "helper.py")
        scan = agent.dispatch("scan_folder", {})
        assert scan["code_files"] == 1
    finally:
        # Restore the platform workspace AND clear single-file mode so later
        # tests never inherit TARGET_FILE pointing at a deleted temp file.
        wt.set_workspace(str(old_root))
        dev_config.set_target_root(old_root)


# ──────────────────────────────────────────────────────────────────────────────
# SC6 - RAG knowledge base (FR10)
# ──────────────────────────────────────────────────────────────────────────────


def test_rag_base_full_scenario(isolated_data, monkeypatch):
    """Create base -> add files -> index (mocked embeddings) -> search -> delete."""
    from core import rag
    import core.rag_indexer as indexer
    import core.rag_search as rs

    # 1. Create the base; folder + manifest + empty index appear.
    base = rag.create_base(
        "Platform Docs", description="Справочник платформы",
        provider="YandexAI", embedding_model="text-search-doc",
        chunk_size=200, chunk_overlap=20,
    )
    slug = base["slug"]
    assert slug == "platform_docs"
    assert base["status"] == "draft"
    assert base["index_stats"]["chunks"] == 0

    # 2. Upload a source document long enough to produce 3 chunks at size 200.
    body = (
        b"# Guide\n\n"
        b"SagaAI platform guide. "
        b"The platform combines assistants, skills and RAG bases. "
        b"It stores data in SQLite for persistence. "
        b"This first sentence is already quite long and should be its own chunk.\n\n"
        b"Second paragraph about assistants. "
        b"Assistants are configured with prompts and models. "
        b"They operate on the selected workspace and produce answers for the user.\n\n"
        b"Third paragraph about indexing. "
        b"Chunks are split by paragraphs and sentences to keep boundaries clean. "
        b"Embeddings are computed per chunk and stored in the index.\n\n"
    )
    info = rag.add_file(slug, "guide.txt", body)
    assert info["path"] == "guide.txt" and info["size"] == len(body)
    assert rag.list_files(slug) == [{"path": "guide.txt", "size": len(body)}]
    assert rag.read_file_contents(slug, "guide.txt").startswith("# Guide")
    # No provider credentials in the isolated config -> inactive base.
    assert rag.base_has_credentials(slug) is False

    # 3. Index with synthetic embeddings (no network). index_base imports
    # embed_text from core.rag_embeddings at call time, so the patch targets
    # that module, not rag_indexer.
    import core.rag_embeddings as emb
    fake_vector = [1.0, 0.0, 0.0]
    monkeypatch.setattr(
        emb, "embed_text",
        lambda text, model=emb.DOC_EMBEDDING_MODEL, dimension=256,
               api_key=None, folder_id=None: list(fake_vector),
    )
    monkeypatch.setattr(
        indexer, "get_yandex_embedding_credentials", lambda: ("key", "folder")
    )
    ready = indexer.index_base(slug)
    assert ready["status"] == "ready"
    assert ready["index_stats"]["chunks"] >= 1

    # 4. Chunk-level inspection and edit (embedding gets invalidated).
    page = rag.list_chunks(slug, limit=10)
    assert page["total"] >= 2
    cid = page["chunks"][0]["chunk_id"]
    assert rag.get_chunk(slug, cid)["has_embedding"] is True
    out = rag.update_chunk(slug, cid, "Updated platform chunk text")
    assert out["ok"] is True and out["reembedded"] is False
    assert "embedding" in out["warning"].lower()
    assert rag.get_chunk(slug, cid)["has_embedding"] is False

    # 5. Semantic search (query embedding mocked, cosine score local).
    monkeypatch.setattr(rs, "embed_query", lambda text: list(fake_vector))
    hits = rs.search_base(slug, "platform")
    assert hits and hits[0]["source"] == "guide.txt"
    ctx = rs.build_search_context(hits)
    assert "score" in ctx and "guide.txt" in ctx
    chat_ctx = rs.chat_context(slug, "platform")
    assert "Материалы из базы знаний" in chat_ctx

    # 6. Cleanup: chunk, file, then the whole base.
    assert rag.delete_chunk(slug, cid) is True
    assert rag.delete_chunk(slug, cid) is False
    assert rag.remove_file(slug, "guide.txt") is True
    assert rag.list_files(slug) == []
    assert rag.delete_base(slug) is True
    assert rag.get_base(slug) == {}


# ──────────────────────────────────────────────────────────────────────────────
# SC7 - i18n and fallback chain (FR3)
# ──────────────────────────────────────────────────────────────────────────────


def test_i18n_scenario(isolated_data):
    """Discover languages, translate, fall back to English, then to the key."""
    from core.i18n import get_langs, t, invalidate_langs_cache, discover_langs

    langs = get_langs()
    assert langs
    assert "English" in langs

    # The canonical English file resolves known keys.
    assert t("btn_send", "English") != "btn_send"
    assert t("api_error", "English", error="boom") == "API error: boom"

    # Russian and Chinese resolve too (or fall back to English).
    ru_name = next((n for n, p in langs.items() if p.endswith("ru.json")), None)
    assert ru_name, "Russian language file must be discovered"
    assert isinstance(t("btn_send", ru_name), str) and t("btn_send", ru_name)

    zh_name = next((n for n, p in langs.items() if p.endswith("zh-CN.json")), None)
    assert zh_name, "Chinese language file must be discovered"
    assert isinstance(t("btn_send", zh_name), str) and t("btn_send", zh_name)

    # A key missing everywhere returns the key itself.
    assert t("no_such_key_xyz", "English") == "no_such_key_xyz"

    # The discover cache invalidates cleanly and re-scans.
    invalidate_langs_cache()
    assert get_langs()
    assert isinstance(discover_langs(), dict)


# ──────────────────────────────────────────────────────────────────────────────
# SC8 - settings, secrets and connection test (FR5)
# ──────────────────────────────────────────────────────────────────────────────


def test_config_secrets_and_connection_scenario(isolated_data, monkeypatch):
    """Secrets are encrypted at rest; env keys win; connection test works."""
    from unittest.mock import MagicMock, patch

    from core.config import (
        save_config, load_config, has_key, list_env_keys,
        is_env_key_set_for_service,
    )
    from storage.repository import repo_load_config

    # 1. Save; secret keys are encrypted, plain keys stay plain.
    assert save_config({
        "openai_key": "plain-value",
        "DEEPSEEK_API_KEY": "sk-super-secret",
    }) is True
    cfg = load_config()
    assert cfg["openai_key"] == "plain-value"
    assert cfg["DEEPSEEK_API_KEY"] == "sk-super-secret"
    raw = repo_load_config()
    assert raw["openai_key"] == "plain-value"
    assert raw["DEEPSEEK_API_KEY"] != "sk-super-secret"

    # 2. Environment variables override stored values.
    monkeypatch.setenv("SAGAAI_DEEPSEEK_KEY", "env-key")
    assert load_config()["DEEPSEEK_API_KEY"] == "env-key"
    assert is_env_key_set_for_service("DeepSeek", "config_key") is True
    info = list_env_keys()
    assert info["DeepSeek"]["env_wins"] is True
    assert info["DeepSeek"]["db_value_masked"] == "***"

    # has_key reflects the configured key.
    assert has_key({"config_key": "DEEPSEEK_API_KEY"}) is True

    # 3. The settings page can test a live connection (mocked HTTP).
    from core.api_layer import test_connection
    svc = {"DeepSeek": {
        "auth_type": "bearer",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "config_key": "DEEPSEEK_API_KEY",
        "models": [{"id": "deepseek-v4-pro"}],
    }}
    with patch("core.api_layer.get_services", return_value=svc), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_post.return_value = mock_resp
        ok_conn, msg = test_connection("DeepSeek", {"DEEPSEEK_API_KEY": "env-key"})
        assert ok_conn is True
        assert "200" in msg


# ──────────────────────────────────────────────────────────────────────────────
# SC9 - prompt improvement (FR2)
# ──────────────────────────────────────────────────────────────────────────────


def test_prompt_improvement_scenario(isolated_data):
    """Weak-model prompt improvement with injectable instructions + sender."""
    from core.prompt_improver import improve_prompt_with_weak_model

    def fake_send(user_message, assistant, file_context="", history=None,
                  lang=None, **kwargs):
        # The improvement instruction replaces the assistant system prompt.
        assert assistant["text"].strip().startswith("# Правила")
        assert "Improve" in user_message
        return "## Роль\nУлучшенный промпт."

    inst = "# Правила\nВозвращай только улучшенный текст."
    improved = improve_prompt_with_weak_model(
        "Ты - переводчик.", send_request_fn=fake_send, instruction_text=inst,
    )
    assert improved == "## Роль\nУлучшенный промпт."

    # Empty inputs are rejected before any LLM call.
    with pytest.raises(ValueError):
        improve_prompt_with_weak_model(
            "", send_request_fn=fake_send, instruction_text=inst,
        )
    with pytest.raises(ValueError):
        improve_prompt_with_weak_model(
            "Ты - переводчик.", send_request_fn=fake_send, instruction_text="",
        )


# ──────────────────────────────────────────────────────────────────────────────
# SC10 - API round trips (FR1)
# ──────────────────────────────────────────────────────────────────────────────


def test_api_roundtrip_scenarios(isolated_data):
    """Yandex and DeepSeek send_request round trips + error classification."""
    from unittest.mock import MagicMock, patch

    from core.api_layer import send_request
    from core.api_errors import ApiKeyMissingError

    # --- YandexAI round trip -----------------------------------------------
    assistant = {
        "id": "1", "text": "Х", "service": "YandexAI",
        "model": "yandexgpt-5-lite", "temperature": 0.3, "tools": [],
    }
    svc_y = {"YandexAI": {
        "auth_type": "yandex_iam",
        "base_url": "https://ai.api.cloud.yandex.net/v1",
        "config_key": "yandex_iam_token",
        "config_key2": "yandex_cloud_id",
    }}
    cfg_y = {"yandex_iam_token": "token", "yandex_cloud_id": "folder"}
    with patch("core.api_layer.get_services", return_value=svc_y), \
         patch("core.api_layer.load_config", return_value=cfg_y), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output_text": "Привет из Яндекса"}
        mock_post.return_value = mock_resp
        assert send_request("Привет", assistant) == "Привет из Яндекса"
        assert "/responses" in mock_post.call_args[0][0]

    # --- DeepSeek Responses round trip -------------------------------------
    assistant_ds = {
        "id": "2", "text": "Х", "service": "DeepSeek",
        "model": "deepseek-v4-pro", "temperature": 0.8,
        "tools": ["web_search"],
    }
    svc_ds = {"DeepSeek": {
        "auth_type": "deepseek_responses",
        "base_url": "https://api.deepseek.com/responses",
        "config_key": "DEEPSEEK_API_KEY",
    }}
    cfg_ds = {"DEEPSEEK_API_KEY": "sk-x"}
    with patch("core.api_layer.get_services", return_value=svc_ds), \
         patch("core.api_layer.load_config", return_value=cfg_ds), \
         patch("core.api_layer.load_skill_files_context", return_value=""), \
         patch("core.api_layer._deepseek_responses_request",
               return_value="DeepSeek answer") as mock_ds_req:
        assert send_request("Вопрос", assistant_ds) == "DeepSeek answer"
        assert mock_ds_req.called

    # --- Missing-key classification ----------------------------------------
    with patch("core.api_layer.get_services", return_value=svc_ds), \
         patch("core.api_layer.load_config",
               return_value={"DEEPSEEK_API_KEY": ""}), \
         patch("core.api_layer.load_skill_files_context", return_value=""):
        with pytest.raises(ApiKeyMissingError) as exc_info:
            send_request("Вопрос", assistant_ds)
        assert exc_info.value.service == "DeepSeek"
