"""
test_preset_orchestrators.py - tests for the bundled default orchestrators.

YaAgent is the only non-dev built-in, bundled under
``defaults/orchestrators/ya_agent/`` (orchestrator.json + system_prompt.md).

The legacy presets/*.json mechanism has been removed; every import now goes
through ``core.defaults.defaults.load_default_orchestrator`` +
``ensure_default_orchestrators``.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


PRESET_SLUG = "ya_agent"
DEFAULTS_ORCH_PATH = (
    Path(__file__).resolve().parent.parent
    / "defaults" / "orchestrators" / "ya_agent" / "orchestrator.json"
)


@pytest.fixture
def isolated_data_dir(tmp_path):
    """Temporary DATA_DIR isolating default-import tests from real data."""
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = str(tmp_path)

    import core.paths as paths_mod
    old_attrs = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_attrs[attr] = getattr(paths_mod, attr, None)

    paths_mod.DATA_DIR = str(tmp_path)
    paths_mod.DB_PATH = os.path.join(str(tmp_path), "sagaai.db")
    paths_mod.DEVAGENT_DB_PATH = os.path.join(str(tmp_path), "devagent.db")
    paths_mod.HISTORY_DIR = os.path.join(str(tmp_path), "history")
    paths_mod.SYSTEM_PROMPTS_DIR = os.path.join(str(tmp_path), "system_prompts")

    import storage.db as db_mod
    db_mod.DB_PATH = paths_mod.DB_PATH
    db_mod.DEVAGENT_DB_PATH = paths_mod.DEVAGENT_DB_PATH

    reset_engine()
    reset_devagent_engine()

    yield tmp_path

    reset_engine()
    reset_devagent_engine()

    if old_env is not None:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr, val in old_attrs.items():
        if val is not None:
            setattr(paths_mod, attr, val)


def _run_bootstrap():
    from core.orchestrators import ensure_builtin_orchestrators
    return ensure_builtin_orchestrators()


def _get_preset():
    from core.orchestrators import get_orchestrator
    return get_orchestrator(PRESET_SLUG)


def test_preset_created_on_first_boot(isolated_data_dir):
    """First run creates YaAgent from the bundled defaults folder."""
    result = _run_bootstrap()
    assert result.get(PRESET_SLUG) == "created", result

    orch = _get_preset()
    assert orch is not None
    assert orch["name"] == "YaAgent"
    assert orch["is_builtin"] is False
    assert orch["sort_order"] == 150

    defaults_cfg = json.loads(DEFAULTS_ORCH_PATH.read_text(encoding="utf-8"))["config"]

    cfg = orch["config"]
    assert cfg["strong_service"] == defaults_cfg["strong_service"]
    assert cfg["strong_model"] == defaults_cfg["strong_model"]
    assert cfg["strong_temperature"] == defaults_cfg["strong_temperature"]
    assert cfg["weak_service"] == defaults_cfg["weak_service"]
    assert cfg["weak_model"] == defaults_cfg["weak_model"]
    assert cfg["weak_temperature"] == defaults_cfg["weak_temperature"]
    assert cfg["search_service"] == defaults_cfg["search_service"]
    assert cfg["search_model"] == defaults_cfg["search_model"]
    assert cfg["search_max_tool_calls"] == defaults_cfg["search_max_tool_calls"]
    assert cfg["economy_tail_messages"] == defaults_cfg["economy_tail_messages"]
    assert cfg["economy_cache_enabled"] is defaults_cfg["economy_cache_enabled"]
    assert cfg["economy_cache_multiplier"] == defaults_cfg["economy_cache_multiplier"]


def test_preset_prompt_loaded_from_md(isolated_data_dir):
    """YaAgent gets the Russian system prompt from defaults system_prompt.md."""
    _run_bootstrap()
    orch = _get_preset()
    prompt = orch["prompt_text"]
    assert "YaAgent" in prompt
    assert "Yandex AI Studio" in prompt
    assert "loop_status" in prompt
    assert len(prompt) > 5000


def test_preset_grants_full_toolset(isolated_data_dir):
    """Empty tools list yields the full DevAgent tool set."""
    _run_bootstrap()
    tools = _get_preset()["tools"]
    for name in ("read_file", "propose_file", "run_test", "web_search", "list_skills_library"):
        assert name in tools


def test_preset_is_idempotent(isolated_data_dir):
    """Second run does not recreate an existing default orchestrator."""
    _run_bootstrap()
    result = _run_bootstrap()
    assert result.get(PRESET_SLUG) == "exists", result


def test_preset_preserves_user_changes(isolated_data_dir):
    """User-edited YaAgent settings survive subsequent bootstrap runs."""
    from core.orchestrators import save_orchestrator

    _run_bootstrap()

    cfg = dict(_get_preset()["config"])
    cfg["strong_service"] = "DeepSeek"
    cfg["strong_model"] = "deepseek-v4-pro"
    cfg["strong_temperature"] = 0.9
    cfg["economy_tail_messages"] = 12
    save_orchestrator(PRESET_SLUG, config=cfg, name="MyCustomAgent")

    result = _run_bootstrap()
    assert result.get(PRESET_SLUG) == "exists", result

    orch = _get_preset()
    assert orch["name"] == "MyCustomAgent"
    assert orch["config"]["strong_service"] == "DeepSeek"
    assert orch["config"]["strong_model"] == "deepseek-v4-pro"
    assert orch["config"]["strong_temperature"] == 0.9
    assert orch["config"]["economy_tail_messages"] == 12
