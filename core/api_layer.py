"""
core.api_layer - HTTP requests to AI service APIs.
No streamlit imports. Uses core.config and core.services.

All failures are raised as subclasses of APIError (core.api_errors).
Callers should catch APIError for user-facing messages and inspect
``exc.code`` for programmatic handling.
"""
from typing import Optional

import requests

from core.api_bearer import bearer_request_impl  # noqa: F401 - delegation target
from core.api_deepseek import (  # noqa: F401 - re-export surface
    _ANTHROPIC_VERSION,
    _DEEPSEEK_REASONING_EFFORTS,
    _DEEPSEEK_SERVER_WEB_SEARCH_TOOL,
    _MAX_USES_BY_CONTEXT,
    _WEB_SEARCH_TOOL_NAME,
    _anthropic_web_search_used,
    _deepseek_reasoning_effort,
    _extract_anthropic_text,
)
from core.api_deepseek import (
    _deepseek_anthropic_web_search as deepseek_anthropic_web_search_impl,
)
from core.api_deepseek import (
    _deepseek_responses_request as deepseek_responses_request_impl,
)
from core.api_errors import (
    APIError,
    ApiKeyMissingError,
    AuthTypeUnknownError,
    NetworkError,
    ProviderHTTPError,
    RequestTimeoutError,
    ServiceNotFoundError,
)
from core.api_gigachat import (  # noqa: F401 - re-export surface
    _gigachat_messages,
    gigachat_token,
)
from core.api_gigachat import (
    gigachat_models_url as _gigachat_models_url,
)
from core.api_history_guard import (  # noqa: F401 - re-export surface
    _SANITIZED_REASON,
    _parse_sanitized_info,
    _protect_history,
)
from core.api_http import (  # noqa: F401 - re-export surface
    _DEFAULT_RETRY_ATTEMPTS,
    _DEFAULT_RETRY_DELAY,
    _GIGACHAT_DEFAULT_CA_BUNDLE,
    _VERIFY_TLS,
    MODEL_REQUEST_TIMEOUT,
    MODEL_RESPONSE_TIMEOUT,
    _extract_error_body,
    _extract_gigachat_error,
    _retry_params,
    gigachat_verify_impl,
    retry_call,
)
from core.api_payload import (  # noqa: F401 - re-export surface
    _DEFAULT_MAX_TOKENS,
    _clamp_max_tokens,
    _content_has_material,
    _estimate_tokens_in,
    _get_model_max_tokens,
    _gigachat_response_format,
    _has_native_function_tools,
    _normalise_json_schema,
    _normalise_tools,
    _openai_response_format,
    _responses_json_format,
)
from core.api_responses import (  # noqa: F401 - re-export surface
    _extract_deepseek_responses_text,
    _extract_openai_message_text,
    _extract_responses_text,
    _format_function_call_item,
    _is_schema_rejection,
    _prepare_response_content,
    _raise_responses_failure,
    _responses_failure_details,
    _responses_failure_message,
    _responses_incomplete_details,
    _unwrap_json_text,
)
from core.api_yandex import (  # noqa: F401 - re-export surface
    _YANDEX_REASONING_EFFORTS,
    yandex_responses_request_impl,
)
from core.api_yandex import (  # noqa: F401 - re-export surface
    yandex_reasoning_effort as _yandex_reasoning_effort,
)
from core.api_yandex import (
    yandex_web_search_config as _yandex_web_search_config,
)
from core.assistants import load_assistant_files_context
from core.config import load_config
from core.fs import combine_nonempty
from core.prompt_guard import (  # noqa: F401 - re-export surface
    detect_injection_signatures,
    is_tool_result_text,
    sanitize_text,
    sanitize_tool_result_content,
    wrap_data,
)
from core.services import get_services

# Legacy alias (old "skill" terminology) for backward compatibility.
load_skill_files_context = load_assistant_files_context

# Timeouts for a model request; defined in core.api_http and re-exported.


# ─── TLS verification policy ────────────────────────────────────────────────
# The policy itself and the GigaChat CA bundle live in core.api_http; they are
# re-exported below so existing readers and tests keep addressing them here.
def _gigachat_verify():
    """Return the ``verify`` argument for GigaChat requests.

    Delegates to core.api_http with the values held on this module, so a caller
    that replaces ``_VERIFY_TLS`` or ``_GIGACHAT_DEFAULT_CA_BUNDLE`` here still
    changes the outcome.
    """
    return gigachat_verify_impl(_VERIFY_TLS, _GIGACHAT_DEFAULT_CA_BUNDLE)


# ─── history sanitising (moved to core.api_history_guard) ───────────────────

# ─── structured output (native JSON Schema) ─────────────────────────────────
# ─── Responses API failure detection / input sanitising ─────────────────────


# ─── Incomplete (truncated) Responses detection ─────────────────────────────


# ─── history sanitising (moved to core.api_history_guard) ───────────────────

# ─── token estimation (moved to core.api_payload) ───────────────────────────

def _bearer_request(url: str, api_key: str, model: str, messages: list, temperature: float,
                    tools: Optional[list] = None, usage_callback=None, max_tokens: Optional[int] = None,
                    service: str = "", json_schema=None) -> str:
    """Send one chat/completions request; delegates to core.api_bearer."""
    return bearer_request_impl(
        url, api_key, model, messages, temperature,
        tools=tools, usage_callback=usage_callback, max_tokens=max_tokens,
        service=service, json_schema=json_schema,
    )


