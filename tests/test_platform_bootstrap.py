"""
test_platform_bootstrap.py - tests for core.bootstrap in the current
SagaAI platform architecture.

Verifies that first-run provisioning seeds:
  - internal DevAgent instructions (Assistant Creator, Employee Creator,
    Self-Reflection),
  - the built-in 'dev_agent' orchestrator with its system prompt,
    configuration, and tool set.

Also verifies idempotency: re-running bootstrap refreshes prompt text
but never overwrites user-chosen service/model/temperature settings,
and backfills missing config fields (e.g. economy_tail_messages=30,
max_tokens=384000).

This test file deliberately avoids reading back from the DB inside
instruction tests because storage.repository caches its DB session at
import time and cannot be trivially redirected per-test alongside other
test files. Instead we check bootstrap return values and the in-code
prompt constants directly.
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

# Allow importing the sagaai package from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR that isolates bootstrap from real data.

    Follows the same pattern used by test_orchestrator_folders.py.
    """
    tmp = tempfile.mkdtemp(prefix="sagaai_test_bootstrap_")

    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths as paths_mod
    old_attrs = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_attrs[attr] = getattr(paths_mod, attr, None)

    paths_mod.DATA_DIR = tmp
    paths_mod.DB_PATH = os.path.join(tmp, "sagaai.db")
    paths_mod.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    paths_mod.HISTORY_DIR = os.path.join(tmp, "history")
    paths_mod.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    import storage.db as db_mod
    db_mod.DB_PATH = paths_mod.DB_PATH
    db_mod.DEVAGENT_DB_PATH = paths_mod.DEVAGENT_DB_PATH

    reset_engine()
    reset_devagent_engine()

    yield tmp

    reset_engine()
    reset_devagent_engine()

    if old_env is not None:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr, val in old_attrs.items():
        if val is not None:
            setattr(paths_mod, attr, val)

    shutil.rmtree(tmp, ignore_errors=True)


# --- internal instructions (prompt content & bootstrap return values) --------


def _load_default_instruction(fname):
    """Load a bundled default instruction .md and return (meta, body)."""
    import core.defaults as defaults_mod
    from core.orchestrators import DEVAGENT_SLUG
    path = os.path.join(
        defaults_mod.orchestrators_dir(), DEVAGENT_SLUG, "instructions", fname
    )
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return defaults_mod.parse_front_matter(raw, default_id=fname[:-3])


def test_assistant_creator_prompt_is_long_enough():
    _meta, body = _load_default_instruction("assistant_creator.md")
    assert len(body) > 500
    assert "## Role" in body
    assert "Self-contained" in body
    assert "Output format" in body


def test_employee_creator_prompt_is_long_enough():
    _meta, body = _load_default_instruction("employee_creator.md")
    assert len(body) > 500
    assert "Employee Creator" in body
    assert "create_orchestrator" in body


def test_prompt_improver_instruction_is_long_enough():
    _meta, body = _load_default_instruction("prompt_improver.md")
    assert len(body) > 500
    assert "## Role" in body
    assert "## Task" in body
    assert "Output format" in body


def test_ensure_instructions_returns_expected_keys(isolated_data_dir):
    from core.bootstrap import ensure_instructions
    result = ensure_instructions()
    assert "devagent_instructions" in result
    statuses = result["devagent_instructions"]
    assert statuses["assistant_creator"] == "created"
    assert statuses["employee_creator"] == "created"
    assert statuses["self_reflection"] == "created"
    assert statuses["prompt_improver"] == "created"
    assert statuses["github_connector"] == "created"
    assert result["global_cleaned"] == []


def test_ensure_instructions_is_idempotent(isolated_data_dir):
    from core.bootstrap import ensure_instructions
    ensure_instructions()
    result = ensure_instructions()
    statuses = result["devagent_instructions"]
    assert statuses["assistant_creator"] == "exists"
    assert statuses["employee_creator"] == "exists"
    assert statuses["self_reflection"] == "exists"
    assert statuses["prompt_improver"] == "exists"
    assert statuses["github_connector"] == "exists"


