# -*- coding: utf-8 -*-
"""
Tests for core.personal_memory - the personal memory layer.

Covers scope parsing, path resolution inside DATA_DIR, roundtrip persistence,
limits, credential refusal, atomicity, backups, access control and the
connection-memory adapter that keeps the existing connector file untouched.
"""
import os

import pytest

import core.paths


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp directory so memory never touches real data."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    yield tmp_path


# ─── Scope parsing ────────────────────────────────────────────────────────────


def test_parse_scope_user(isolated_data_dir):
    import core.personal_memory as pm
    assert pm.parse_scope("user") == ("user", "")
    assert pm.parse_scope("USER") == ("user", "")
    assert pm.format_scope("user") == "user"


def test_parse_scope_orchestrator_normalises_slug(isolated_data_dir):
    import core.personal_memory as pm
    kind, key = pm.parse_scope("orchestrator:Dev Agent")
    assert kind == "orchestrator"
    assert key == "dev_agent"
    assert pm.format_scope(kind, key) == "orchestrator:dev_agent"


def test_parse_scope_project_hashes_path(isolated_data_dir):
    import core.personal_memory as pm
    kind, key = pm.parse_scope("project:/tmp/my project")
    assert kind == "project"
    assert len(key) == 12
    assert key == pm.project_key("/tmp/my project")
    # The same folder written differently yields the same key.
    assert pm.project_key("/tmp/my project/") == key


def test_parse_scope_rejects_garbage(isolated_data_dir):
    import core.personal_memory as pm
    for bad in ("", "   ", "unknown", "orchestrator:", "orchestrator:...",
                "connector:", "connector:../etc"):
        with pytest.raises(ValueError):
            pm.parse_scope(bad)


# ─── Path containment ─────────────────────────────────────────────────────────


def test_all_scope_paths_stay_inside_data_dir(isolated_data_dir):
    import core.personal_memory as pm
    root = str(isolated_data_dir)
    scopes = ["user", "orchestrator:dev_agent", "project:/tmp/some/folder",
              "connector:abcd1234"]
    for scope in scopes:
        path = pm.memory_path(scope)
        assert os.path.commonpath([os.path.abspath(path), root]) == root


# ─── Roundtrip ────────────────────────────────────────────────────────────────


def test_read_missing_scope_is_not_an_error(isolated_data_dir):
    import core.personal_memory as pm
    result = pm.read_memory("orchestrator:dev_agent")
    assert result["ok"] is True
    assert result["exists"] is False
    assert result["content"] == ""
    assert pm.memory_exists("orchestrator:dev_agent") is False


def test_write_read_roundtrip(isolated_data_dir):
    import core.personal_memory as pm
    body = "## Notes\n- Тесты гоняются через pytest -q.\n- Эмодзи: \u2705"
    saved = pm.write_memory("orchestrator:dev_agent", body)
    assert saved["ok"] is True
    assert saved["revision"] == 1

    result = pm.read_memory("orchestrator:dev_agent")
    assert result["exists"] is True
    assert "pytest -q" in result["content"]
    assert "\u2705" in result["content"]
    assert result["revision"] == 1
    assert result["updated_by"] == "agent"
    assert result["updated_at"]


