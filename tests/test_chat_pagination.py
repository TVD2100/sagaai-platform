# -*- coding: utf-8 -*-
"""tests/test_chat_pagination.py - pagination of the orchestrator chat feed.

The chat tab must render only the trailing window of messages
(CHAT_PAGE_SIZE = 50) instead of the whole history.  A "show earlier"
button above the feed widens the window until every message is visible.
The final assistant message keeps its download/copy controls because it is
always inside the trailing window.

Verified invariants:
1. 120 messages render exactly 50 trailing markdown messages + one
   button whose label contains the remaining count.
2. Clicking the button widens the window in CHAT_PAGE_SIZE steps until
   everything is shown and the button disappears.
3. The i18n key orch_show_earlier exists in all six language files and
   renders with the {count} substitution.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


_LANG_RELS = (
    "defaults/langs/en.json",
    "defaults/langs/ru.json",
    "defaults/langs/zh-CN.json",
    "langs/en.json",
    "langs/ru.json",
    "langs/zh-CN.json",
)


@pytest.fixture()
def ui_env(monkeypatch, tmp_path):
    """Isolated DATA_DIR + fresh ui.* modules under the streamlit mock."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st


def _rerender(st, fn):
    try:
        fn()
    except StopRerun:
        pass


def _orch_dict(slug="custom1"):
    """Minimal orchestrator record for the chat page."""
    return {
        "slug": slug,
        "name": "Custom",
        "description": "",
        "is_builtin": False,
        "prompt_text": "",
        "config": {"strong_service": "DeepSeek", "strong_model": "m1"},
    }


def _prepare_page(ui_env, monkeypatch, slug="custom1", n=120):
    """Wire get_orchestrator and seed an n-message history (alternating)."""
    import ui.pages.orchestrator as orch_page

    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: _orch_dict(slug) if s == slug else None)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    ui_env.session_state.update({"ui_lang": "English"})
    history = []
    for i in range(n):
        history.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"msg-{i}",
            "ts": "",
        })
    ui_env.session_state[f"orch_{slug}_history"] = history
    return orch_page


def _markdown_calls(st):
    return [c[1][0] for c in st.calls if c[0] == "markdown"]


def _button_by_key(st, key):
    for name, args, kwargs in st.calls:
        if name == "button" and kwargs.get("key") == key:
            return args, kwargs
    return None


def test_trailing_window_and_show_earlier_button(ui_env, monkeypatch):
    """120 messages render the 50 trailing ones + the show-earlier button."""
    slug = "custom1"
    orch_page = _prepare_page(ui_env, monkeypatch, slug, n=120)

    _rerender(ui_env, lambda: orch_page.page_orchestrator(slug))

    md = [m for m in _markdown_calls(ui_env) if m.startswith("msg-")]
    assert len(md) == orch_page.CHAT_PAGE_SIZE, f"rendered {len(md)} messages"
    assert md[0] == f"msg-{120 - orch_page.CHAT_PAGE_SIZE}"
    assert md[-1] == "msg-119"

    btn = _button_by_key(ui_env, f"orch_show_earlier_{slug}")
    assert btn is not None, "show-earlier button missing"
    label, _kwargs = btn
    assert f"({120 - orch_page.CHAT_PAGE_SIZE})" in label[0]


def test_show_earlier_clicks_widen_the_window(ui_env, monkeypatch):
    """Each click widens the window by CHAT_PAGE_SIZE; the last click hides
    the button and renders the complete history."""
    slug = "custom1"
    orch_page = _prepare_page(ui_env, monkeypatch, slug, n=120)
    step = orch_page.CHAT_PAGE_SIZE

    _rerender(ui_env, lambda: orch_page.page_orchestrator(slug))

    for expected_visible in (100, 120):
        ui_env.click(f"orch_show_earlier_{slug}")
        _rerender(ui_env, lambda: orch_page.page_orchestrator(slug))
        assert ui_env.session_state[f"orch_{slug}_chat_show_count"] == expected_visible
        ui_env.reset_clicks()
        ui_env.calls.clear()
        _rerender(ui_env, lambda: orch_page.page_orchestrator(slug))
        md = [m for m in _markdown_calls(ui_env) if m.startswith("msg-")]
        assert len(md) == expected_visible, f"rendered {len(md)} messages"
        if expected_visible < 120:
            btn = _button_by_key(ui_env, f"orch_show_earlier_{slug}")
            assert btn is not None
            assert f"({120 - expected_visible})" in btn[0][0]
        else:
            assert _button_by_key(ui_env, f"orch_show_earlier_{slug}") is None


def test_short_history_renders_without_pagination(ui_env, monkeypatch):
    """A history shorter than CHAT_PAGE_SIZE renders fully, button-less."""
    slug = "custom1"
    orch_page = _prepare_page(ui_env, monkeypatch, slug, n=7)

    _rerender(ui_env, lambda: orch_page.page_orchestrator(slug))

    md = [m for m in _markdown_calls(ui_env) if m.startswith("msg-")]
    assert len(md) == 7
    assert _button_by_key(ui_env, f"orch_show_earlier_{slug}") is None


def test_show_earlier_key_in_all_langs():
    """orch_show_earlier is present in every language file and supports
    the {count} placeholder."""
    root = Path(__file__).resolve().parent.parent
    for rel in _LANG_RELS:
        data = json.loads((root / rel).read_text(encoding="utf-8"))
        assert "orch_show_earlier" in data, f"missing key in {rel}"
        assert "{count}" in data["orch_show_earlier"], f"no placeholder in {rel}"
