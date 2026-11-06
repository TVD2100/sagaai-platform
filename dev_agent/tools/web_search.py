"""Web search through the configured provider."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.api_errors import APIError, api_error_message
from core.prompt_guard import sanitize_search_result

from .deps import ToolMixin


class WebSearchTools(ToolMixin):
    """Web search through the configured provider."""

    def web_search(self, query: str,
                   instructions: Optional[str] = None,
                   allowed_domains: Optional[List[str]] = None,
                   search_context_size: Optional[str] = None) -> Dict[str, Any]:
        """Search the internet using the configured web-search model.

        Uses the service, model, temperature and base system prompt from the
        ACTIVE ORCHESTRATOR's web-search config (set by the UI). When no
        orchestrator config is attached (e.g. outside the orchestrator page),
        falls back to the global DevAgent settings
        (search_service / search_model).

        The base system prompt is configurable per orchestrator via the
        ``web_search_prompt`` config key (Settings -> Web-search model).
        Task-specific instructions must be passed via ``instructions``;
        they are appended to the base prompt. Do NOT repeat the general
        rules already covered by the base prompt.

        Provider-side behavior by auth type:
          - Yandex (``yandex_iam``): ``tool_choice={"type": "web_search"}``
            is forced so the model actually performs the search on /responses.
          - DeepSeek (``deepseek_responses``): the built-in web_search tool is
            silently ignored on the OpenAI-compatible /responses route, so the
            search runs server-side through DeepSeek's Anthropic-compatible
            Messages endpoint (``web_search_20250305`` server tool). The fact
            of the search is verified from the response blocks and an explicit
            error is returned when the provider did not search.
        If a provider fails and returns an empty response anyway, the request
        is retried once (without ``tool_choice`` when it was present). When
        both attempts are empty, an explicit error is returned instead of an
        empty result.

        Supports optional parameters for filtering and result depth:
          - instructions: short task-specific guidance for the search agent
            (language, format, constraints). Optional.
          - allowed_domains: limit search to these domains (e.g. ["docs.python.org"]).
          - search_context_size: "low", "medium", or "high" (default: "medium").

        When the web-search checkbox is disabled, this tool returns an error.

        The response is SANITIZED before being returned: control/format
        characters are removed, HTML tags are stripped, the text is truncated
        to a safe length, and a visible [DATA_FROM_WEB_SEARCH] label is
        prepended so the model treats the content as untrusted data.

        Args:
            query: The search query to send to the model.
            instructions: Optional short task-specific instructions appended
                to the base web-search system prompt.
            allowed_domains: Optional list of domain names to restrict search to.
            search_context_size: Optional search depth ("low"/"medium"/"high").
        Returns:
            {"ok": True, "text": "..."} with the model's search-augmented
            response, or {"ok": False, "error": "..."} on failure.
        """
        if not self._web_search_enabled:
            return {
                "ok": False,
                "error": "Web search is disabled via checkbox. "
                         "Enable it in the DevAgent UI to use this tool."
            }

        # Prefer the active orchestrator's web-search config. When absent,
        # fall back to the global DevAgent config (backward compatibility).
        ws_cfg = self._web_search_config or {}
        dev_cfg = self.deps().load_devagent_config()

        search_svc_name = str(ws_cfg.get("service") or "").strip() \
            or dev_cfg.get("search_service", "").strip()
        search_mdl = str(ws_cfg.get("model") or "").strip() \
            or dev_cfg.get("search_model", "").strip()
        temperature = float(ws_cfg.get("temperature") or dev_cfg.get("search_temperature", 0.3) or 0.3)

        if not search_svc_name or not search_mdl:
            return {
                "ok": False,
                "error": "Web search is not configured. "
                         "Go to Settings → DevAgent → «Модель для веб-поиска» "
                         "and choose a service and model that support web_search."
            }

        # Base system prompt from the orchestrator config, with fallback to
        # the built-in default. Dynamic task-specific additions go through
        # the `instructions` argument.
        from core.orchestrators import (
            DEFAULT_WEB_SEARCH_PROMPT,
            get_web_search_prompt,
        )
        base_prompt = str(ws_cfg.get("prompt") or "").strip()
        if not base_prompt:
            base_prompt = get_web_search_prompt()
        if not base_prompt:
            base_prompt = DEFAULT_WEB_SEARCH_PROMPT

        if instructions and instructions.strip():
            base_prompt = f"{base_prompt}\n\n{instructions.strip()}"

        # Build the web_search tool object with optional filters
        tool_obj: Dict[str, Any] = {"type": "web_search"}

        filters: Dict[str, Any] = {}
        if allowed_domains:
            filters["allowed_domains"] = allowed_domains
        if filters:
            tool_obj["filters"] = filters

        if search_context_size:
            tool_obj["search_context_size"] = search_context_size

        assistant = {
            "id": "__web_search__",
            "name": "Web Search",
            "service": search_svc_name,
            "model": search_mdl,
            "temperature": temperature,
            "text": base_prompt,
            "tools": [tool_obj],  # now a dict, not a string
        }
        search_effort = str(ws_cfg.get("reasoning_effort") or dev_cfg.get("search_reasoning_effort", "") or "").strip()
        if search_effort:
            assistant["reasoning_effort"] = search_effort

        # Provider-side search routing:
        #   - Yandex (``yandex_iam``): ``tool_choice={"type": "web_search"}``
        #     is forced so the model actually performs the search on /responses.
        #   - DeepSeek (``deepseek_responses``): the built-in web_search tool is
        #     silently ignored on /responses, so the search runs server-side
        #     through the Anthropic-compatible Messages endpoint with the
        #     ``web_search_20250305`` tool and is verified from the response.
        _search_svc = (self.deps().get_services().get(search_svc_name) or {})
        _auth_type = str(_search_svc.get("auth_type") or "")
        if _auth_type == "deepseek_responses":
            return self._run_deepseek_web_search(
                search_svc=_search_svc,
                search_svc_name=search_svc_name,
                search_mdl=search_mdl,
                base_prompt=base_prompt,
                query=query,
                allowed_domains=allowed_domains,
                search_context_size=search_context_size,
            )
        if _auth_type == "yandex_iam":
            assistant["tool_choice"] = {"type": "web_search"}

        try:
            response = self.deps().send_request(
                user_message=query,
                assistant=assistant,
                file_context="",
                history=[],
            )
            # A provider can answer with an empty final text even when the
            # search itself ran fine. Retry once so the model produces the
            # final answer. When the first attempt used tool_choice, the
            # retry drops it.
            if not str(response or "").strip():
                fallback_assistant = dict(assistant)
                fallback_assistant.pop("tool_choice", None)
                response = self.deps().send_request(
                    user_message=query,
                    assistant=fallback_assistant,
                    file_context="",
                    history=[],
                )
        except APIError as e:
            return {"ok": False, "error": f"Web search request failed: {api_error_message(e)}"}
        except Exception as e:
            return {"ok": False, "error": f"Web search request failed: {e}"}

        if not str(response or "").strip():
            return {
                "ok": False,
                "error": (
                    "Web search returned an empty response from the provider. "
                    "Please retry or rephrase the query."
                ),
            }

        if response.startswith("Ошибка") or response.startswith("Error"):
            return {"ok": False, "error": response}

        # Sanitize untrusted web-search output before returning it to the LLM.
        safe_response = sanitize_search_result(response)
        return {"ok": True, "text": safe_response}

    def _run_deepseek_web_search(self, *, search_svc: dict,
                                 search_svc_name: str, search_mdl: str,
                                 base_prompt: str, query: str,
                                 allowed_domains: Optional[List[str]] = None,
                                 search_context_size: Optional[str] = None
                                 ) -> Dict[str, Any]:
        """Run the web search for DeepSeek through its Anthropic endpoint.

        The built-in web_search tool of DeepSeek's OpenAI-compatible
        /responses route is silently ignored by the provider (HTTP 200,
        no search blocks in the response), so the search is sent to the
        Anthropic-compatible Messages API with the ``web_search_20250305``
        server tool, which the provider executes on its side. The presence
        of the search blocks in the response is verified, otherwise an
        explicit error is returned instead of an answer from model memory.
        """
        from core.api_layer import _deepseek_anthropic_web_search

        cfg = self.deps().load_config()
        api_key = cfg.get(search_svc.get("config_key", ""), "")
        if isinstance(api_key, str):
            api_key = api_key.strip()
        if not api_key:
            return {
                "ok": False,
                "error": f"API key for service '{search_svc_name}' is not configured.",
            }

        ws_base_url = str(search_svc.get("web_search_base_url") or "").strip()
        if not ws_base_url:
            ws_base_url = (
                str(search_svc.get("base_url") or "").rstrip("/")
            )
            # base_url of the service points at /responses; the search
            # endpoint is the sibling /anthropic/v1/messages.
            ws_base_url = ws_base_url.replace("/responses", "/anthropic/v1/messages")

        strict_prompt = (
            f"{base_prompt}\n\n"
            "Strict rule: perform exactly ONE web search, then immediately "
            "write your final answer based on its results. Never perform "
            "additional searches."
        )

        try:
            result = _deepseek_anthropic_web_search(
                base_url=ws_base_url,
                api_key=api_key,
                model=search_mdl,
                system_prompt=strict_prompt,
                query=query,
                allowed_domains=allowed_domains,
                search_context_size=search_context_size,
                svc_name=search_svc_name,
            )
        except APIError as e:
            return {
                "ok": False,
                "error": f"Web search request failed: {api_error_message(e)}",
            }
        except Exception as e:
            return {"ok": False, "error": f"Web search request failed: {e}"}

        if not result.get("ok"):
            return {
                "ok": False,
                "error": str(result.get("error") or "Web search failed."),
            }

        text = str(result.get("text") or "").strip()
        if not text:
            return {
                "ok": False,
                "error": (
                    "DeepSeek executed the search but returned no answer text. "
                    "Please retry or rephrase the query."
                ),
            }
        if text.startswith("Ошибка") or text.startswith("Error"):
            return {"ok": False, "error": text}

        safe_response = sanitize_search_result(text)
        return {"ok": True, "text": safe_response}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
