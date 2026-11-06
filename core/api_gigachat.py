"""
core.api_gigachat - the GigaChat transport.

GigaChat keeps its own OAuth token endpoint and its own request shape: the
conversation is flattened into a single ``messages`` list with a leading
system turn, and the token is fetched per request rather than stored. TLS
verification is supplied by the caller because GigaChat is signed by the
Russian Trusted Root CA and therefore needs a dedicated CA bundle.
"""
import uuid

import requests

from core.api_errors import ProviderHTTPError


def _gigachat_messages(sys_text: str, hist_msgs: list, user_content: str) -> list:
    """Build the ``messages`` payload for a GigaChat chat completion.

    GigaChat accepts AT MOST ONE system message and it MUST be the first
    element of ``messages``; any system message inside the conversation
    makes the provider reject the whole payload (HTTP 422, "system message
    must be the first message"). The agent loop, however, injects system
    blocks into the history: the economy-mode metadata is PREPENDED (a
    leading block), while the append-only context snapshots (external
    task state, thread context) sit in the middle of the conversation.

    To keep the request prefix byte-stable for provider-side caching, only
    LEADING system blocks are folded into the single leading system
    message (the assistant prompt first, then the blocks in their original
    order). A mid-history system block keeps its position and is emitted
    with the ``user`` role instead (it is harness context, never assistant
    speech); consecutive same-role messages are then merged (joined with a
    blank line) to satisfy the provider's strict role validation. Only
    ``role`` and ``content`` keys are emitted.
    """
    system_parts: list = []
    if sys_text and str(sys_text).strip():
        system_parts.append(str(sys_text))

    body: list = []
    leading = True
    for m in hist_msgs or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if content is None:
            continue
        text = str(content)
        if not text.strip():
            continue
        role = m.get("role", "user")
        if role in ("system", "developer"):
            if leading:
                system_parts.append(text)
                continue
            # Mid-history block: keep its position, but use a role the
            # provider accepts (system messages are only allowed first).
            role = "user"
        else:
            leading = False
        if role not in ("user", "assistant"):
            role = "user"
        if body and body[-1]["role"] == role:
            body[-1]["content"] = body[-1]["content"] + "\n\n" + text
        else:
            body.append({"role": role, "content": text})

    if user_content and str(user_content).strip():
        text = str(user_content)
        if body and body[-1]["role"] == "user":
            body[-1]["content"] = body[-1]["content"] + "\n\n" + text
        else:
            body.append({"role": "user", "content": text})

    messages: list = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    messages.extend(body)
    return messages


def gigachat_token(credentials: str, scope: str = "GIGACHAT_API_PERS", *, verify=None) -> str:
    """Obtain an OAuth token for GigaChat. Raises ProviderHTTPError on failure.

    *verify* is the ``verify`` value for the request (a CA bundle path or a
    bool); the caller owns that policy so it stays patchable in one place.
    """
    if verify is None:
        verify = True
    r = requests.post(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        headers={
            "Authorization": f"Basic {credentials}",
            "RqUID":         str(uuid.uuid4()),
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={"scope": scope},
        timeout=30,
        verify=verify,
    )
    if r.status_code != 200:
        body = r.text[:300]
        raise ProviderHTTPError(r.status_code, body, service="GigaChat")
    return r.json()["access_token"]


def gigachat_models_url(base_url: str) -> str:
    """Derive the GigaChat models URL from a chat-completions base URL.

    Service definitions store the chat-completions endpoint in ``base_url``
    (e.g. ``https://api.giga.chat/v1/chat/completions``); the models
    catalogue lives at the sibling ``/models`` path. Falls back to the
    current public endpoint when *base_url* is empty or unrecognized.
    """
    default = "https://api.giga.chat/v1/models"
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return default
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/models"
    if url.endswith("/models"):
        return url
    return default

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