def _deepseek_responses_request(base_url: str, api_key: str, model: str, sys_text: str,
                                hist_msgs: list, user_content: str, *, max_tokens: Optional[int] = None,
                                tools_list: Optional[list] = None, max_tool_calls=None,
                                reasoning_effort: Optional[str] = None, cfg: Optional[dict] = None,
                                svc_name: str = "", usage_callback=None, incomplete_callback=None,
                                tool_choice=None, json_schema=None) -> str:
    """Send one DeepSeek Responses request; delegates to core.api_deepseek."""
    return deepseek_responses_request_impl(
        base_url, api_key, model, sys_text, hist_msgs, user_content,
        max_tokens=max_tokens, tools_list=tools_list,
        max_tool_calls=max_tool_calls, reasoning_effort=reasoning_effort,
        cfg=cfg, svc_name=svc_name, usage_callback=usage_callback,
        incomplete_callback=incomplete_callback,
        tool_choice=tool_choice, json_schema=json_schema,
    )


def _deepseek_anthropic_web_search(
        base_url: str, api_key: str, model: str,
        system_prompt: str, query: str,
        *, allowed_domains: Optional[list] = None,
        search_context_size: Optional[str] = None,
        max_tokens: int = 4096, svc_name: str = "",
        timeout: tuple = MODEL_REQUEST_TIMEOUT) -> dict:
    """Run a DeepSeek web search; delegates to core.api_deepseek."""
    return deepseek_anthropic_web_search_impl(
        base_url, api_key, model, system_prompt, query,
        allowed_domains=allowed_domains,
        search_context_size=search_context_size,
        max_tokens=max_tokens, svc_name=svc_name, timeout=timeout,
    )


def _assistant_web_search_config(assistant: Optional[dict], cfg: dict,
                                 svc_name: str) -> tuple:
    """Return (context_size, allowed_domains) for an assistant's web search.

    Priority:
      1. Per-assistant overrides from DATA_DIR/assistants/<slug>/manifest.json
         (``web_search_context_size`` / ``web_search_allowed_domains``);
      2. Provider-level persisted config (same keys);
      3. Defaults (medium / empty domains).
    """
    ctx_size, allowed = _yandex_web_search_config(cfg or {}, svc_name)
    if not assistant:
        return ctx_size, allowed
    try:
        from core.assistant_folders import get_assistant_web_search_settings
        slug = str(assistant.get("slug") or "")
        if not slug:
            return ctx_size, allowed
        settings = get_assistant_web_search_settings(slug) or {}
        if settings.get("context_size") in ("low", "medium", "high"):
            ctx_size = settings["context_size"]
        doms = settings.get("allowed_domains")
        if isinstance(doms, list) and doms:
            allowed = list(dict.fromkeys(str(d).strip() for d in doms if str(d).strip()))
    except Exception:
        pass  # fall back to provider-level defaults
    return ctx_size, allowed


def _yandex_responses_request(base_url: str, api_key: str, folder_id: str,
                              model: str, sys_text: str, hist_msgs: list,
                              user_content: str, *, temperature: float,
                              max_tokens: Optional[int] = None,
                              tools_list: Optional[list] = None,
                              max_tool_calls=None,
                              reasoning_effort: Optional[str] = None,
                              cfg: Optional[dict] = None,
                              svc_name: str = "", usage_callback=None,
                              incomplete_callback=None,
                              tool_choice=None, svc: Optional[dict] = None,
                              assistant: Optional[dict] = None,
                              json_schema=None) -> str:
    """Send one Yandex Responses request; delegates to core.api_yandex."""
    return yandex_responses_request_impl(
        base_url, api_key, folder_id, model, sys_text, hist_msgs, user_content,
        temperature=temperature, max_tokens=max_tokens, tools_list=tools_list,
        max_tool_calls=max_tool_calls, reasoning_effort=reasoning_effort,
        cfg=cfg, svc_name=svc_name, usage_callback=usage_callback,
        incomplete_callback=incomplete_callback, tool_choice=tool_choice,
        svc=svc, assistant=assistant, json_schema=json_schema,
        assistant_web_search_config=_assistant_web_search_config,
    )


def _gigachat_token(credentials: str, scope: str = "GIGACHAT_API_PERS") -> str:
    """Obtain an OAuth token for GigaChat; delegates to core.api_gigachat.

    The TLS policy is read here so a caller that replaces ``_gigachat_verify``
    or ``_VERIFY_TLS`` on this module keeps control over the request.
    """
    return gigachat_token(credentials, scope, verify=_gigachat_verify())


# ─── multimodal requests (vision / image generation) ────────────────────────
# Image analysis goes through the OpenAI-compatible chat/completions endpoint:
# Yandex AI Studio and any bearer service accept the standard
# {"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}
# content part. Image generation uses the OpenAI-compatible Images API:
# Yandex AI Studio (Alice AI ART 3.0) receives the model URI
# art://<folder_id>/<model>/latest plus the folder id in the
# OpenAI-Project / x-project headers and returns the finished image as a
# base64 payload (data[0].b64_json).


