# -*- coding: utf-8 -*-
"""tests/test_single_model_config.py - unit tests for the single-model
DevAgent configuration layer and the optional multimodal model fields.

Single-model mode (v1.4.0): an orchestrator has ONE main model stored in the
canonical ``strong_*`` config keys. The legacy ``weak_*`` keys are tolerated
on read but are never written any more; every compatibility view (the flat
``load_devagent_config`` dict, ``build_assistant_dicts``) maps the weak
aliases onto the main model.

Covered here:
  * save_devagent_config accepts weak_* arguments but does not persist them;
  * vision_/image_ fields are stored when passed and left untouched when
    omitted (None); an explicit "" clears the assignment;
  * legacy configs that still carry weak_* keys load without errors and
    behave as single-model (weak alias == main model);
  * build_assistant_dicts returns an identical weak/strong pair;
  * DevAgent defaults and first-boot seeding produce no weak_* keys and
    backfill the multimodal keys as empty ("not assigned").
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR that isolates orchestrators from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_single_model_")
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


def _bootstrap():
    """Seed the built-in DevAgent into the isolated DATA_DIR."""
    from core.bootstrap import ensure_devagent_settings
    return ensure_devagent_settings()


def _current_prompt() -> str:
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator
    orch = get_orchestrator(DEVAGENT_SLUG) or {}
    return orch.get("prompt_text", "")


def _save(**kwargs):
    """Call save_devagent_config with safe defaults for the required args."""
    from core.orchestrators import save_devagent_config
    kwargs.setdefault("service", "")
    kwargs.setdefault("model", "")
    kwargs.setdefault("temperature", 0.2)
    kwargs.setdefault("prompt_text", _current_prompt())
    return save_devagent_config(**kwargs)


def test_save_accepts_weak_args_but_does_not_persist(isolated_data_dir):
    """save_devagent_config keeps its weak_* parameters (backward
    compatibility) but the weak values never reach the stored config."""
    _bootstrap()
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator

    ok = _save(
        strong_service="YandexAI", strong_model="yandexgpt-5-pro",
        strong_temperature=0.6,
        weak_service="WeakSvc", weak_model="weak-model",
        weak_temperature=0.9, weak_max_tokens=111,
        search_service="YandexAI", search_model="aliceai-llm-flash",
    )
    assert ok is True
    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]
    assert cfg["strong_service"] == "YandexAI"
    assert cfg["strong_model"] == "yandexgpt-5-pro"
    assert cfg["strong_temperature"] == 0.6
    for legacy_key in ("weak_service", "weak_model", "weak_temperature", "weak_max_tokens"):
        assert legacy_key not in cfg


def test_multimodal_fields_set_and_clear(isolated_data_dir):
    """vision_/image_ fields update only when passed; an explicit empty
    string clears the assignment while omitted keys keep their value."""
    _bootstrap()
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator

    assert _save(
        strong_service="DeepSeek", strong_model="deepseek-v4-pro",
        vision_service="YandexAI", vision_model="qwen3.6-35b-a3b",
        image_service="YandexAI", image_model="yandex-art-3.0",
    ) is True
    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]
    assert cfg["vision_service"] == "YandexAI"
    assert cfg["vision_model"] == "qwen3.6-35b-a3b"
    assert cfg["image_service"] == "YandexAI"
    assert cfg["image_model"] == "yandex-art-3.0"

    # A save without the multimodal kwargs keeps the current values.
    assert _save(strong_service="DeepSeek", strong_model="deepseek-v4-pro") is True
    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]
    assert cfg["vision_model"] == "qwen3.6-35b-a3b"
    assert cfg["image_model"] == "yandex-art-3.0"

    # An explicit empty string clears the assignment.
    assert _save(
        strong_service="DeepSeek", strong_model="deepseek-v4-pro",
        vision_service="", vision_model="",
    ) is True
    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]
    assert cfg["vision_service"] == ""
    assert cfg["vision_model"] == ""
    assert cfg["image_model"] == "yandex-art-3.0"


def test_legacy_weak_config_is_tolerated_and_single_model(isolated_data_dir):
    """A stored config that still carries weak_* keys loads fine; the flat
    view and build_assistant_dicts map everything onto the main model."""
    _bootstrap()
    from core.orchestrators import (
        DEVAGENT_SLUG, build_assistant_dicts, get_orchestrator, save_orchestrator,
    )
    from core.config import load_devagent_config

    legacy = dict(get_orchestrator(DEVAGENT_SLUG)["config"])
    legacy.update({
        "weak_service": "WeakSvc",
        "weak_model": "weak-model",
        "weak_temperature": 0.9,
        "weak_max_tokens": 123,
    })
    assert save_orchestrator(DEVAGENT_SLUG, config=legacy) is True

    flat = load_devagent_config()
    assert flat["weak_service"] == flat["strong_service"]
    assert flat["weak_model"] == flat["strong_model"]
    assert flat["weak_model"] != "weak-model"
    assert flat["vision_service"] == ""
    assert flat["image_model"] == ""

    strong, weak = build_assistant_dicts(DEVAGENT_SLUG)
    assert weak["service"] == strong["service"]
    assert weak["model"] == strong["model"]
    assert strong["model"] != "weak-model"
    assert weak["temperature"] == strong["temperature"]
    assert weak["text"] == strong["text"]
    assert weak.get("reasoning_effort") == strong.get("reasoning_effort")
    assert weak.get("max_tokens") == strong.get("max_tokens")


def test_defaults_and_bootstrap_have_no_weak_keys(isolated_data_dir):
    """DevAgent defaults and first-boot seeding produce no weak_* keys and
    expose the multimodal keys as empty strings."""
    from core.config import reload_devagent_defaults
    from core.orchestrators import DEVAGENT_SLUG, _devagent_default_config, get_orchestrator

    reload_devagent_defaults()
    defaults = _devagent_default_config()
    for legacy_key in ("weak_service", "weak_model", "weak_temperature",
                       "weak_max_tokens", "weak_reasoning_effort"):
        assert legacy_key not in defaults
    assert defaults["vision_service"] == ""
    assert defaults["vision_model"] == ""
    assert defaults["image_service"] == ""
    assert defaults["image_model"] == ""

    _bootstrap()
    cfg = get_orchestrator(DEVAGENT_SLUG)["config"]
    assert "weak_service" not in cfg
    assert "weak_max_tokens" not in cfg
    assert cfg["vision_service"] == ""
    assert cfg["vision_model"] == ""

    import core.config as config_mod
    assert not hasattr(config_mod, "get_default_weak_max_tokens")


def test_export_import_roundtrip_keeps_multimodal_fields(isolated_data_dir):
    """An exported orchestrator carries the multimodal fields; importing it
    as a new orchestrator keeps them and never reintroduces weak_* keys."""
    _bootstrap()
    from core.orchestrators import (
        DEVAGENT_SLUG, export_orchestrator, get_orchestrator, import_orchestrator,
    )

    assert _save(
        strong_service="DeepSeek", strong_model="deepseek-v4-pro",
        vision_service="YandexAI", vision_model="qwen3.6-35b-a3b",
        image_service="YandexAI", image_model="yandex-art-3.0",
    ) is True

    data = export_orchestrator(DEVAGENT_SLUG)
    assert data is not None
    assert data["config"]["vision_service"] == "YandexAI"
    assert data["config"]["image_model"] == "yandex-art-3.0"
    assert "weak_service" not in data["config"]

    data = dict(data)
    data["slug"] = "single_model_import"
    res = import_orchestrator(data, overwrite=False)
    assert res["ok"] is True, res
    imported = get_orchestrator(res["slug"])
    assert imported is not None
    cfg = imported["config"]
    assert cfg["vision_model"] == "qwen3.6-35b-a3b"
    assert cfg["image_model"] == "yandex-art-3.0"
    assert "weak_service" not in cfg


def test_dev_agent_build_assistant_dict_from_config_single_model(isolated_data_dir):
    """dev_agent's own config builder returns weak == main (copy)."""
    _bootstrap()
    from core.orchestrators import DEVAGENT_SLUG, get_orchestrator, save_orchestrator
    from dev_agent.universal_agent import build_assistant_dict_from_config

    cfg = dict(get_orchestrator(DEVAGENT_SLUG)["config"])
    cfg.update({"strong_service": "DeepSeek", "strong_model": "deepseek-v4-pro"})
    assert save_orchestrator(DEVAGENT_SLUG, config=cfg) is True

    main, weak = build_assistant_dict_from_config()
    assert main["service"] == "DeepSeek"
    assert main["model"] == "deepseek-v4-pro"
    assert weak["service"] == main["service"]
    assert weak["model"] == main["model"]
    assert weak["text"] == main["text"]
    # A copy, not the same object: mutating one must not affect the other.
    assert weak is not main
    weak["model"] = "changed"
    assert main["model"] == "deepseek-v4-pro"
