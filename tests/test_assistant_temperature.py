# -*- coding: utf-8 -*-
"""tests.test_assistant_temperature - temperature bounds helpers."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock  # noqa: E402

with install_streamlit_mock():
    from ui.pages.assistants import _temperature_bounds, _clamp_temperature  # noqa: E402


def test_temperature_bounds_from_service():
    assert _temperature_bounds({"temp_min": 0.0, "temp_max": 1.0, "temp_step": 0.05}) == (0.0, 1.0, 0.05)


def test_temperature_bounds_fallback():
    assert _temperature_bounds({}) == (0.0, 2.0, 0.05)


def test_temperature_bounds_bad_range():
    assert _temperature_bounds({"temp_min": 1.0, "temp_max": 0.5}) == (0.0, 2.0, 0.05)


def test_clamp_temperature_within_range():
    assert _clamp_temperature(0.3, {"temp_min": 0.0, "temp_max": 1.0}) == 0.3


def test_clamp_temperature_low():
    assert _clamp_temperature(-1.0, {"temp_min": 0.0, "temp_max": 1.0}) == 0.0


def test_clamp_temperature_high():
    assert _clamp_temperature(1.7, {"temp_min": 0.0, "temp_max": 1.0}) == 1.0

def test_clamp_temperature_fallback():
    assert _clamp_temperature(5.0, {}) == 2.0