def send_vision_request(service: str, model: str, prompt: str, images: list,
                        *, temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                        timeout: tuple = MODEL_REQUEST_TIMEOUT) -> str:
    """Send an image-analysis (vision) request; return the answer text.

    *images*: list of {"mime": "image/jpeg", "data": "<base64>"} dicts
    (built by :mod:`core.multimodal`). ``yandex_iam`` services use the
    model URI ``gpt://<folder_id>/<model>`` on
    ``<base_url>/chat/completions``; ``bearer`` services post to their
    configured base_url as-is (it must already point at a
    chat/completions endpoint) and receive ``max_tokens`` instead of
    ``max_completion_tokens``. ``deepseek_responses`` services (the
    default base_url is the text-only Responses API) post to the
    dedicated ``vision_base_url`` with the plain model id, ``max_tokens``
    and ``detail="original"`` on every image (better OCR of small text).

    Raises the standard APIError subclasses (ServiceNotFoundError,
    ApiKeyMissingError, AuthTypeUnknownError, ProviderHTTPError,
    RequestTimeoutError, NetworkError).
    """
    services = get_services()
    svc = services.get(service)
    if not svc:
        raise ServiceNotFoundError(service)

    auth_type = svc.get("auth_type", "bearer")
    if auth_type not in ("bearer", "yandex_iam", "deepseek_responses"):
        raise AuthTypeUnknownError(service, auth_type)

    cfg = load_config()
    api_key = cfg.get(svc.get("config_key", ""), "")
    if isinstance(api_key, str):
        api_key = api_key.strip()
    if not api_key:
        field = "IAM token" if auth_type == "yandex_iam" else "api key"
        raise ApiKeyMissingError(service, field=field)

    base_url = str(svc.get("base_url", "")).rstrip("/")
    if auth_type == "yandex_iam":
        folder_id = cfg.get(svc.get("config_key2", ""), "")
        if isinstance(folder_id, str):
            folder_id = folder_id.strip()
        if not folder_id:
            raise ApiKeyMissingError(service, field="Folder ID")
        model_name = f"gpt://{folder_id}/{model}"
    elif auth_type == "deepseek_responses":
        # DeepSeek: the default base_url points at the Responses API, so
        # vision requests use the dedicated chat/completions endpoint.
        base_url = str(svc.get("vision_base_url") or base_url).rstrip("/")
        model_name = str(model)
    else:
        model_name = str(model)
    url = base_url
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"

    content_parts: list = []
    if prompt and str(prompt).strip():
        content_parts.append({"type": "text", "text": str(prompt)})
    for image in images or []:
        if not isinstance(image, dict):
            continue
        data = str(image.get("data") or "")
        if not data:
            continue
        mime = str(image.get("mime") or "image/jpeg")
        image_url: dict = {"url": f"data:{mime};base64,{data}"}
        if auth_type == "deepseek_responses":
            # DeepSeek reads small text best at the original resolution.
            image_url["detail"] = "original"
        content_parts.append({
            "type": "image_url",
            "image_url": image_url,
        })
    if not content_parts:
        raise APIError("vision request carries no prompt or images",
                       service=service)

    payload: dict = {
        "model": model_name,
        "messages": [{"role": "user", "content": content_parts}],
    }
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if max_tokens:
        key = "max_completion_tokens" if auth_type == "yandex_iam" else "max_tokens"
        payload[key] = int(max_tokens)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    try:
        r = requests.post(url, headers=headers, json=payload,
                          timeout=timeout, verify=_VERIFY_TLS)
    except requests.exceptions.Timeout:
        raise RequestTimeoutError(service=service)
    except requests.exceptions.RequestException as e:
        raise NetworkError(str(e), service=service)
    if r.status_code != 200:
        raise ProviderHTTPError(r.status_code, _extract_error_body(r),
                                service=service)

    data = r.json() or {}
    choices = data.get("choices") or []
    message = choices[0].get("message") if (choices and isinstance(choices[0], dict)) else {}
    return _extract_openai_message_text(message or {})


