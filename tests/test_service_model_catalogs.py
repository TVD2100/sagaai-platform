# -*- coding: utf-8 -*-
"""tests/test_service_model_catalogs.py - unit tests for the optional
multimodal model catalogs in service profiles.

Service JSON files may declare two optional blocks:
  * ``vision_models`` - models able to analyse images (image + text -> text);
  * ``image_models``  - text-to-image generation models.

Services that ship such models declare these blocks: YandexAI (Qwen3.6 35B
A3B for vision, Alice AI ART 3.0 for generation) and DeepSeek (deepseek-flash
for vision, addressed through the dedicated ``vision_base_url`` endpoint).
core.services exposes the read helpers get_vision_models() and
get_image_models(); the settings UI renders an empty option when a service
declares no catalog.
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
    # DeepSeek V4.1 Flash also answers through the YandexAI connection
    # (verified live): declared as a second vision model since 1.7.0.
    assert "deepseek-v4.1-flash" in vision_ids
    # Single generation model since 1.6.0: the async yandex-art was removed.
    assert image_ids == ["aliceai-image-art-3.0"], image_ids
    for entry in vision + image:
        label = entry.get("label") or {}
        assert label.get("ru"), entry
        assert label.get("en"), entry


def test_deepseek_declares_vision_catalog_with_dedicated_endpoint():
    """DeepSeek ships a vision catalog (deepseek-flash) and a dedicated
    vision_base_url: the chat/completions endpoint, because the profile's
    default base_url points at the Responses API (text-only transport)."""
    svc = get_services().get("DeepSeek") or {}
    vision = get_vision_models(svc)
    assert [m.get("id") for m in vision] == ["deepseek-flash"]
    for entry in vision:
        label = entry.get("label") or {}
        assert label.get("ru"), entry
        assert label.get("en"), entry
    assert svc.get("vision_base_url") == "https://api.deepseek.com/chat/completions"
    assert get_image_models(svc) == []


def test_services_without_a_catalog_return_empty_lists():
    """GigaChat does not declare vision/image catalogs."""
    svc = get_services().get("GigaChat") or {}
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


def test_every_shipped_model_declares_an_output_limit():
    """B3: every shipped model entry declares ``max_tokens`` (the output
    limit default) so the guard reserve ``min(max_tokens, max(4096,
    0.25 * window))`` and ``_clamp_max_tokens`` never have to guess.
    DeepSeek declares the ~32k economy profile: on its 1M window the guard
    reserves 32_768 tokens instead of the 384k provider ceiling."""
    from core.context_guard import _output_reserve

    services = get_services()
    for name, svc in services.items():
        for m in svc.get("models") or []:
            assert isinstance(m, dict), (name, m)
            assert int(m.get("max_tokens") or 0) > 0, (name, m.get("id"))

    deepseek = services.get("DeepSeek") or {}
    limits = [m.get("max_tokens") for m in deepseek.get("models") or []]
    assert limits == [32_768, 32_768], limits
    assert _output_reserve(1_000_000, 32_768) == 32_768
