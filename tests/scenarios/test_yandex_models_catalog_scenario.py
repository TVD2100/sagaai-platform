# -*- coding: utf-8 -*-
"""tests/scenarios/test_yandex_models_catalog_scenario.py - user-level scenario
tests for the YandexAI model catalog update (release 1.3.2).

Scenarios (given -> when -> then):

  Scenario 1 - the YandexAI catalog offers deepseek-v4.1-flash right after
               deepseek-v4-flash with identical settings, while
               qwen3-235b-a22b-fp8, gpt-oss-120b and gpt-oss-20b are gone
               from both service copies and from the effective runtime
               service.

  Scenario 2 - the two service copies stay byte-identical (defaults/ and the
               runtime services/yandex.json).

  Scenario 3 - the YaAgent reference doc and system prompt agree with the
               catalog: the reference lists deepseek-v4.1-flash and none of
               the retired models; the prompt is v2.11 and no longer mentions
               GPT-OSS.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

ROOT = Path(__file__).resolve().parent.parent.parent

REMOVED_IDS = ("qwen3-235b-a22b-fp8", "gpt-oss-120b", "gpt-oss-20b")
NEW_ID = "deepseek-v4.1-flash"
FLASH_ID = "deepseek-v4-flash"
SERVICE_COPIES = ("services/yandex.json", "defaults/services/yandex.json")


def _json(path: str) -> dict:
    """Load one repository JSON file relative to the project root."""
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _models(svc: dict) -> dict:
    """Return a service's catalog models keyed by model id."""
    return {m.get("id"): m for m in (svc.get("models") or [])}


# --- Scenario 1 - catalog contents and flash settings -----------------------


def test_catalog_offers_deepseek_v41_flash_and_drops_retired_models():
    """Given the updated YandexAI provider definitions, when they are loaded
    (defaults win), then deepseek-v4.1-flash exists next to deepseek-v4-flash
    with identical settings and none of the retired model ids are present in
    either copy or in the effective runtime service."""
    for path in SERVICE_COPIES:
        svc = _json(path)
        models = _models(svc)
        for bad in REMOVED_IDS:
            assert bad not in models, (path, bad)
        assert NEW_ID in models, (path, NEW_ID)

        new_cfg = dict(models[NEW_ID])
        flash_cfg = dict(models[FLASH_ID])
        new_cfg.pop("id")
        flash_cfg.pop("id")
        # Since 1.9.2 the two Flash models differ ONLY in reasoning effort
        # options: Yandex AI Studio rejects "minimal"/"xhigh" for
        # deepseek-v4.1-flash (verified live on 2026-10-03), while
        # deepseek-v4-flash accepts them.
        new_opts = new_cfg.pop("reasoning_effort_options")
        flash_opts = flash_cfg.pop("reasoning_effort_options")
        assert new_cfg == flash_cfg, path
        assert flash_opts == ["", "none", "minimal", "low", "medium", "high", "xhigh"], path
        assert new_opts == ["", "none", "low", "medium", "high"], path

        ids = [m.get("id") for m in svc["models"]]
        assert ids[ids.index(FLASH_ID) + 1] == NEW_ID, path

    from core.services import _cached_services, discover_services
    _cached_services.cache_clear()
    effective = _models(discover_services()["YandexAI"])
    for bad in REMOVED_IDS:
        assert bad not in effective, bad
    assert NEW_ID in effective


# --- Scenario 2 - copies stay byte-identical --------------------------------


def test_service_copies_are_byte_identical():
    """Given the defaults and runtime service copies, when they are compared,
    then their sha256 digests match (single source of truth kept in sync)."""
    digests = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in SERVICE_COPIES
    }
    assert len(set(digests.values())) == 1, digests


# --- Scenario 3 - docs and prompt agree with the catalog ---------------------


def test_reference_and_prompt_agree_with_catalog():
    """Given the YaAgent reference doc and system prompt, when they are
    inspected, then the reference advertises deepseek-v4.1-flash (no retired
    models) and the prompt carries the v2.11 header without GPT-OSS."""
    reference = (
        ROOT / "defaults" / "orchestrators" / "ya_agent"
        / "instructions" / "yandex_models_reference.md"
    ).read_text(encoding="utf-8")
    assert NEW_ID in reference
    assert "DeepSeek V4.1 Flash" in reference
    for bad in REMOVED_IDS + ("qwen3-235b", "GPT-OSS", "gpt-oss"):
        assert bad not in reference, bad

    prompt = (
        ROOT / "defaults" / "orchestrators" / "ya_agent" / "system_prompt.md"
    ).read_text(encoding="utf-8")
    assert prompt.splitlines()[0].endswith("(v2.11)")
    assert "GPT-OSS" not in prompt