def send_image_generation_request(service: str, model: str, prompt: str,
                                  *,
                                  timeout: tuple = MODEL_REQUEST_TIMEOUT) -> dict:
    """Generate an image from a text prompt; return {"mime", "data", ...}.

    Both auth modes use the OpenAI-compatible synchronous Images API
    (``<base_url>/images/generations``, ``response_format="b64_json"`` and
    the image in ``data[0].b64_json``). ``yandex_iam`` services (Yandex AI
    Studio) receive the model URI ``art://<folder_id>/<model>/latest`` and
    the folder id in the ``OpenAI-Project`` / ``x-project`` headers;
    ``bearer`` services get the plain model id and no folder headers.

    Raises the standard APIError subclasses (ServiceNotFoundError,
    ApiKeyMissingError, AuthTypeUnknownError, ProviderHTTPError,
    RequestTimeoutError, NetworkError).
    """
    services = get_services()
    svc = services.get(service)
    if not svc:
        raise ServiceNotFoundError(service)

    auth_type = svc.get("auth_type", "bearer")
    if auth_type not in ("bearer", "yandex_iam"):
        raise AuthTypeUnknownError(service, auth_type)

    cfg = load_config()
    api_key = cfg.get(svc.get("config_key", ""), "")
    if isinstance(api_key, str):
        api_key = api_key.strip()
    if not api_key:
        field = "IAM token" if auth_type == "yandex_iam" else "api key"
        raise ApiKeyMissingError(service, field=field)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    text = str(prompt or "").strip()

    if auth_type == "yandex_iam":
        folder_id = cfg.get(svc.get("config_key2", ""), "")
        if isinstance(folder_id, str):
            folder_id = folder_id.strip()
        if not folder_id:
            raise ApiKeyMissingError(service, field="Folder ID")
        model_name = f"art://{folder_id}/{model}/latest"
        headers["OpenAI-Project"] = folder_id
        headers["x-project"] = folder_id
    else:
        folder_id = ""
        model_name = str(model)

    url = str(svc.get("base_url", "")).rstrip("/")
    if not url.endswith("/images/generations"):
        url = f"{url}/images/generations"
    payload = {
        "model": model_name,
        "prompt": text,
        "response_format": "b64_json",
    }
    try:
        r = requests.post(url, headers=headers, json=payload,
                          timeout=timeout, verify=_VERIFY_TLS)
    except requests.exceptions.Timeout:
        raise RequestTimeoutError(service=service)
    except requests.exceptions.RequestException as e:
        raise NetworkError(str(e), service=service)
    if r.status_code != 200:
        body = _extract_error_body(r)
        if r.status_code == 403 and folder_id:
            body += (". Check that the service account behind the API key/IAM "
                     f"token has the 'ai.imageGeneration.user' role on folder '{folder_id}'")
        raise ProviderHTTPError(r.status_code, body, service=service)
    data = r.json() or {}
    items = data.get("data") or []
    first = items[0] if (items and isinstance(items[0], dict)) else {}
    image_b64 = str(first.get("b64_json") or "")
    if not image_b64:
        raise ProviderHTTPError(
            200, "image generation returned no b64_json payload",
            service=service)
    return {"mime": "image/jpeg", "data": image_b64,
            "service": service, "model": str(model)}


def _assistant_rag_context(assistant: dict, user_message: str) -> str:
    """Build an auto-RAG context block from the assistant's bound knowledge bases.

    The binding is stored in the assistant folder manifest under the
    ``rag_bases`` key (list of base slugs). Bases with non-empty
    ``rag_slots`` must include the assistant slug / id / name to be used.
    Failures are swallowed so chat continues with an empty context.
    """
    try:
        from core.assistant_folders import load_assistant_bundle
        from core.assistants import get_assistant_by_id
        from core.rag import get_base
        from core.rag_search import chat_context

        a_id = str(assistant.get("id") or "")
        slug = str(assistant.get("slug") or "")
        if not slug and a_id:
            full = get_assistant_by_id(a_id)
            if full:
                slug = str(full.get("slug") or "")
        if not slug:
            return ""
        bundle = load_assistant_bundle(slug) or {}
        bases = [
            str(b).strip().lower()
            for b in (bundle.get("rag_bases") or [])
            if str(b).strip()
        ]
        if not bases:
            return ""
        allowed_names = {
            slug.lower(),
            a_id.lower(),
            str(assistant.get("name") or "").strip().lower(),
        }
        parts = []
        for bslug in bases:
            b = get_base(bslug, with_stats=False)
            if not b:
                continue
            slots = {str(s).strip().lower() for s in (b.get("rag_slots") or [])}
            if slots and not slots.intersection(allowed_names):
                continue
            ctx = chat_context(bslug, str(user_message))
            if ctx:
                parts.append(ctx)
        return "\n\n".join(parts)
    except Exception:
        return ""


# ─── request preparation (phases of send_request) ────────────────────────────


def _resolve_chat_service(assistant: dict) -> tuple:
    """Return ``(cfg, services, svc_name, svc)`` for *assistant*.

    Raises:
        ServiceNotFoundError - the assistant's service is not registered.
    """
    cfg = load_config()
    services = get_services()
    svc_name = assistant.get("service", "")
    svc = services.get(svc_name)
    if not svc:
        raise ServiceNotFoundError(svc_name)
    return cfg, services, svc_name, svc


def _resolve_generation_settings(assistant: dict, svc: dict) -> tuple:
    """Return ``(model, temp, sys_text, max_tokens)`` for one request.

    ``assistant["model"]`` is a direct index on purpose: a missing key must
    surface as ``KeyError`` instead of silently sending an empty model.
    """
    model = assistant["model"]
    temp = float(assistant.get("temperature", svc.get("temp_default", 0.7)))
    sys_text = assistant.get("text", "")
    # Clamp to the model's explicit output limit (e.g. GigaChat <= 32768)
    # so a value saved for another model is not rejected by the provider.
    max_tokens = _clamp_max_tokens(
        assistant.get("max_tokens") or _get_model_max_tokens(svc, model),
        svc, model,
    )
    return model, temp, sys_text, max_tokens


