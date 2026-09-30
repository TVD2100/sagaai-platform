# -*- coding: utf-8 -*-
"""tests/test_service_model_catalogs.py - unit tests for the optional
multimodal model catalogs in service profiles.

Service JSON files may declare two optional blocks:
  * ``vision_models`` - models able to analyse images (image + text -> text);
  * ``image_models``  - text-to-image generation models.

Only services that actually ship such models declare these blocks
(currently YandexAI: Qwen3.6 35B A3B for vision, Alice AI ART 3.0 /
YandexART for generation). core.services exposes the read helpers
get_vision_models() and get_image_models(); the settings UI renders an
empty option when a service declares no catalog.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.services import get_image_models, get_services, get_vision_models


def _repo_root() -> str:
    """Return the repository root (the parent of this tests/ folder)."""
    return str(Path(__file__).resolve().parent.parent)


def test_yandex_declares_vision_and_image_catalogs():
    """YandexAI ships the documented multimodal catalogs with ru/en labels."""
    svc = get_services().get("YandexAI") or {}
    vision = get_vision_models(svc)
    image = get_image_models(svc)

    vision_ids = [m.get("id") for m in vision]
    image_ids = [m.get("id") for m in image]
    assert "qwen3.6-35b-a3b" in vision_ids
    assert "yandex-art" in image_ids
    for entry in vision + image:
        label = entry.get("label") or {}
        assert label.get("ru"), entry
        assert label.get("en"), entry


def test_services_without_a_catalog_return_empty_lists():
    """DeepSeek and GigaChat do not declare vision/image catalogs."""
    services = get_services()
    for name in ("DeepSeek", "GigaChat"):
        svc = services.get(name) or {}
        assert get_vision_models(svc) == []
        assert get_image_models(svc) == []


def test_catalog_helpers_are_defensive():
    """The helpers tolerate None and malformed entries instead of raising."""
    assert get_vision_models(None) == []
    assert get_image_models(None) == []
    assert get_vision_models({"vision_models": ["x", 42]}) == []
    assert get_image_models({"image_models": [{"id": "m"}]}) == [{"id": "m"}]


def test_defaults_and_legacy_service_files_stay_in_sync():
    """defaults/services/*.json and the legacy services/*.json copies carry
    identical content, so both layouts expose the same model catalogs."""
    root = _repo_root()
    for fname in ("yandex.json", "deepseek.json", "gigachat.json"):
        path_defaults = os.path.join(root, "defaults", "services", fname)
        path_legacy = os.path.join(root, "services", fname)
        with open(path_defaults, encoding="utf-8") as f:
            data_defaults = json.load(f)
        with open(path_legacy, encoding="utf-8") as f:
            data_legacy = json.load(f)
        assert data_defaults == data_legacy, fname
