# -*- coding: utf-8 -*-
"""Tests for core.service_migration - carrying records off a retired service.

The migration rewrites three stores: the API key in the configuration KV, the
service/model pair of every assistant and the per-role model fields of every
orchestrator config. Each test drives one store, and one test pins the
idempotency that makes the call safe on every cold start.
"""
import os
import shutil
import tempfile

import pytest

from storage.db import get_engine, reset_devagent_engine, reset_engine


@pytest.fixture()
def isolated_data_dir():
    """Temporary DATA_DIR so the real database and the real config are untouched."""
    tmp = tempfile.mkdtemp(prefix="sagaai_test_migration_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR",
                 "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    reset_engine()
    reset_devagent_engine()
    get_engine()
    try:
        yield tmp
    finally:
        reset_engine()
        reset_devagent_engine()
        if old_data_dir:
            os.environ["SAGAAI_DATA_DIR"] = old_data_dir
        else:
            os.environ.pop("SAGAAI_DATA_DIR", None)
        for attr, value in old_values.items():
            if value is not None:
                setattr(core.paths, attr, value)
        shutil.rmtree(tmp, ignore_errors=True)


AGENT = "GigaChat"
REPLACEMENT = "Cloud"


def test_retired_service_is_declared_with_a_replacement():
    from core.service_migration import RETIRED_SERVICES

    table = RETIRED_SERVICES[AGENT]
    assert table["replacement"] == REPLACEMENT
    # The key is removed rather than carried: a key authenticates only
    # against the gateway that issued it.
    assert "GIGACHAT_API_KEY" in table["drop_config_keys"]
    assert "config_key" not in table
    assert table["default_model"]


def test_migrated_model_maps_known_and_unknown_ids():
    from core.service_migration import RETIRED_SERVICES, migrated_model

    table = RETIRED_SERVICES[AGENT]
    assert migrated_model(table, "GigaChat-3-Ultra") == "GigaChat/GigaChat-4-708B-A41"
    assert migrated_model(table, "GigaChat-3-Pro") == "GigaChat/GigaChat-3-Pro"
    # An id the replacement cannot serve falls back to the declared default.
    assert migrated_model(table, "some-retired-id") == table["default_model"]


def test_retired_key_is_removed_and_not_passed_to_the_replacement(isolated_data_dir):
    from core.config import load_stored_config, save_config
    from core.service_migration import migrate_retired_services

    save_config({"GIGACHAT_API_KEY": "legacy-key", "GIGACHAT_SCOPE": "GIGACHAT_API_PERS"})
    report = migrate_retired_services()

    stored = load_stored_config()
    assert "GIGACHAT_API_KEY" not in stored
    assert "GIGACHAT_SCOPE" not in stored
    assert not stored.get("CLOUD_API_KEY")
    assert report["services"][AGENT]["config"] == "updated"
    assert sorted(report["services"][AGENT]["config_keys_removed"]) == [
        "GIGACHAT_API_KEY", "GIGACHAT_SCOPE"]


def test_a_key_the_owner_already_entered_survives_the_migration(isolated_data_dir):
    from core.config import load_stored_config, save_config
    from core.service_migration import migrate_retired_services

    save_config({"GIGACHAT_API_KEY": "legacy-key", "CLOUD_API_KEY": "owner-key"})
    migrate_retired_services()

    stored = load_stored_config()
    assert stored.get("CLOUD_API_KEY") == "owner-key"
    assert "GIGACHAT_API_KEY" not in stored


def test_assistant_is_repointed_to_the_replacement_service(isolated_data_dir):
    from core.service_migration import migrate_retired_services
    from storage.repository import repo_create_assistant, repo_get_assistant

    assert repo_create_assistant(
        assistant_id="a1", name="Помощник", service=AGENT,
        model="GigaChat-2-Max", temperature=0.3, prompt_text="ты полезен",
    )
    report = migrate_retired_services()

    record = repo_get_assistant("a1")
    assert record["service"] == REPLACEMENT
    assert record["model"] == "GigaChat/GigaChat-4-708B-A41"
    assert report["services"][AGENT]["assistants"] == 1


def test_orchestrator_role_fields_are_rewritten(isolated_data_dir):
    from core.service_migration import migrate_retired_services
    from storage.repository import repo_create_orchestrator, repo_get_orchestrator_by_slug

    assert repo_create_orchestrator(
        orchestrator_id="o1", slug="tester", name="Tester", description="",
        prompt_text="prompt",
        config={
            "strong_service": AGENT, "strong_model": "GigaChat-3-Pro",
            "search_service": "DeepSeek", "search_model": "deepseek-flash",
            "vision_service": AGENT, "vision_model": "GigaChat-3-Lightning",
        },
    )
    report = migrate_retired_services()

    config = repo_get_orchestrator_by_slug("tester")["config"]
    assert config["strong_service"] == REPLACEMENT
    assert config["strong_model"] == "GigaChat/GigaChat-3-Pro"
    assert config["vision_service"] == REPLACEMENT
    assert config["vision_model"] == "ai-sage/GigaChat3-10B-A1.8B"
    # A role that never used the retired service is left exactly as it was.
    assert config["search_service"] == "DeepSeek"
    assert config["search_model"] == "deepseek-flash"
    assert report["services"][AGENT]["orchestrators"] == 1


def test_second_run_finds_nothing_to_migrate(isolated_data_dir):
    from core.config import load_stored_config, save_config
    from core.service_migration import migrate_retired_services
    from storage.repository import repo_create_assistant

    save_config({"GIGACHAT_API_KEY": "legacy-key"})
    assert repo_create_assistant(
        assistant_id="a1", name="Помощник", service=AGENT,
        model="GigaChat-2", temperature=0.3, prompt_text="prompt",
    )
    first = migrate_retired_services()
    second = migrate_retired_services()

    assert first["services"][AGENT]["assistants"] == 1
    assert second["services"][AGENT]["assistants"] == "unchanged"
    assert second["services"][AGENT]["orchestrators"] == 0
    assert second["services"][AGENT]["config"] == "unchanged"
    assert second["ok"] is True
    assert "GIGACHAT_API_KEY" not in load_stored_config()

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