def _compose_user_content(assistant: dict, file_context: str,
                          user_message: str) -> str:
    """Build the final user turn from file context, auto-RAG and the message.

    Untrusted content (assistant attachments, user-provided file context) is
    marked as DATA before it reaches the model. Assistants with a native
    ``rag_search`` function tool search their bases themselves through the
    function-calling loop, so the automatic context is skipped for them: it
    would duplicate the search and would use the raw message without history.
    """
    assistant_file_ctx = load_assistant_files_context(assistant.get("id", ""))
    combined_file_ctx = combine_nonempty([
        f"**Материалы помощника:**\n{assistant_file_ctx}" if assistant_file_ctx else "",
        f"**Файл пользователя:**\n{file_context}"   if file_context   else "",
    ])
    protected_file_ctx = wrap_data(
        sanitize_text(combined_file_ctx, max_len=20000),
        source="file_context",
    ) if combined_file_ctx.strip() else ""

    if _has_native_function_tools(assistant.get("tools", [])):
        rag_ctx = ""
    else:
        rag_ctx = _assistant_rag_context(assistant, user_message)
    protected_rag_ctx = wrap_data(
        sanitize_text(rag_ctx, max_len=20000),
        source="rag_context",
    ) if rag_ctx.strip() else ""

    if protected_file_ctx and not user_message.strip():
        user_content = (
            "Проанализируй содержимое файла согласно системному промпту."
            f"\n\n---\n**Контекст:**\n{protected_file_ctx}"
        )
    elif protected_file_ctx:
        user_content = (
            f"{user_message}"
            f"\n\n---\n**Контекст из файлов:**\n{protected_file_ctx}"
        )
    elif protected_rag_ctx:
        user_content = f"{user_message}"
    else:
        return user_message
    if protected_rag_ctx:
        user_content += f"\n\n---\n**База знаний:**\n{protected_rag_ctx}"
    return user_content


def _resolve_tool_options(assistant: dict, auth_type: str) -> tuple:
    """Return the per-assistant tool and structured-output options.

    Providers with an unreliable auto tool invocation: Yandex models
    (aliceai-*) may answer with placeholder text instead of calling
    web_search when tool_choice is "auto". An assistant that declares a
    web_search tool and sets no explicit tool_choice gets the search forced
    for yandex_iam. DeepSeek is intentionally left UNFORCED: a forced
    tool_choice makes it loop through many searches and finish with an empty
    answer, so the prompt-based "exactly one search" rule is used instead.
    """
    tools_list = assistant.get("tools", [])
    max_tool_calls = assistant.get("max_tool_calls", None)
    native_function_tools = _has_native_function_tools(tools_list)
    json_schema = assistant.get("json_schema")
    reasoning_effort = assistant.get("reasoning_effort")
    tool_choice = assistant.get("tool_choice", None)
    if tool_choice is None and auth_type == "yandex_iam" and not native_function_tools:
        for t in tools_list:
            t_type = t if isinstance(t, str) else (t.get("type") if isinstance(t, dict) else None)
            if t_type == "web_search":
                tool_choice = {"type": "web_search"}
                break
    return (tools_list, max_tool_calls, native_function_tools,
            json_schema, reasoning_effort, tool_choice)


def _run_with_retry(request_kwargs: dict, native_function_tools: bool,
                    json_schema, retry_callback=None) -> str:
    """Run one logical request, retrying transient transport failures.

    ``request_kwargs`` carries every argument of :func:`_do_request` except
    ``json_schema``, which is chosen per attempt: a provider that rejects the
    schema is retried once without it.
    """

    def _send_once(with_schema: bool) -> str:
        # One physical attempt: transport errors are normalised into the
        # APIError contract so retry_call can decide whether to re-send.
        try:
            return _do_request(
                json_schema=json_schema if with_schema else None,
                retry_callback=retry_callback,
                **request_kwargs
            )
        except APIError:
            raise
        except requests.exceptions.Timeout:
            raise RequestTimeoutError(service=request_kwargs.get("svc_name"))
        except requests.exceptions.RequestException as e:
            raise NetworkError(str(e), service=request_kwargs.get("svc_name"))
        except Exception as e:
            raise NetworkError(str(e), service=request_kwargs.get("svc_name"))

    def _call(with_schema: bool) -> str:
        if native_function_tools:
            # Every physical POST inside the Yandex tool loop already retries
            # individually (see core.assistant_tools._post_yandex_responses);
            # wrapping the whole multi-POST loop again would multiply the
            # retry budget beyond the configured attempts.
            return _send_once(with_schema)
        # Transparent connection retries: pause + re-send, up to the
        # configured number of attempts in a row.
        return retry_call(lambda: _send_once(with_schema),
                          retry_callback=retry_callback)

    try:
        return _call(True)
    except ProviderHTTPError as exc:
        if json_schema and _is_schema_rejection(exc.status_code, exc.body or ""):
            # Structured output rejected by the provider: retry without
            # the schema so the call still succeeds.
            return _call(False)
        raise


