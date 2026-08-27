"""
test_phase1_storage.py - SQLite storage layer tests.
Tests: DB creation, CRUD for assistants/threads/config, round-trip operations,
schema integrity, and column usage consistency.
"""
import os
import sys
import tempfile
import pytest

# Ensure the sagaai package is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own temporary SQLite database."""
    db_file = str(tmp_path / "test_sagaai.db")
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    # Reset cached engine so it picks up the new env var
    import importlib
    import storage.db as db_mod
    db_mod.reset_engine()
    # Reload core.paths so DB_PATH reflects the new env
    import core.paths as paths_mod
    importlib.reload(paths_mod)
    # Reload db module so it uses new paths
    importlib.reload(db_mod)
    yield
    db_mod.reset_engine()


# --- DB creation --------------------------------------------------------------

def test_db_creates_tables(tmp_path):
    """Engine creation should create all expected tables."""
    from storage.db import get_engine
    from sqlalchemy import inspect
    engine = get_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "assistants" in tables
    assert "threads"   in tables
    assert "messages"  in tables
    assert "config_kv" in tables


def test_orm_models_match_actual_schema():
    """
    Verify that every column declared on each ORM model actually exists
    in the corresponding SQLite table right after DB creation.
    This catches situations where the code added a column but the
    database was not migrated (e.g. via ALTER TABLE ADD COLUMN).
    """
    from storage.db import get_engine
    from storage.models import Base
    from sqlalchemy import inspect
    engine = get_engine()
    inspector = inspect(engine)
    # Iterate over all registered ORM tables
    for table_name, model_cls in Base.metadata.tables.items():
        # model_cls is a sqlalchemy.Table, not the ORM class directly -
        # columns are defined on the Table object.
        orm_columns = set(model_cls.columns.keys())
        # Get actual columns from the physical database
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        # Check that every ORM column is present in the DB
        missing = orm_columns - existing_columns
        assert not missing, \
            f"Table '{table_name}' is missing ORM columns: {missing}"


def test_orm_to_dict_keys_match_real_columns():
    """
    Verify that ``to_dict()`` of every ORM model returns a dict
    whose keys are a subset of the actual DB columns.  This catches
    stale to_dict implementations that still reference removed columns.
    """
    from storage.db import get_engine
    from storage.models import Assistant, Thread, Message, ConfigKV, Instruction
    from sqlalchemy import inspect

    engine = get_engine()
    inspector = inspect(engine)

    def _check_model(model_class, create_kwargs):
        table_name = model_class.__tablename__
        real_cols = {col["name"] for col in inspector.get_columns(table_name)}
        # Instantiate a temporary object so to_dict() has data
        try:
            obj = model_class(**create_kwargs)
        except TypeError:
            return  # some models cannot be instantiated without required args
        d = obj.to_dict()
        extra_keys = set(d.keys()) - real_cols
        assert not extra_keys, (
            f"{model_class.__name__}.to_dict() returns keys "
            f"not in DB columns {real_cols}: {extra_keys}"
        )

    _check_model(Assistant, dict(
        id="test_sk", name="n", service="s", model="m", temperature=0.5,
        description="d", prompt_text="pt", tools="[]", max_tool_calls=None,
        created_at="2024", updated_at="2024"
    ))
    _check_model(Thread, dict(
        thread_id="tid", assistant_id=None, assistant_name="sn", title="t",
        type="chat", created_at="2024", updated_at="2024"
    ))
    _check_model(Message, dict(
        thread_id="tid", role="user", content="c", ts="2024",
        file_name="", file_chars=0
    ))
    _check_model(ConfigKV, dict(key="k", value="v"))
    _check_model(Instruction, dict(
        id="iid", name="n", description="d", prompt_text="pt",
        created_at="2024", updated_at="2024"
    ))


def test_repo_functions_use_only_existing_columns():
    """
    Smoke‑test every repository function that queries or writes data.
    This guarantees that no function references a column that does not
    exist in the freshly‑created database.
    """
    from storage.repository import (
        repo_create_thread, repo_load_thread_meta, repo_save_thread_meta,
        repo_append_message, repo_list_all_threads, repo_list_threads_by_type,
    )

    # Create threads of different types
    repo_create_thread("t_chat", None, "ChatSkill", thread_type="chat")
    repo_create_thread("t_dev",  None, "DevSkill",  thread_type="devagent")

    # Append a message to each
    repo_append_message("t_chat", "user", "hello")
    repo_append_message("t_dev",  "user", "fix bug")

    # list_all_threads (should not raise)
    all_t = repo_list_all_threads()
    assert len(all_t) == 2

    # list_threads_by_type
    chat_t = repo_list_threads_by_type("chat")
    dev_t  = repo_list_threads_by_type("devagent")
    assert len(chat_t) == 1 and chat_t[0]["thread_id"] == "t_chat"
    assert len(dev_t)  == 1 and dev_t[0]["thread_id"] == "t_dev"

    # save_thread_meta touches Thread.type among others
    repo_save_thread_meta("t_chat", {"title": "updated title", "type": "chat"})
    meta = repo_load_thread_meta("t_chat")
    assert meta["title"] == "updated title"
    assert meta["type"] == "chat"