# --- built-in DevAgent orchestrator -----------------------------------------


def test_ensure_devagent_settings_seeds_builtin_orchestrator(isolated_data_dir):
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator_by_slug

    result = ensure_devagent_settings()
    assert DEVAGENT_SLUG in result

    orch = get_orchestrator_by_slug(DEVAGENT_SLUG)
    assert orch is not None
    assert orch["name"] == "DevAgent"
    assert orch["is_builtin"] is True


def test_ensure_devagent_settings_sets_prompt_from_system_prompt_md(isolated_data_dir):
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator

    ensure_devagent_settings()
    orch = get_orchestrator(DEVAGENT_SLUG)
    assert orch is not None
    prompt = orch.get("prompt_text", "")
    assert len(prompt) > 50


def test_ensure_devagent_settings_seeds_config_and_tools(isolated_data_dir):
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator

    ensure_devagent_settings()
    orch = get_orchestrator(DEVAGENT_SLUG)
    cfg = orch["config"]
    assert cfg.get("strong_service")
    assert cfg.get("strong_model")
    assert cfg.get("weak_service")
    assert cfg.get("weak_model")
    # DeepSeek strong/weak models must default to 384000 output tokens.
    assert cfg.get("strong_max_tokens") == 384000
    assert cfg.get("weak_max_tokens") == 384000
    assert len(orch["tools"]) > 0


def test_ensure_devagent_settings_seeds_economy_defaults(isolated_data_dir):
    """A fresh DevAgent orchestrator gets the default economy settings:
    tail_messages=30, cache_enabled=True, cache_multiplier=3. The on-disk
    bundle (orchestrator.json) is created with the same defaults."""
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator
    from core.orchestrator_folders import load_orchestrator_bundle

    ensure_devagent_settings()
    orch = get_orchestrator(DEVAGENT_SLUG)
    cfg = orch["config"]
    assert cfg.get("economy_tail_messages") == 30
    assert cfg.get("economy_cache_enabled") is True
    assert cfg.get("economy_cache_multiplier") == 3

    # The fresh bundle on disk must also carry the current defaults.
    bundle = load_orchestrator_bundle(DEVAGENT_SLUG)
    assert bundle is not None
    assert bundle["config"].get("economy_tail_messages") == 30
    assert bundle["config"].get("economy_cache_enabled") is True
    assert bundle["config"].get("economy_cache_multiplier") == 3


def test_ensure_devagent_settings_seeds_max_tokens_in_bundle(isolated_data_dir):
    """The fresh on-disk bundle also carries strong/weak max_tokens=384000."""
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import DEVAGENT_SLUG
    from core.orchestrator_folders import load_orchestrator_bundle

    ensure_devagent_settings()
    bundle = load_orchestrator_bundle(DEVAGENT_SLUG)
    assert bundle is not None
    assert bundle["config"].get("strong_max_tokens") == 384000
    assert bundle["config"].get("weak_max_tokens") == 384000


def test_ensure_devagent_settings_is_idempotent(isolated_data_dir):
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import DEVAGENT_SLUG

    ensure_devagent_settings()
    result = ensure_devagent_settings()
    assert result[DEVAGENT_SLUG] == "updated"


def test_ensure_devagent_settings_preserves_user_config(isolated_data_dir):
    """Re-running bootstrap must not overwrite user-chosen models/services."""
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import (
        DEVAGENT_SLUG,
        get_orchestrator,
        save_devagent_config,
    )

    ensure_devagent_settings()

    ok = save_devagent_config(
        service="CustomService",
        model="custom-model",
        temperature=0.9,
        prompt_text="Custom prompt",
        strong_service="CustomService",
        strong_model="custom-strong",
        weak_service="CustomService",
        weak_model="custom-weak",
        search_service="YandexAI",
        search_model="aliceai-llm-flash",
    )
    assert ok is True

    ensure_devagent_settings()

    orch = get_orchestrator(DEVAGENT_SLUG)
    cfg = orch["config"]
    assert cfg["strong_service"] == "CustomService"
    assert cfg["strong_model"] == "custom-strong"
    assert cfg["weak_service"] == "CustomService"
    assert cfg["weak_model"] == "custom-weak"
    # prompt_text is intentionally refreshed from system_prompt.md
    assert orch["prompt_text"] != "Custom prompt"


