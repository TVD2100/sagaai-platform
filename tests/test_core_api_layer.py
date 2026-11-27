"""
tests.test_core_api_layer - unit tests for core.api_layer.

Tests for pure functions that do NOT require network mocking:
_normalise_tools, _extract_responses_text, _prepare_response_content.

Tests requiring network mocks (send_request, _bearer_request, etc.)
are in test_core_api_send.py.
"""

# ─── _normalise_tools ──────────────────────────────────────────────────────

def test_normalise_tools_strings():
    from core.api_layer import _normalise_tools
    tools = ["web_search", "code_interpreter"]
    expected = [
        {"type": "web_search"},
        {"type": "code_interpreter"},
    ]
    assert _normalise_tools(tools) == expected


def test_normalise_tools_dicts():
    from core.api_layer import _normalise_tools
    tools = [
        {"type": "web_search", "filters": {"allowed_domains": ["python.org"]}},
        {"type": "code_interpreter"},
    ]
    assert _normalise_tools(tools) == tools


def test_normalise_tools_mixed():
    from core.api_layer import _normalise_tools
    tools = [
        "web_search",
        {"type": "code_interpreter", "max_tool_calls": 5},
        "python",
    ]
    expected = [
        {"type": "web_search"},
        {"type": "code_interpreter", "max_tool_calls": 5},
        {"type": "python"},
    ]
    assert _normalise_tools(tools) == expected


def test_normalise_tools_empty():
    from core.api_layer import _normalise_tools
    assert _normalise_tools([]) == []
    assert _normalise_tools(None) == []


# ─── _to_chat_tools ────────────────────────────────────────────────────────

def test_to_chat_tools_nests_flat_function_tool():
    """A Responses-shape function tool is wrapped one level deeper."""
    from core.api_chat import _to_chat_tools
    flat = {
        "type": "function",
        "name": "calculate",
        "description": "Evaluate arithmetic",
        "parameters": {"type": "object", "properties": {}},
    }
    converted = _to_chat_tools([flat])
    assert converted == [{
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate arithmetic",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    # The source tool must not be mutated in place.
    assert flat["name"] == "calculate"
    assert "function" not in flat


def test_to_chat_tools_passes_nested_function_tool_through():
    """An already-nested tool is left untouched, making the call idempotent."""
    from core.api_chat import _to_chat_tools
    nested = {"type": "function", "function": {"name": "calculate"}}
    assert _to_chat_tools([nested]) == [nested]
    assert _to_chat_tools(_to_chat_tools([nested])) == [nested]


def test_to_chat_tools_passes_server_tools_through():
    """Non-function tools keep their shape - only function tools are nested."""
    from core.api_chat import _to_chat_tools
    tools = [
        {"type": "web_search", "filters": {"allowed_domains": ["python.org"]}},
        {"type": "code_interpreter"},
    ]
    assert _to_chat_tools(tools) == tools


def test_to_chat_tools_converts_strings_and_drops_unknown():
    from core.api_chat import _to_chat_tools
    converted = _to_chat_tools(["web_search", 42, None, {"type": "function", "name": "x"}])
    assert converted[0] == {"type": "web_search"}
    assert converted[1] == {"type": "function", "function": {"name": "x"}}
    assert len(converted) == 2


def test_to_chat_tools_empty():
    from core.api_chat import _to_chat_tools
    assert _to_chat_tools([]) == []
    assert _to_chat_tools(None) == []


# ─── _merge_extra_payload ──────────────────────────────────────────────────

def test_merge_extra_payload_adds_new_keys():
    """Provider-specific fields land in the body next to the platform keys."""
    from core.api_chat import _merge_extra_payload
    payload = {"model": "m", "messages": []}
    _merge_extra_payload(payload, {"chat_template_kwargs": {"enable_thinking": True}})
    assert payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert payload["model"] == "m"


def test_merge_extra_payload_never_overrides_platform_keys():
    """A catalog entry cannot replace the model, the conversation or tools."""
    from core.api_chat import _merge_extra_payload
    payload = {
        "model": "real-model",
        "messages": [{"role": "user", "content": "q"}],
        "tools": [{"type": "web_search"}],
        "temperature": 0.3,
        "max_tokens": 900,
    }
    _merge_extra_payload(payload, {
        "model": "hijacked",
        "messages": [],
        "tools": [],
        "temperature": 2.0,
        "max_tokens": 1,
        "response_format": {"type": "text"},
        "reasoning": {"effort": "high"},
    })
    assert payload["model"] == "real-model"
    assert payload["messages"] == [{"role": "user", "content": "q"}]
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["temperature"] == 0.3
    assert payload["max_tokens"] == 900
    assert "response_format" not in payload
    # A genuinely provider-specific key still gets through.
    assert payload["reasoning"] == {"effort": "high"}


def test_merge_extra_payload_empty_is_noop():
    from core.api_chat import _merge_extra_payload
    payload = {"model": "m"}
    _merge_extra_payload(payload, None)
    _merge_extra_payload(payload, {})
    assert payload == {"model": "m"}


# ─── _extract_responses_text ───────────────────────────────────────────────

def test_extract_responses_text_from_output_text():
    from core.api_layer import _extract_responses_text
    data = {"output_text": "Привет, мир!"}
    assert _extract_responses_text(data) == "Привет, мир!"


def test_extract_responses_text_from_output_blocks():
    from core.api_layer import _extract_responses_text
    data = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"text": "Первый блок."},
                    {"text": "Второй блок."}
                ]
            },
            {"type": "other", "content": [{"text": "игнорируется"}]}
        ]
    }
    assert _extract_responses_text(data) == "Первый блок.\n\nВторой блок."