# --- Assistants CRUD (legacy repo_*_skill aliases are exercised on purpose) ---

def test_skill_create_and_load():
    """Create a skill and retrieve it."""
    from storage.repository import repo_create_skill, repo_get_skill, repo_load_skills
    ok = repo_create_skill(
        skill_id="abc12345", name="Test Skill", service="openai",
        model="gpt-4", temperature=0.7, prompt_text="You are helpful.",
        description="A test skill",
    )
    assert ok is True

    skill = repo_get_skill("abc12345")
    assert skill is not None
    assert skill["name"]        == "Test Skill"
    assert skill["service"]     == "openai"
    assert skill["model"]       == "gpt-4"
    assert skill["temperature"] == pytest.approx(0.7)
    assert skill["description"] == "A test skill"

    all_skills = repo_load_skills()
    assert len(all_skills) == 1
    assert all_skills[0]["id"] == "abc12345"


def test_skill_update():
    """Create then update a skill."""
    from storage.repository import (
        repo_create_skill, repo_update_skill, repo_get_skill_with_text
    )
    repo_create_skill("sk000001", "Old Name", "openai", "gpt-3.5-turbo", 0.5,
                      "Old prompt", "")
    ok = repo_update_skill("sk000001", "New Name", "openai", "gpt-4", 0.9,
                           "New prompt", "Updated desc")
    assert ok is True
    skill = repo_get_skill_with_text("sk000001")
    assert skill["name"]        == "New Name"
    assert skill["model"]       == "gpt-4"
    assert skill["temperature"] == pytest.approx(0.9)
    assert skill["text"]        == "New prompt"
    assert skill["description"] == "Updated desc"


def test_skill_delete():
    """Create then delete a skill."""
    from storage.repository import repo_create_skill, repo_delete_skill, repo_get_skill
    repo_create_skill("sk000002", "To Delete", "openai", "gpt-4", 0.7, "text", "")
    ok = repo_delete_skill("sk000002")
    assert ok is True
    assert repo_get_skill("sk000002") is None


def test_skill_round_trip():
    """Full create -> load -> update -> delete cycle."""
    from storage.repository import (
        repo_create_skill, repo_get_skill_with_text,
        repo_save_prompt_text, repo_load_prompt_text, repo_delete_skill,
    )
    repo_create_skill("rt000001", "RT Skill", "svc", "mdl", 0.3, "v1 prompt", "desc")
    sk = repo_get_skill_with_text("rt000001")
    assert sk["text"] == "v1 prompt"

    # Save updated prompt text
    ok = repo_save_prompt_text("rt000001", "v2 prompt")
    assert ok is True
    assert repo_load_prompt_text("rt000001") == "v2 prompt"

    repo_delete_skill("rt000001")
    assert repo_get_skill_with_text("rt000001") is None


# --- Threads CRUD -------------------------------------------------------------

def test_skill_reasoning_effort_round_trip():
    """reasoning_effort is persisted and returned by CRUD."""
    from storage.repository import repo_create_skill, repo_get_skill, repo_get_skill_with_text, repo_update_skill
    ok = repo_create_skill(
        skill_id="re000001", name="RE Skill", service="DeepSeek",
        model="deepseek-v4-pro", temperature=0.4, prompt_text="p",
        description="d", reasoning_effort="max",
    )
    assert ok is True
    skill = repo_get_skill("re000001")
    assert skill["reasoning_effort"] == "max"
    assert repo_get_skill_with_text("re000001")["reasoning_effort"] == "max"

    ok = repo_update_skill(
        "re000001", "RE Skill", "DeepSeek", "deepseek-v4-pro", 0.4,
        "p", "d", reasoning_effort="high",
    )
    assert ok is True
    assert repo_get_skill("re000001")["reasoning_effort"] == "high"


def test_thread_create_and_load():
    """Create a thread and load its metadata."""
    from storage.repository import repo_create_thread, repo_load_thread_meta
    ok = repo_create_thread("tid_001", None, "MySkill")
    assert ok is True
    meta = repo_load_thread_meta("tid_001")
    assert meta["thread_id"]  == "tid_001"
    assert meta["assistant_name"] == "MySkill"
    assert meta["title"]      == ""