def test_write_bumps_revision_and_keeps_header(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("user", "## Rules\n- Никогда не коммитить без тестов.")
    pm.write_memory("user", "## Rules\n- Никогда не коммитить без тестов.\n## Notes\n- Отчёты короткие.")
    result = pm.read_memory("user")
    assert result["revision"] == 2
    assert result["content"].startswith("# Personal memory: user")
    assert "updated_by: agent" in result["content"]


def test_user_write_marks_human_author(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("user", "## Notes\n- Правка человека.", updated_by="user")
    assert pm.read_memory("user")["updated_by"] == "user"


def test_max_chars_truncates(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("user", "## Notes\n" + "- строка заметки\n" * 100)
    full = pm.read_memory("user")
    short = pm.read_memory("user", max_chars=50)
    assert short["truncated"] is True
    assert len(short["content"]) == 50
    assert full["truncated"] is False
    assert len(full["content"]) > 50
    # ``bytes`` always reports the stored size, so the caller sees the real cost.
    assert full["bytes"] == short["bytes"] > 50


# ─── Limits ───────────────────────────────────────────────────────────────────


def test_oversized_write_is_rejected_and_file_unchanged(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("user", "## Notes\n- короткая заметка")
    before = pm.read_memory("user")["content"]
    huge = "## Notes\n" + ("x" * (pm.PERSONAL_MEMORY_MAX_BYTES + 100))
    with pytest.raises(ValueError, match="too large"):
        pm.write_memory("user", huge)
    assert pm.read_memory("user")["content"] == before


# ─── Credential screen ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "токен: ghp_abcdefghijklmnopqrstuvwx",
    "sk-abcdefghijklmnopqrstuvwx",
    "-----BEGIN RSA PRIVATE KEY-----",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    "password = supersecret123",
    "api_key: abcdef123456789",
])
def test_write_refuses_credential_like_text(isolated_data_dir, text):
    import core.personal_memory as pm
    with pytest.raises(ValueError, match="credential"):
        pm.write_memory("user", "## Notes\n- " + text)
    assert pm.memory_exists("user") is False


def test_refusal_message_does_not_echo_the_secret(isolated_data_dir):
    import core.personal_memory as pm
    secret = "ghp_abcdefghijklmnopqrstuvwx"
    with pytest.raises(ValueError) as info:
        pm.write_memory("user", secret)
    assert secret not in str(info.value)


def test_ordinary_notes_pass_the_screen(isolated_data_dir):
    import core.personal_memory as pm
    text = ("## Environment\n- Проекты лежат в ~/Projects.\n"
            "- Пароль спрашивать у владельца, не хранить здесь.")
    assert pm.detect_secret(text) == ""
    assert pm.write_memory("user", text)["saved"] is True


# ─── Atomicity, backups, archive ──────────────────────────────────────────────


def test_no_temporary_files_left_behind(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("orchestrator:dev_agent", "## Notes\n- заметка")
    folder = os.path.dirname(pm.memory_path("orchestrator:dev_agent"))
    leftovers = [n for n in os.listdir(folder) if ".tmp-" in n]
    assert leftovers == []


def test_backup_created_on_overwrite_and_rotated(isolated_data_dir):
    import core.personal_memory as pm
    scope = "orchestrator:dev_agent"
    pm.write_memory(scope, "## Notes\n- v1")
    folder = pm._service_dir(".backups", scope)
    assert not os.path.isdir(folder) or os.listdir(folder) == []

    for index in range(pm.PERSONAL_BACKUPS_KEPT + 3):
        pm.write_memory(scope, f"## Notes\n- версия {index}")

    files = sorted(n for n in os.listdir(folder) if n.endswith(".md"))
    assert len(files) == pm.PERSONAL_BACKUPS_KEPT


def test_delete_moves_file_to_archive(isolated_data_dir):
    import core.personal_memory as pm
    scope = "orchestrator:dev_agent"
    pm.write_memory(scope, "## Notes\n- заметка")
    assert pm.delete_memory(scope) is True
    assert pm.memory_exists(scope) is False
    archive = pm._service_dir(".archive", scope)
    assert len([n for n in os.listdir(archive) if n.endswith(".md")]) == 1
    assert pm.delete_memory(scope) is False


# ─── Access control ───────────────────────────────────────────────────────────


def test_employee_cannot_touch_another_employee_memory(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("orchestrator:second", "## Notes\n- чужая заметка")
    with pytest.raises(PermissionError):
        pm.read_memory("orchestrator:second", caller_slug="first")
    with pytest.raises(PermissionError):
        pm.write_memory("orchestrator:second", "подмена", caller_slug="first")
    # The owner of the scope still reaches its own notes.
    assert pm.read_memory("orchestrator:second", caller_slug="second")["exists"] is True


def test_employee_may_write_own_and_user_scope(isolated_data_dir):
    import core.personal_memory as pm
    pm.check_access("orchestrator:first", caller_slug="first", write=True)
    pm.check_access("user", caller_slug="first", write=True)
    pm.check_access("project:/tmp/x", caller_slug="first", write=True)


def test_agent_cannot_write_connection_memory(isolated_data_dir):
    import core.personal_memory as pm
    with pytest.raises(PermissionError):
        pm.check_access("connector:abcd1234", caller_slug="first", write=True)


# ─── Connection adapter (existing file, existing contract) ────────────────────


def test_connection_scope_reads_the_existing_connector_file(isolated_data_dir):
    import core.connectors as c
    import core.personal_memory as pm
    created = c.create_connection("github_rest", "GitHub", "tok")
    conn_id = created["id"]
    c.set_connection_memory(conn_id, "Endpoint: api.github.com")

    scope = f"connector:{conn_id}"
    assert pm.memory_path(scope) == os.path.join(
        core.paths.DATA_DIR, "connectors", conn_id, "memory.md"
    )
    result = pm.read_memory(scope)
    assert result["exists"] is True
    assert result["content"] == "Endpoint: api.github.com"


def test_connection_scope_write_delegates_to_connectors(isolated_data_dir):
    import core.connectors as c
    import core.personal_memory as pm
    conn_id = c.create_connection("github_rest", "GitHub", "tok")["id"]
    pm.write_memory(f"connector:{conn_id}", "Auth header template")
    assert c.get_connection_memory(conn_id) == "Auth header template"


def test_connection_scope_rejects_oversized_content(isolated_data_dir):
    import core.connectors as c
    import core.personal_memory as pm
    conn_id = c.create_connection("github_rest", "GitHub", "tok")["id"]
    oversized = "заметка про сервер\n" * (c.CONNECTION_MEMORY_MAX_BYTES // 10)
    with pytest.raises(ValueError, match="too large"):
        pm.write_memory(f"connector:{conn_id}", oversized)


# ─── Registry and owner card ─────────────────────────────────────────────────


def test_list_memory_scopes_reports_only_non_empty(isolated_data_dir):
    import core.connectors as c
    import core.personal_memory as pm
    pm.write_memory("user", "## Notes\n- заметка владельца")
    pm.write_memory("orchestrator:dev_agent", "## Notes\n- заметка сотрудника")
    pm.write_memory("orchestrator:empty", "")
    conn_id = c.create_connection("github_rest", "GitHub", "tok")["id"]

    scopes = [item["scope"] for item in pm.list_memory_scopes()]
    assert "user" in scopes
    assert "orchestrator:dev_agent" in scopes
    assert f"connector:{conn_id}" not in scopes
    assert not any(s.startswith(".") for s in scopes)


def test_card_cap_follows_window_size(isolated_data_dir):
    import core.personal_memory as pm
    assert pm.card_cap(0) == pm.PERSONAL_CARD_HARD_CAP
    assert pm.card_cap(24_768) == 743
    assert pm.card_cap(111_616) == pm.PERSONAL_CARD_HARD_CAP
    assert pm.card_cap(616_000) == pm.PERSONAL_CARD_HARD_CAP
    assert pm.card_cap(1000) == pm.PERSONAL_CARD_MIN_CAP


def test_render_owner_card_is_empty_without_memory(isolated_data_dir):
    import core.personal_memory as pm
    assert pm.render_owner_card() == ""


def test_render_owner_card_keeps_section_order(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("user", (
        "## Notes\n- Заметка.\n"
        "## Preferences\n- Отчёты таблицами.\n"
        "## Rules\n- Никогда не коммитить без тестов.\n"
    ))
    card = pm.render_owner_card()
    assert card.index("## Rules") < card.index("## Preferences") < card.index("## Notes")


def test_render_owner_card_never_drops_rules(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("user", (
        "## Rules\n- Красная линия.\n"
        "## Preferences\n" + "- длинная строка предпочтений\n" * 200
    ))
    card = pm.render_owner_card(max_chars=pm.PERSONAL_CARD_MIN_CAP)
    assert "Красная линия" in card
    # A section that does not fit is trimmed, not dropped: the card keeps as
    # many of its lines as the budget allows and stays within the cap.
    assert "длинная строка предпочтений" in card
    assert len(card) <= pm.PERSONAL_CARD_MIN_CAP


def test_rebuild_index_lists_scopes(isolated_data_dir):
    import core.personal_memory as pm
    pm.write_memory("user", "## Notes\n- заметка")
    index = pm.rebuild_index()
    assert index["version"] == 1
    assert "user" in index["scopes"]
    assert os.path.exists(os.path.join(pm.personal_dir(), "index.json"))
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