def test_extract_responses_text_empty():
    from core.api_layer import _extract_responses_text
    assert _extract_responses_text({}) == ""
    assert _extract_responses_text({"output_text": ""}) == ""
    assert _extract_responses_text({"output": []}) == ""
    assert _extract_responses_text({"output": [{"type": "other", "content": []}]}) == ""


# ─── _prepare_response_content ─────────────────────────────────────────────

def test_prepare_response_content_text_only():
    from core.api_layer import _prepare_response_content
    msg = {"content": "Ответ модели"}
    assert _prepare_response_content(msg) == "Ответ модели"


def test_prepare_response_content_ignores_reasoning_when_content_empty():
    """DeepSeek thinking mode: reasoning_content must never leak.

    Regression: _prepare_response_content used to fall back to
    'reasoning_content' when 'content' was empty, which surfaced the
    model's chain-of-thought in the UI and in parse_tool_calls.
    """
    from core.api_layer import _prepare_response_content
    msg = {"content": "", "reasoning_content": "internal thought"}
    assert _prepare_response_content(msg) == ""


def test_prepare_response_content_ignores_reasoning_when_content_present():
    """reasoning_content is dropped even when content exists."""
    from core.api_layer import _prepare_response_content
    msg = {"content": "Ответ модели", "reasoning_content": "internal thought"}
    result = _prepare_response_content(msg)
    assert result == "Ответ модели"
    assert "internal thought" not in result


def test_prepare_response_content_tool_calls_only():
    from core.api_layer import _prepare_response_content
    msg = {
        "tool_calls": [
            {
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "README.md"}'
                }
            }
        ]
    }
    result = _prepare_response_content(msg)
    assert "read_file" in result
    assert "README.md" in result
    assert result.startswith("```json")


def test_prepare_response_content_both():
    from core.api_layer import _prepare_response_content
    msg = {
        "content": "Проверяю файл.",
        "tool_calls": [
            {
                "function": {
                    "name": "list_files",
                    "arguments": '{}'
                }
            }
        ]
    }
    result = _prepare_response_content(msg)
    assert "Проверяю файл." in result
    assert "list_files" in result


def test_prepare_response_content_empty():
    from core.api_layer import _prepare_response_content
    assert _prepare_response_content({}) == ""
    assert _prepare_response_content({"content": ""}) == ""
    assert _prepare_response_content({"content": "   "}) == ""


def test_prepare_response_content_broken_tool_args():
    from core.api_layer import _prepare_response_content
    msg = {
        "tool_calls": [
            {
                "function": {
                    "name": "bad_tool",
                    "arguments": "not-json"
                }
            }
        ]
    }
    result = _prepare_response_content(msg)
    assert "bad_tool" in result
    assert "args" in result