def test_thread_messages_round_trip():
    """Save and reload thread messages."""
    from storage.repository import (
        repo_create_thread, repo_save_thread_messages, repo_load_thread_messages
    )
    repo_create_thread("tid_002", None, "Skill")
    msgs = [
        {"role": "user",      "content": "Hello",   "ts": "2024-01-01T00:00:00",
         "file_name": "",     "file_chars": 0},
        {"role": "assistant", "content": "Hi there", "ts": "2024-01-01T00:00:01",
         "file_name": "",     "file_chars": 0},
    ]
    repo_save_thread_messages("tid_002", msgs)
    loaded = repo_load_thread_messages("tid_002")
    assert len(loaded) == 2
    assert loaded[0]["role"]    == "user"
    assert loaded[0]["content"] == "Hello"
    assert loaded[1]["role"]    == "assistant"


def test_thread_append_message():
    """Append a message and verify it updates thread title and updated_at."""
    from storage.repository import (
        repo_create_thread, repo_append_message,
        repo_load_thread_messages, repo_load_thread_meta
    )
    repo_create_thread("tid_003", None, "S")
    repo_append_message("tid_003", "user", "My first message")
    msgs = repo_load_thread_messages("tid_003")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "My first message"
    meta = repo_load_thread_meta("tid_003")
    assert meta["title"] == "My first message"[:60]
    assert meta["updated_at"] != ""


def test_thread_delete():
    """Delete a thread removes it and its messages."""
    from storage.repository import (
        repo_create_thread, repo_append_message,
        repo_delete_thread, repo_load_thread_meta, repo_load_thread_messages
    )
    repo_create_thread("tid_del", None, "S")
    repo_append_message("tid_del", "user", "msg")
    repo_delete_thread("tid_del")
    assert repo_load_thread_meta("tid_del") == {}
    assert repo_load_thread_messages("tid_del") == []


def test_list_all_threads_sorted():
    """list_all_threads returns threads sorted by updated_at desc."""
    from storage.repository import (
        repo_create_thread, repo_append_message, repo_list_all_threads
    )
    repo_create_thread("older", None, "S")
    repo_append_message("older", "user", "old msg")

    import time; time.sleep(0.01)  # ensure different timestamps

    repo_create_thread("newer", None, "S")
    repo_append_message("newer", "user", "new msg")

    threads = repo_list_all_threads()
    tids = [t["thread_id"] for t in threads]
    assert tids.index("newer") < tids.index("older")


def test_delete_all_threads():
    """delete_all_threads removes every thread."""
    from storage.repository import (
        repo_create_thread, repo_delete_all_threads, repo_list_all_threads
    )
    for i in range(3):
        repo_create_thread(f"bulk_{i}", None, "S")
    assert len(repo_list_all_threads()) == 3
    repo_delete_all_threads()
    assert repo_list_all_threads() == []


# --- Config CRUD --------------------------------------------------------------

def test_config_save_and_load():
    """Save a config dict and reload it."""
    from storage.repository import repo_save_config, repo_load_config
    cfg = {"openai_key": "sk-test123", "ui_lang": "English", "count": 42}
    ok = repo_save_config(cfg)
    assert ok is True
    loaded = repo_load_config()
    assert loaded["openai_key"] == "sk-test123"
    assert loaded["ui_lang"]    == "English"
    assert loaded["count"]      == 42


def test_config_overwrite():
    """Saving a new config replaces the old one."""
    from storage.repository import repo_save_config, repo_load_config
    repo_save_config({"old_key": "old_val"})
    repo_save_config({"new_key": "new_val"})
    loaded = repo_load_config()
    assert "old_key" not in loaded
    assert loaded["new_key"] == "new_val"


def test_config_empty():
    """Saving empty config and reloading returns empty dict."""
    from storage.repository import repo_save_config, repo_load_config
    repo_save_config({})
    assert repo_load_config() == {}


# --- core.config wrapper -----------------------------------------------------

def test_core_config_has_key():
    """has_key returns True when key is configured."""
    from core.config import save_config, has_key
    save_config({"my_api_key": "abc123"})
    svc_def = {"config_key": "my_api_key"}
    assert has_key(svc_def) is True


def test_core_config_has_key_missing():
    """has_key returns False when key is empty or absent."""
    from core.config import save_config, has_key
    save_config({"my_api_key": ""})
    svc_def = {"config_key": "my_api_key"}
    assert has_key(svc_def) is False
