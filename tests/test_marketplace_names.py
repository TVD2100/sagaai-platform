# -*- coding: utf-8 -*-
"""tests/test_marketplace_names.py - localized names in the marketplace.

Every entity carries four name slots: the user-defined name and the en/ru/zh
translations. These tests cover the three places a marketplace name passes
through, because each has its own way of losing the other three slots:

- reading a package (``orchestrator.json`` / ``manifest.json``), where a
  bare string, a name mapping and explicit translation keys all occur;
- resolving a catalog name for the interface language;
- merging an update, where the user's own name must survive.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.marketplace.agent_merge import _merge_assistant_payload  # noqa: E402
from core.marketplace.agent_package import (  # noqa: E402
    _assistant_bundle_from_folder,
    _bundle_from_folder,
)


def _write(folder: Path, name: str, payload) -> None:
    """Write one text member of a package folder."""
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def test_orchestrator_package_with_name_mapping_fills_translations(tmp_path):
    folder = tmp_path / "content"
    _write(folder, "orchestrator.json", {
        "name": {"en": "Developer", "ru": "Разработчик", "zh-CN": "开发者"},
    })
    _write(folder, "system_prompt.md", "You are a developer.\n")

    bundle = _bundle_from_folder(str(folder), "dev_agent")

    # A marketplace package carries no user-defined name: the owner never
    # typed one, so the slot stays empty and the interface shows a
    # translation.
    assert bundle["name"] == ""
    assert bundle["name_en"] == "Developer"
    assert bundle["name_ru"] == "Разработчик"
    assert bundle["name_zh"] == "开发者"


def test_orchestrator_package_with_explicit_translation_keys(tmp_path):
    folder = tmp_path / "content"
    _write(folder, "orchestrator.json", {
        "name": "Мой сотрудник",
        "name_en": "My employee",
        "name_ru": "Мой сотрудник",
    })
    _write(folder, "system_prompt.md", "Prompt.\n")

    bundle = _bundle_from_folder(str(folder), "my_worker")

    # A bare name is the user-defined slot; it wins in every language.
    assert bundle["name"] == "Мой сотрудник"
    assert bundle["name_en"] == "My employee"
    assert bundle["name_ru"] == "Мой сотрудник"
    assert bundle["name_zh"] == ""


def test_assistant_package_fills_the_four_name_slots(tmp_path):
    folder = tmp_path / "content"
    _write(folder, "manifest.json", {
        "name": {"en": "Proofreader", "ru": "Корректор"},
        "service": "DeepSeek",
        "model": "deepseek-flash",
    })
    _write(folder, "prompt.md", "You proofread.\n")

    payload = _assistant_bundle_from_folder(str(folder), "korrektor")
    bundle = payload["bundle"]

    assert bundle["name"] == ""
    assert bundle["name_en"] == "Proofreader"
    assert bundle["name_ru"] == "Корректор"
    assert bundle["name_zh"] == ""


def test_catalog_name_follows_the_interface_language():
    from core.marketplace.catalog import localized

    names = {"en": "Developer", "ru": "Разработчик", "zh-CN": "开发者"}

    assert localized(names, "ru") == "Разработчик"
    assert localized(names, "zh-CN") == "开发者"
    assert localized(names, "en") == "Developer"
    # An interface language without a translation shows English rather than
    # an empty card title.
    assert localized(names, "de") == "Developer"


def test_catalog_name_prefers_the_user_defined_slot():
    from core.marketplace.catalog import localized

    value = {"user": "Мой агент", "en": "My agent"}

    for lang in ("en", "ru", "zh"):
        assert localized(value, lang) == "Мой агент"


def test_display_name_resolves_for_the_requested_language():
    from core.marketplace.installer import _display_name

    entity = {
        "id": "dev_agent",
        "name": {"en": "Developer", "ru": "Разработчик", "zh-CN": "开发者"},
    }

    assert _display_name(entity, "ru") == "Разработчик"
    assert _display_name(entity, "zh-CN") == "开发者"
    assert _display_name(entity, "en") == "Developer"
    # Without a translation for the language, English is the answer - not a
    # translation picked by dictionary order.
    assert _display_name(entity, "de") == "Developer"


def test_display_name_keeps_a_plain_string_as_is():
    from core.marketplace.installer import _display_name

    assert _display_name({"id": "x", "name": "Local"}, "ru") == "Local"


def test_assistant_update_keeps_the_user_name_and_takes_a_new_translation():
    payload = {
        "bundle": {
            "name": "",
            "name_en": "Proofreader",
            "name_ru": "",
            "name_zh": "",
            "description": "Proofreads",
        },
        "prompt_text": "Prompt.\n",
    }
    installed_row = {
        "name": "Мой корректор",
        "name_en": "Proofreader",
        "name_ru": "",
        "name_zh": "",
        "description": "Proofreads",
        "text": "Prompt.\n",
    }
    previous = {"name": {"en": "Proofreader"}, "name_en": "Proofreader"}

    merged = _merge_assistant_payload(payload, installed_row, previous, None)

    # The owner renamed the helper: an update must not overwrite that.
    assert merged["bundle"]["name"] == "Мой корректор"
    # The translation was shipped by the package before and is untouched by
    # the user, so it follows the package.
    assert merged["bundle"]["name_en"] == "Proofreader"


def test_assistant_update_fills_an_empty_translation_slot():
    payload = {
        "bundle": {"name": "", "name_en": "", "name_ru": "Корректор",
                   "name_zh": "", "description": ""},
        "prompt_text": "Prompt.\n",
    }
    installed_row = {"name": "", "name_en": "", "name_ru": "", "name_zh": "",
                    "description": ""}
    previous = {"name": {"en": "Proofreader"}}

    merged = _merge_assistant_payload(payload, installed_row, previous, None)

    # An empty slot is a translation the installed version never shipped, not
    # a deliberate choice of the user.
    assert merged["bundle"]["name_ru"] == "Корректор"


def test_orchestrator_update_keeps_the_user_name(monkeypatch):
    import core.orchestrators as orchestrators
    from core.marketplace import agent_orchestrator

    installed_row = {"name_user": "Мой сотрудник", "name_en": "My employee",
                    "name_ru": "", "name_zh": "", "config": {}, "tools": []}
    monkeypatch.setattr(orchestrators, "get_orchestrator", lambda slug: dict(installed_row))

    bundle = {"name": "", "name_en": "My employee", "name_ru": "Мой сотрудник",
              "name_zh": "开发者", "config": {}}
    existing = {"package_sha256": ""}

    merged = agent_orchestrator._merge_orchestrator_update(bundle, existing, "my_worker")

    assert merged["name"] == "Мой сотрудник"
    assert merged["name_ru"] == "Мой сотрудник"
    assert merged["name_zh"] == "开发者"


def test_catalog_bundles_resolve_an_i18n_name():
    from core.marketplace import bundles

    class _FakeSource:
        def fetch_index(self, force=False):
            return {"bundles": [
                {"id": "starter", "name": {"en": "Starter Set", "ru": "Стартовый набор"}},
            ]}

    listed = bundles.catalog_bundles(_FakeSource(), lang="ru")

    assert listed[0]["name"] == "Стартовый набор"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
