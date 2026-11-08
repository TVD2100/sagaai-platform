# -*- coding: utf-8 -*-
"""Storage tests for the localized name slots (revision 0011)."""
import sqlite3

import pytest

from storage import migrations


@pytest.fixture()
def main_db(tmp_path):
    """Return the path of a fresh main database migrated to head."""
    path = str(tmp_path / "sagaai.db")
    report = migrations.upgrade(path, migrations.MAIN_KIND)
    assert report.get("ok"), report
    return path


def _columns(path: str, table: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]
    finally:
        conn.close()


def test_both_entity_tables_receive_the_three_slots(main_db):
    assert {"name_en", "name_ru", "name_zh"} <= set(_columns(main_db, "orchestrators"))
    assert {"name_en", "name_ru", "name_zh"} <= set(_columns(main_db, "assistants"))


def test_the_column_helpers_are_idempotent(main_db):
    """A second pass over an already migrated database changes nothing."""
    from sqlalchemy import create_engine

    from storage import db as storage_db

    engine = create_engine("sqlite:///" + main_db)
    try:
        before = set(_columns(main_db, "orchestrators"))
        storage_db._ensure_orchestrator_columns(engine)
        storage_db._ensure_assistant_columns(engine)
        assert set(_columns(main_db, "orchestrators")) == before
    finally:
        engine.dispose()


def test_rows_written_before_the_migration_survive(main_db):
    """A pre-existing row keeps its name and reads NULL in every new slot."""
    conn = sqlite3.connect(main_db)
    try:
        conn.execute(
            "INSERT INTO orchestrators (id, slug, name, description, prompt_text,"
            " config_json, tools, max_steps, auto_apply, is_builtin, sort_order,"
            " created_at, updated_at) VALUES ('abc12345', 'local_agent', '"
            "Мой агент', '', '', '{}', '[]', 100, 1, 0, 100, 'now', 'now')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT name, name_en, name_ru, name_zh FROM orchestrators"
            " WHERE slug = 'local_agent'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("Мой агент", None, None, None)
