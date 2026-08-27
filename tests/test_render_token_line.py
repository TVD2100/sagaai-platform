"""Tests for format_token_line and the theme-aware clipboard_button rendering."""

import html as html_lib
import json

from core.render import format_token_line
from tests._st_mock import install_streamlit_mock


def test_format_token_line_basic():
    html = format_token_line(1234, 1000, 2579)
    assert "Context: current <span style=\"color:green;font-weight:600\">1,234</span>" in html
    assert "/ total: 3,579" in html
    assert "(in 1,000 / out 2,579)" in html


def test_format_token_line_with_economy_meta():
    html = format_token_line(53495, 5000000, 2000000, economy_meta="💡 economy (30/122 msgs)")
    assert "💡 economy (30/122 msgs)" in html
    assert "/ total: 7,000,000" in html
    assert "(in 5,000,000 / out 2,000,000)" in html


def test_format_token_line_zeros():
    html = format_token_line(0, 0, 0)
    assert "/ total: 0" in html
    assert "(in 0 / out 0)" in html


def test_format_token_line_custom_color():
    html = format_token_line(10, 5, 5, color="orange")
    assert "color:orange" in html


def test_format_token_line_with_cache():
    html = format_token_line(7849, 24362, 966, tokens_cache=20221)
    assert "(in 24,362 / cache 83% / out 966)" in html


def test_format_token_line_cache_zero_hidden():
    html = format_token_line(100, 50, 10, tokens_cache=0)
    assert "cache" not in html
    assert "(in 50 / out 10)" in html


def test_format_token_line_cache_clamped_at_100():
    html = format_token_line(100, 30, 10, tokens_cache=90)
    assert "cache 100%" in html


def test_format_token_line_cache_ignored_when_in_zero():
    html = format_token_line(100, 0, 10, tokens_cache=5)
    assert "cache" not in html


def _html_calls(st_mock):
    """Clipboard buttons render via st.html(unsafe_allow_javascript=True)."""
    return [c for c in st_mock.calls if c[0] == "html"]


def _attr_value(payload, name):
    start = payload.index(name + chr(61) + chr(34)) + len(name) + 2
    end = payload.index(chr(34), start)
    return html_lib.unescape(payload[start:end])


def _call_payload(st_mock):
    calls = _html_calls(st_mock)
    assert len(calls) == 1, f"expected exactly one st.html call: {calls}"
    assert calls[0][2].get("unsafe_allow_javascript") is True
    return calls[0][1][0]


def test_clipboard_button_with_html_and_newlines():
    """Clipboard content is HTML-escaped and kept intact in data-clip."""
    from core.render import clipboard_button

    text = "<script>alert(1)</script><b>bold</b>" + chr(10) + "line2"
    with install_streamlit_mock() as st_mock:
        clipboard_button(text=text, key="k1", label="📋 MD")

    payload = _call_payload(st_mock)
    assert "📋 MD" in payload
    assert "<script>alert(1)" not in payload
    assert "<b>bold</b>" not in payload
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in payload
    assert json.loads(_attr_value(payload, "data-clip")) == text
    assert _attr_value(payload, "data-label") == "📋 MD"


def test_clipboard_button_with_quotes_and_html_label():
    """Quotes in text and HTML in the label stay safely encoded."""
    from core.render import clipboard_button

    text = "He said " + chr(34) + "hi" + chr(34) + " tail" + chr(10) + "<i>tail</i>"
    label = "A " + chr(34) + " <b>label</b>"
    with install_streamlit_mock() as st_mock:
        clipboard_button(text=text, key="k2", label=label)

    payload = _call_payload(st_mock)
    assert "<b>label</b>" not in payload
    assert "&lt;b&gt;label&lt;/b&gt;" in payload
    assert json.loads(_attr_value(payload, "data-clip")) == text
    assert _attr_value(payload, "data-label") == label


def test_clipboard_button_uses_theme_css_variables():
    """The button must style itself with Streamlit theme variables, not
    fixed colours, so the label stays visible in dark mode too."""
    from core.render import clipboard_button

    with install_streamlit_mock() as st_mock:
        clipboard_button(text="hello", key="k3", label="Copy MD")

    payload = _call_payload(st_mock)
    assert "var(--text-color" in payload
    assert "background: transparent" in payload
    assert "var(--border-color" in payload
    assert "color: inherit" not in payload


def test_clipboard_button_copy_url_params_mode():
    """copy_url_params must be JSON-embedded as data-params for the URL mode."""
    from core.render import clipboard_button

    params = {"orchestrator": "dev_agent", "thread": "20260825_01_abc123"}
    with install_streamlit_mock() as st_mock:
        clipboard_button(text="ignored", key="cp_url_1",
                         label="Copy URL", copy_url_params=params)

    payload = _call_payload(st_mock)
    assert json.loads(_attr_value(payload, "data-params")) == params
    assert "url.searchParams.set" in payload
    assert "new URL(window.location.href)" in payload
