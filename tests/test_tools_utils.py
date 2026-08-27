"""
Unit tests for core.tools_utils.
"""
from core.tools_utils import service_supported_tools

TOOL_DEFS = [
    {"name": "web_search", "desc": "Search the web"},
    {"name": "read_file", "desc": "Read a file"},
    {"name": "propose_file", "desc": "Write a file"},
]


def test_no_service_def_returns_empty():
    assert service_supported_tools(None) == []
    assert service_supported_tools({}) == []


def test_missing_tools_options_returns_empty():
    svc = {"name": "GigaChat", "models": []}
    assert service_supported_tools(svc) == []


def test_tools_options_dict_keys():
    svc = {"tools_options": [{"key": "web_search"}]}
    assert service_supported_tools(svc) == ["web_search"]


def test_tools_options_strings():
    svc = {"tools_options": ["web_search", "read_file"]}
    assert service_supported_tools(svc) == ["web_search", "read_file"]


def test_tools_options_unknown_keys():
    svc = {"tools_options": [{"key": "web_search"}, {"key": "not_a_real_tool"}]}
    assert service_supported_tools(svc) == ["web_search", "not_a_real_tool"]


def test_filters_by_catalog():
    svc = {"tools_options": [{"key": "web_search"}, {"key": "not_known"}]}
    result = service_supported_tools(svc, TOOL_DEFS)
    assert result == ["web_search"]


def test_empty_tools_options_returns_empty():
    assert service_supported_tools({"tools_options": []}) == []


def test_malformed_entries_skipped():
    svc = {"tools_options": [None, 42, {"label": "no key"}, "  "]}
    assert service_supported_tools(svc) == []