def send_request(user_message: str, assistant: Optional[dict] = None, file_context: str = "",
                 history: Optional[list] = None, lang: Optional[str] = None, usage_callback=None,
                 incomplete_callback=None, enable_injection_protection: bool = True,
                 sanitized_callback=None, sanitized_approved_paths=None, **kwargs) -> str:
    """Send a chat-completion request and return the text response.

    Args:
        enable_injection_protection: Forwarded to ``_protect_history`` to
            control the strictness of prompt-injection counter-measures.
        sanitized_callback: optional callable invoked when a tool-result
            payload is replaced by the [SANITIZED] placeholder. Receives a
            compact dict with "reason" and optional "tool" / "path" / "error".
        usage_callback: optional callable invoked with
            {"in": N, "out": M, "cache": C} where C is the number of cached
            input tokens reported by the provider (0 when not available).

    Raises:
        ServiceNotFoundError  - assistant's service is not registered.
        ApiKeyMissingError    - required credential is absent.
        AuthTypeUnknownError  - service has an unknown auth_type.
        ProviderHTTPError     - provider responded non-200.
        RequestTimeoutError   - request timed out.
        NetworkError          - DNS / connection-level failure.
    """
    # Backward compatibility: accept the legacy 'skill' argument name.
    if assistant is None and 'skill' in kwargs:
        assistant = kwargs.pop('skill')
    if assistant is None:
        assistant = {}
    cfg, services, svc_name, svc = _resolve_chat_service(assistant)
    model, temp, sys_text, max_tokens = _resolve_generation_settings(assistant, svc)
    user_content = _compose_user_content(assistant, file_context, user_message)

    auth_type = svc.get("auth_type", "bearer")
    base_url  = svc.get("base_url", "")
    # Tool-result payloads in history are sanitized and wrapped as data
    # so they cannot inject instructions into the running conversation.
    hist_msgs = _protect_history(
        history if history else [],
        enable_injection_protection=enable_injection_protection,
        sanitized_callback=sanitized_callback,
        approved_paths=sanitized_approved_paths,
    )
    from core.context_guard import apply_context_guard
    hist_msgs = apply_context_guard(hist_msgs, user_content, assistant=assistant,
                                    max_tokens=max_tokens, svc_name=svc_name,
                                    services=services)
    (tools_list, max_tool_calls, native_function_tools,
     json_schema, reasoning_effort, tool_choice) = _resolve_tool_options(
        assistant, auth_type)
    request_kwargs = dict(
        auth_type=auth_type, svc_name=svc_name, svc=svc, cfg=cfg,
        base_url=base_url, model=model, sys_text=sys_text,
        hist_msgs=hist_msgs, user_content=user_content,
        temp=temp, max_tokens=max_tokens,
        tools_list=tools_list, max_tool_calls=max_tool_calls,
        reasoning_effort=reasoning_effort, tool_choice=tool_choice,
        usage_callback=usage_callback,
        incomplete_callback=incomplete_callback,
        native_function_tools=native_function_tools,
        assistant=assistant,
        on_tool_call=kwargs.get("on_tool_call"),
    )
    return _run_with_retry(request_kwargs, native_function_tools, json_schema,
                           retry_callback=kwargs.get("retry_callback"))


# ─── per-auth request handlers (dispatched by _do_request) ──────────────────


def _svc_key(cfg: dict, svc: dict, svc_name: str, field: str = "") -> str:
    """Return the stripped credential of *svc*, or raise ApiKeyMissingError."""
    api_key = cfg.get(svc.get("config_key", ""), "")
    if isinstance(api_key, str):
        api_key = api_key.strip()
    if not api_key:
        if field:
            raise ApiKeyMissingError(svc_name, field=field)
        raise ApiKeyMissingError(svc_name)
    return api_key


def _svc_folder(cfg: dict, svc: dict, svc_name: str,
                field: str = "Folder ID") -> str:
    """Return the stripped folder id of *svc*, or raise ApiKeyMissingError."""
    folder_id = cfg.get(svc.get("config_key2", ""), "")
    if isinstance(folder_id, str):
        folder_id = folder_id.strip()
    if not folder_id:
        raise ApiKeyMissingError(svc_name, field=field)
    return folder_id


def _request_bearer(req: dict) -> str:
    """Send the OpenAI-compatible chat/completions request."""
    svc, cfg, svc_name = req["svc"], req["cfg"], req["svc_name"]
    api_key = _svc_key(cfg, svc, svc_name)
    messages = (
        [{"role": "system", "content": req["sys_text"]}]
        + req["hist_msgs"]
        + [{"role": "user", "content": req["user_content"]}]
    )
    return _bearer_request(
        req["base_url"], api_key, req["model"], messages, req["temp"],
        tools=req["tools_list"], usage_callback=req["usage_callback"],
        max_tokens=req["max_tokens"], service=svc_name,
        json_schema=req["json_schema"],
    )


def _request_deepseek(req: dict) -> str:
    """Send the DeepSeek Responses request."""
    svc, cfg, svc_name = req["svc"], req["cfg"], req["svc_name"]
    api_key = _svc_key(cfg, svc, svc_name)
    return _deepseek_responses_request(
        req["base_url"], api_key, req["model"], req["sys_text"],
        req["hist_msgs"], req["user_content"],
        max_tokens=req["max_tokens"],
        tools_list=req["tools_list"],
        max_tool_calls=req["max_tool_calls"],
        reasoning_effort=req["reasoning_effort"],
        cfg=cfg,
        svc_name=svc_name,
        usage_callback=req["usage_callback"],
        incomplete_callback=req["incomplete_callback"],
        tool_choice=req["tool_choice"],
        json_schema=req["json_schema"],
    )


