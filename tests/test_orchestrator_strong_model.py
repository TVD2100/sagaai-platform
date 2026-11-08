# -*- coding: utf-8 -*-
"""tests/test_orchestrator_strong_model.py - which model an employee runs on.

An agent installed from a catalog does not have to name a model: the
platform supplies one. The resolution has a single implementation, so the
chat page (which decides whether the agent can run at all) and the prompt
builder (which actually calls the provider) always agree on the answer.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from core.orchestrator_strong import (  # noqa: E402
    DEFAULT_STRONG_MODEL,
    DEFAULT_STRONG_SERVICE,
    get_strong_model,
    resolve_strong_model,
)


def test_empty_config_resolves_to_the_platform_defaults():
    """A config that names no model still names one after resolution."""
    resolved = resolve_strong_model({})

    assert resolved == {
        "service": DEFAULT_STRONG_SERVICE,
        "model": DEFAULT_STRONG_MODEL,
    }
    assert resolved["service"] and resolved["model"]


def test_explicit_strong_pair_wins_over_every_other_key():
    cfg = {
        "strong_service": "YandexAI",
        "strong_model": "yandexgpt",
        "service": "DeepSeek",
        "model": "deepseek-flash",
    }

    assert resolve_strong_model(cfg) == {
        "service": "YandexAI",
        "model": "yandexgpt",
    }


def test_legacy_single_model_keys_are_honoured():
    """A config written in the older ``service``/``model`` shape keeps working."""
    assert resolve_strong_model(
        {"service": "CustomSvc", "model": "custom-model"}
    ) == {"service": "CustomSvc", "model": "custom-model"}


def test_the_pair_resolves_independently():
    """A config may name the provider and leave the model to the platform."""
    assert resolve_strong_model({"strong_service": "YandexAI"}) == {
        "service": "YandexAI",
        "model": DEFAULT_STRONG_MODEL,
    }
    assert resolve_strong_model({"strong_model": "yandexgpt"}) == {
        "service": DEFAULT_STRONG_SERVICE,
        "model": "yandexgpt",
    }


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_values_fall_through_to_the_next_rung(value):
    cfg = {"strong_service": value, "service": "CustomSvc",
           "strong_model": value, "model": "custom-model"}

    assert resolve_strong_model(cfg) == {
        "service": "CustomSvc",
        "model": "custom-model",
    }


def test_a_non_mapping_config_is_treated_as_empty():
    """A malformed stored config must not crash the resolution."""
    assert resolve_strong_model(None) == {
        "service": DEFAULT_STRONG_SERVICE,
        "model": DEFAULT_STRONG_MODEL,
    }


def test_an_unknown_slug_has_no_model_at_all(monkeypatch):
    """A missing employee is not the same as an employee without a model."""
    import core.orchestrators as facade

    monkeypatch.setattr(facade, "get_orchestrator", lambda slug: None)

    assert get_strong_model("gone") == {"service": "", "model": ""}


def test_a_stored_employee_resolves_through_its_own_config(monkeypatch):
    import core.orchestrators as facade

    monkeypatch.setattr(
        facade, "get_orchestrator",
        lambda slug: {"slug": slug, "config": {"strong_service": "YandexAI"}},
    )

    assert get_strong_model("pm") == {
        "service": "YandexAI",
        "model": DEFAULT_STRONG_MODEL,
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