def test_ensure_devagent_settings_backfills_missing_config_fields(isolated_data_dir):
    """An existing legacy config (created by an older version) gets its
    missing fields backfilled from defaults without losing user values."""
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import (
        DEVAGENT_SLUG,
        get_orchestrator,
        save_orchestrator,
    )

    ensure_devagent_settings()

    # Simulate an old config: only a few fields, economy_tail_messages=15,
    # no cache fields, no max tokens.
    legacy_config = {
        "strong_service": "MyCustomSvc",
        "strong_model": "my-custom-model",
        "strong_temperature": 0.7,
        "weak_service": "MyCustomSvc",
        "weak_model": "my-weak-model",
        "search_temperature": 0.25,
        "economy_tail_messages": 15,
    }
    ok = save_orchestrator(DEVAGENT_SLUG, config=legacy_config)
    assert ok is True

    ensure_devagent_settings()  # must backfill

    orch = get_orchestrator(DEVAGENT_SLUG)
    cfg = orch["config"]
    # Legacy 15 is upgraded to the new default 30.
    assert cfg["economy_tail_messages"] == 30
    # Missing cache fields are backfilled.
    assert cfg["economy_cache_enabled"] is True
    assert cfg["economy_cache_multiplier"] == 3
    # Missing max-token fields are backfilled.
    assert cfg["strong_max_tokens"] == 384000
    assert cfg["weak_max_tokens"] == 384000
    # User-chosen values are preserved.
    assert cfg["strong_service"] == "MyCustomSvc"
    assert cfg["strong_model"] == "my-custom-model"
    assert cfg["weak_service"] == "MyCustomSvc"
    assert cfg["weak_model"] == "my-weak-model"
    assert cfg["strong_temperature"] == 0.7
    assert cfg["search_temperature"] == 0.25


def test_ensure_devagent_settings_backfills_zero_max_tokens(isolated_data_dir):
    """Configs created by the earlier first-boot bug (max_tokens=0) are
    upgraded to the DeepSeek default 384000 on the next bootstrap."""
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import (
        DEVAGENT_SLUG,
        get_orchestrator,
        save_orchestrator,
    )

    ensure_devagent_settings()
    cfg_zero = dict(get_orchestrator(DEVAGENT_SLUG)["config"])
    cfg_zero["strong_max_tokens"] = 0
    cfg_zero["weak_max_tokens"] = 0
    save_orchestrator(DEVAGENT_SLUG, config=cfg_zero)

    ensure_devagent_settings()

    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]
    assert cfg["strong_max_tokens"] == 384000
    assert cfg["weak_max_tokens"] == 384000


def test_ensure_devagent_settings_keeps_user_economy_tail(isolated_data_dir):
    """A user who explicitly saved a non-default economy tail keeps it;
    bootstrap must NOT reset it to the default."""
    from core.bootstrap import ensure_devagent_settings
    from core.orchestrators import (
        DEVAGENT_SLUG,
        get_orchestrator,
        save_orchestrator,
    )

    ensure_devagent_settings()
    cfg_now = get_orchestrator(DEVAGENT_SLUG)["config"]
    user_tail = 12
    cfg_now["economy_tail_messages"] = user_tail
    save_orchestrator(DEVAGENT_SLUG, config=cfg_now)

    ensure_devagent_settings()

    orch = get_orchestrator(DEVAGENT_SLUG)
    assert orch["config"]["economy_tail_messages"] == user_tail