def _request_yandex(req: dict) -> str:
    """Send the Yandex AI Studio Responses request, direct or through tools."""
    svc, cfg, svc_name = req["svc"], req["cfg"], req["svc_name"]
    api_key = _svc_key(cfg, svc, svc_name, field="IAM token")
    folder_id = _svc_folder(cfg, svc, svc_name)
    if req["native_function_tools"]:
        from core.assistant_tools import run_yandex_responses_tool_loop
        return run_yandex_responses_tool_loop(
            req["base_url"], api_key, folder_id, req["model"],
            req["sys_text"], req["hist_msgs"], req["user_content"],
            temperature=req["temp"],
            max_tokens=req["max_tokens"],
            tools_list=req["tools_list"],
            max_tool_calls=req["max_tool_calls"],
            reasoning_effort=req["reasoning_effort"],
            cfg=cfg,
            svc_name=svc_name,
            usage_callback=req["usage_callback"],
            tool_choice=req["tool_choice"],
            svc=svc,
            assistant=req["assistant"],
            on_tool_call=req["on_tool_call"],
            retry_callback=req["retry_callback"],
        )
    return _yandex_responses_request(
        req["base_url"], api_key, folder_id, req["model"],
        req["sys_text"], req["hist_msgs"], req["user_content"],
        temperature=req["temp"],
        max_tokens=req["max_tokens"],
        tools_list=req["tools_list"],
        max_tool_calls=req["max_tool_calls"],
        reasoning_effort=req["reasoning_effort"],
        cfg=cfg,
        svc_name=svc_name,
        usage_callback=req["usage_callback"],
        incomplete_callback=req["incomplete_callback"],
        tool_choice=req["tool_choice"],
        svc=svc,
        assistant=req["assistant"],
        json_schema=req["json_schema"],
    )


def _request_gigachat(req: dict) -> str:
    """Send the GigaChat chat/completions request over an OAuth session."""
    svc, cfg, svc_name = req["svc"], req["cfg"], req["svc_name"]
    creds = _svc_key(cfg, svc, svc_name)
    scope = cfg.get(svc.get("config_key2", ""), "GIGACHAT_API_PERS")
    if isinstance(scope, str):
        scope = scope.strip() or "GIGACHAT_API_PERS"
    else:
        scope = "GIGACHAT_API_PERS"
    token = _gigachat_token(creds, scope)
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    })
    messages = _gigachat_messages(req["sys_text"], req["hist_msgs"],
                                  req["user_content"])
    payload = {
        "model":    req["model"],
        "messages": messages,
        "temperature":     float(req["temp"]),
        "max_tokens":      int(req["max_tokens"]),
        "stream":          False,
        "update_interval": 0,
    }
    json_schema = req["json_schema"]
    giga_format = _gigachat_response_format(json_schema) if json_schema else None
    if giga_format:
        payload["response_format"] = giga_format
    r = session.post(
        req["base_url"], json=payload,
        timeout=MODEL_RESPONSE_TIMEOUT, verify=_gigachat_verify(),
    )
    if not r.ok:
        body = _extract_gigachat_error(r)
        raise ProviderHTTPError(r.status_code, body, service=svc_name)
    response_body = r.json()
    message = response_body["choices"][0]["message"]
    result_text = _prepare_response_content(message)
    if giga_format and result_text.strip():
        result_text = _unwrap_json_text(result_text)

    if req["usage_callback"]:
        usage = response_body.get("usage")
        cache = 0
        if usage and isinstance(usage, dict):
            tokens_in = usage.get("prompt_tokens") or 0
            tokens_out = usage.get("completion_tokens") or 0
            # GigaChat reports cached prompt tokens (a subset of
            # prompt_tokens) as precached_prompt_tokens.
            cache = usage.get("precached_prompt_tokens") or 0
        else:
            tokens_in = _estimate_tokens_in(messages)
            from core.files import estimate_tokens
            tokens_out = estimate_tokens(result_text)
        req["usage_callback"]({"in": int(tokens_in), "out": int(tokens_out),
                               "cache": int(cache)})

    return result_text


_AUTH_HANDLERS = {
    "bearer": _request_bearer,
    "deepseek_responses": _request_deepseek,
    "yandex_iam": _request_yandex,
    "gigachat_oauth": _request_gigachat,
}


def _do_request(auth_type: str, svc_name: str, svc: dict, cfg: dict, base_url: str, model: str,
                sys_text: str, hist_msgs: list, user_content: str, temp: float, max_tokens: int,
                tools_list: list, max_tool_calls, usage_callback, tool_choice=None, reasoning_effort=None,
                incomplete_callback=None, native_function_tools: bool = False,
                assistant: Optional[dict] = None, on_tool_call=None, json_schema=None,
                retry_callback=None) -> str:
    """Dispatch to the appropriate auth-handler. Internal helper of send_request."""
    handler = _AUTH_HANDLERS.get(auth_type)
    if handler is None:
        raise AuthTypeUnknownError(svc_name, auth_type)
    return handler({
        "auth_type": auth_type, "svc_name": svc_name, "svc": svc, "cfg": cfg,
        "base_url": base_url, "model": model, "sys_text": sys_text,
        "hist_msgs": hist_msgs, "user_content": user_content,
        "temp": temp, "max_tokens": max_tokens,
        "tools_list": tools_list, "max_tool_calls": max_tool_calls,
        "usage_callback": usage_callback,
        "tool_choice": tool_choice, "reasoning_effort": reasoning_effort,
        "incomplete_callback": incomplete_callback,
        "native_function_tools": native_function_tools,
        "assistant": assistant, "on_tool_call": on_tool_call,
        "json_schema": json_schema, "retry_callback": retry_callback,
    })


# ─── error-body helpers (core.api_http) ─────────────────────────────────────


# ─── test_connection ──────────────────────────────────────────────────────────


