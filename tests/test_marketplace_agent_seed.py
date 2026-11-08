# -*- coding: utf-8 -*-
"""tests/test_marketplace_agent_seed.py - what an installed agent starts with.

An agent installed from a catalog runs on the platform like a bundled one.
Two things make that true, and both live in the installer so every package
kind gets them: the starting config carries the platform model basis, and
the catalog translations reach the name slots. A package that names its own
settings - a model of its own, the author's own wording - keeps them.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.builtin_config_defaults import builtin_config_defaults  # noqa: E402


def _seed_platform_config(bundle):
    """Call the seed step through a fresh import (another test re-imports it)."""
    from core.marketplace.install_seed import _seed_platform_config as _seed

    return _seed(bundle)


def _apply_entity_names(bundle, entity):
    """Call the name step through a fresh import."""
    from core.marketplace.install_seed import _apply_entity_names as _names

    return _names(bundle, entity)


def test_a_package_without_model_keys_gets_the_platform_basis():
    """The reported defect: no model keys in the package, no model at all."""
    bundle = _seed_platform_config({"config": {"max_steps": 5}})

    config = bundle["config"]
    assert config["strong_service"] == "DeepSeek"
    assert config["strong_model"]
    assert config["strong_temperature"]
    assert config["vision_model"]
    # The package's own setting is untouched.
    assert config["max_steps"] == 5


def test_the_package_always_wins_over_the_platform_basis():
    bundle = _seed_platform_config({"config": {
        "strong_service": "YandexAI",
        "strong_model": "yandexgpt",
        "strong_temperature": 0.9,
    }})

    config = bundle["config"]
    assert config["strong_service"] == "YandexAI"
    assert config["strong_model"] == "yandexgpt"
    assert config["strong_temperature"] == 0.9


def test_search_settings_follow_the_agents_own_switch():
    """A provider name without the switch would look configured and do nothing."""
    plain = _seed_platform_config({"config": {}})["config"]
    assert "search_service" not in plain
    assert "search_model" not in plain

    enabled = _seed_platform_config({"config": {"chat_web_search": True}})["config"]
    assert enabled["search_service"]
    assert enabled["search_model"]


def test_the_seeded_keys_are_the_ones_a_bundled_agent_receives():
    seeded = _seed_platform_config({"config": {"chat_web_search": True}})["config"]
    defaults = builtin_config_defaults()

    # Every platform default is present; the package keeps its own keys too.
    assert set(defaults) <= set(seeded)


def test_a_bundle_without_config_gains_one():
    assert _seed_platform_config({})["config"]["strong_service"]


def test_the_catalog_translations_reach_the_name_slots():
    """The reported defect: an English name in a Russian interface."""
    bundle = {"name": "Project Manager", "name_en": "", "name_ru": "", "name_zh": ""}
    entity = {"name": {"en": "Project Manager", "ru": "Менеджер проектов", "zh-CN": "项目经理"}}

    bundle = _apply_entity_names(bundle, entity)

    assert bundle["name"] == ""
    assert bundle["name_en"] == "Project Manager"
    assert bundle["name_ru"] == "Менеджер проектов"
    assert bundle["name_zh"] == "项目经理"


def test_the_match_ignores_case_and_surrounding_space():
    bundle = {"name": "  project manager "}
    entity = {"name": {"en": "Project Manager", "ru": "Менеджер проектов"}}

    bundle = _apply_entity_names(bundle, entity)

    assert bundle["name_ru"] == "Менеджер проектов"


def test_an_authors_own_name_is_never_replaced():
    """A name the card does not repeat is the author's wording and stays."""
    bundle = {"name": "Мой личный агент", "name_en": "", "name_ru": "", "name_zh": ""}
    entity = {"name": {"en": "Project Manager", "ru": "Менеджер проектов"}}

    bundle = _apply_entity_names(bundle, entity)

    assert bundle["name"] == "Мой личный агент"
    assert bundle["name_ru"] == ""


def test_a_card_without_an_english_name_leaves_the_package_name():
    bundle = {"name": "Project Manager"}

    assert _apply_entity_names(bundle, {"name": {"ru": "Менеджер проектов"}})["name"] == \
        "Project Manager"
    assert _apply_entity_names(bundle, {})["name"] == "Project Manager"


def test_a_bundle_that_already_has_translations_is_left_alone():
    """A package describing itself in every language needs no card."""
    bundle = {"name": "", "name_en": "Project Manager", "name_ru": "Своё имя"}
    entity = {"name": {"en": "Project Manager", "ru": "Менеджер проектов"}}

    assert _apply_entity_names(bundle, entity)["name_ru"] == "Своё имя"


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