def _first_test_model(svc: dict, default: str) -> str:
    """Return the model id the probe should ask for, or *default*."""
    models = svc.get("models", [])
    if not models:
        return default
    first = models[0]
    return first["id"] if isinstance(first, dict) else first


def _probe_bearer(svc: dict, cfg: dict, base_url: str) -> tuple:
    """Probe an OpenAI-compatible chat/completions endpoint."""
    key = cfg.get(svc.get("config_key", ""), "")
    if isinstance(key, str):
        key = key.strip()
    if not key:
        return False, "API key not set"
    test_model = _first_test_model(svc, "gpt-3.5-turbo")
    r = requests.post(
        base_url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": test_model, "messages": [{"role": "user", "content": "Hi"}],
              "max_tokens": 5},
        timeout=30,
        verify=_VERIFY_TLS,
    )
    return (
        (True, f"OK (HTTP {r.status_code})")
        if r.status_code == 200
        else (False, f"HTTP {r.status_code}: {r.text[:200]}")
    )


def _probe_responses(svc: dict, cfg: dict, base_url: str,
                     *, suffix: str, default_model: str,
                     headers: dict) -> tuple:
    """Probe a Responses-style endpoint used by DeepSeek and Yandex."""
    test_model = _first_test_model(svc, default_model)
    r = requests.post(
        f"{base_url}{suffix}",
        headers=headers,
        json={
            "model":       test_model,
            "input":       [{"role": "user", "content": "ping"}],
            "max_output_tokens": 5,
            "stream":      False,
        },
        timeout=30,
        verify=_VERIFY_TLS,
    )
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        failure = _responses_failure_message(r.json())
    except Exception:
        failure = ""
    if failure:
        return False, failure
    return True, f"OK (HTTP {r.status_code})"


def _probe_deepseek(svc: dict, cfg: dict, base_url: str) -> tuple:
    """Probe the DeepSeek Responses endpoint."""
    key = cfg.get(svc.get("config_key", ""), "")
    if isinstance(key, str):
        key = key.strip()
    if not key:
        return False, "API key not set"
    return _probe_responses(
        svc, cfg, base_url, suffix="", default_model="deepseek-flash",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )


def _probe_yandex(svc: dict, cfg: dict, base_url: str) -> tuple:
    """Probe the Yandex AI Studio Responses endpoint."""
    key = cfg.get(svc.get("config_key", ""), "")
    if isinstance(key, str):
        key = key.strip()
    if not key:
        return False, "IAM token not set"
    folder_id = cfg.get(svc.get("config_key2", ""), "")
    if isinstance(folder_id, str):
        folder_id = folder_id.strip()
    if not folder_id:
        return False, "Folder ID not set"
    test_model = _first_test_model(svc, "yandexgpt-5.1")
    return _probe_responses_yandex(base_url, key, folder_id, test_model)


def _probe_responses_yandex(base_url: str, key: str, folder_id: str,
                            test_model: str) -> tuple:
    """Probe Yandex with the folder-scoped model URI."""
    r = requests.post(
        f"{base_url}/responses",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model":       f"gpt://{folder_id}/{test_model}",
            "input":       [{"role": "user", "content": "ping"}],
            "max_output_tokens": 5,
            "stream":      False,
        },
        timeout=30,
        verify=_VERIFY_TLS,
    )
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        failure = _responses_failure_message(r.json())
    except Exception:
        failure = ""
    if failure:
        return False, failure
    return True, f"OK (HTTP {r.status_code})"


def _probe_gigachat(svc: dict, cfg: dict, base_url: str) -> tuple:
    """Probe GigaChat: fetch a token, then check that /models answers.

    A token is the real check here: an unreachable /models still counts as a
    working configuration because the chat endpoint may be routed elsewhere.
    """
    key = cfg.get(svc.get("config_key", ""), "")
    if isinstance(key, str):
        key = key.strip()
    if not key:
        return False, "API key not set"
    scope = cfg.get(svc.get("config_key2", ""), "GIGACHAT_API_PERS")
    if isinstance(scope, str):
        scope = scope.strip() or "GIGACHAT_API_PERS"
    else:
        scope = "GIGACHAT_API_PERS"
    token = _gigachat_token(key, scope)
    if not token:
        return False, "Failed to get token"
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    })
    r2 = session.get(
        _gigachat_models_url(base_url),
        timeout=15, verify=_gigachat_verify(),
    )
    if r2.status_code == 200:
        return True, "OK - token received, /models available"
    return True, f"Token OK, but /models returned {r2.status_code}"


_PROBES = {
    "bearer": _probe_bearer,
    "deepseek_responses": _probe_deepseek,
    "yandex_iam": _probe_yandex,
    "gigachat_oauth": _probe_gigachat,
}


def test_connection(svc_name: str, cfg: dict) -> tuple:
    """Test connectivity to *svc_name*. Returns (ok: bool, message: str).

    Does NOT use the new exception contract - this is a UI helper and
    callers expect the legacy ``(bool, str)`` return.
    """
    services  = get_services()
    svc       = services.get(svc_name)
    if not svc:
        return False, f"Сервис {svc_name} не найден"
    auth_type = svc.get("auth_type", "bearer")
    base_url  = svc.get("base_url", "")
    try:
        probe = _PROBES.get(auth_type)
        if probe is not None:
            return probe(svc, cfg, base_url)
    except Exception as e:
        return False, str(e)
    return False, "Unknown auth_type"


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
