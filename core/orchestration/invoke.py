# -*- coding: utf-8 -*-
"""
core.orchestration.invoke - the one-shot assistant call (invoke_once).

One model call, no agent loop. Two forms share this implementation: an
existing assistant addressed by assistant_id (prompt, model and temperature
come from its card) and an ephemeral call (system plus context_files,
nothing written to the database).

The call deliberately does NOT reuse the assistant-card path of
send_request. That path does four things this contract forbids:

1. load_assistant_files_context always mixes the assistant's attachment
   files into the request;
2. _assistant_rag_context searches the RAG bases bound to the assistant's
   folder manifest;
3. a non-empty tools list starts the native function-calling loop;
4. sanitize_text silently truncates the file context to 20000 characters
   (about 5k tokens), which contradicts the declared upper bound and the
   ban on silent truncation.

Passing a card with NO id, slug or tools and an empty file_context switches
all four off by construction: _assistant_rag_context returns an empty string
without a slug or id, load_assistant_files_context of an empty string
returns an empty string, an empty tools list makes _has_native_function_tools
false so the tool loop never starts, and sanitize_text runs on an empty
string. The caller's context travels in user_message, which sanitize_text
does not touch. Window overflow is caught by apply_context_guard, which
raises ContextWindowError -> error_code context_too_large. Nothing is
truncated silently at any step.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from core.orchestration.contracts import (
    ERR_CONTEXT_TOO_LARGE,
    ERR_INVALID_ARGS,
    ERR_PROVIDER_ERROR,
    ERR_UNKNOWN_ASSISTANT,
    InvokeRequest,
    error_result,
)

#: Card keys forwarded to send_request. Every other key (id, slug, tools,
#: max_tool_calls, json_schema, tool_choice) switches on a separate branch
#: of behaviour and must NOT travel.
_ALLOWED_CARD_KEYS = ('service', 'model', 'temperature', 'text', 'max_tokens')


def _read_document_bytes(path: str) -> Tuple[Optional[str], List[str]]:
    """Return the extracted text of a document file, or ``(None, warnings)``.

    A plain-text extension is left to the utf-8 reader in the caller. A
    document format (PDF, DOCX, DOC, PPTX, XLSX, XLS, RTF, ODT, HTML) is run
    through ``core.document_text`` so the caller may pass the file itself
    instead of pasting pieces of its text into the message.

    ``None`` as the first element means "not a document, read it as text".
    An unreadable or unsupported payload returns the text it could produce
    (often empty) plus the extractor warnings.
    """
    try:
        from core.document_text import TEXT_EXTENSIONS, detect_format, extract_text
    except Exception:  # pragma: no cover - defensive
        return None, []
    if detect_format(path) in TEXT_EXTENSIONS:
        return None, []
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as exc:
        return '', ['cannot read the file: ' + str(exc)]
    text, warnings = extract_text(path, data)
    return str(text or ''), list(warnings or [])


def _read_context_files(paths: List[str],
                        base_dir: Optional[str] = None) -> Tuple[str, List[str]]:
    """Read context files into the shared file-block format.

    The format matches core.assistants.load_assistant_files_context so the
    model sees the same shape it knows from assistant attachments - but the
    files are read here, not through an assistant card, which is what keeps
    the assistant's own attachments out of the request.

    Relative paths resolve against base_dir (the parent workspace) first and
    against the process cwd second, so a caller may pass paths relative to
    the project or absolute ones. Missing or unreadable files are reported in
    the returned skipped list instead of aborting the call: a partial context
    is more useful than a failed request, and the caller sees exactly what
    was left out.

    Returns (context_text, skipped_paths).
    """
    parts: List[str] = []
    skipped: List[str] = []
    for raw in (paths or []):
        path = str(raw or '').strip()
        if not path:
            continue
        resolved = path
        if not os.path.isabs(resolved):
            candidates = []
            if base_dir:
                candidates.append(os.path.join(base_dir, resolved))
            candidates.append(os.path.abspath(resolved))
            for cand in candidates:
                if os.path.isfile(cand):
                    resolved = cand
                    break
        extracted, warnings = _read_document_bytes(resolved)
        if extracted is None:
            try:
                with open(resolved, encoding='utf-8') as fh:
                    content = fh.read()
            except (OSError, UnicodeError):
                skipped.append(path)
                continue
        else:
            content = extracted
            if warnings and content.strip():
                content = content + '\n\n[extraction warnings: ' + '; '.join(warnings) + ']'
        if not content:
            if extracted is not None:
                skipped.append(path)
            continue
        name = os.path.basename(resolved)
        display_name = name[:-4] if name.endswith('.txt') else name
        parts.append('### File: ' + display_name + '\n' + content)
    return '\n\n'.join(parts), skipped


def _resolve_service_model(req: InvokeRequest,
                           card: Optional[Dict[str, Any]],
                           parent: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Resolve service and model in the documented order.

    Order: explicit request arguments, then the assistant card, then the
    parent orchestrator's main model. send_request needs both keys - it
    raises ServiceNotFoundError without service and KeyError without model -
    so an unresolved pair becomes invalid_args with an explicit note instead
    of a raw provider error.

    Returns (service, model); either may be an empty string when it could not
    be resolved (the caller turns that into invalid_args).
    """
    service = ''
    model = ''
    # 1. Explicit request arguments.
    model = str(req.model or '').strip()
    # 2. Assistant card.
    if card:
        service = str(card.get('service') or '').strip()
        model = model or str(card.get('model') or '').strip()
    # 3. Parent orchestrator main model.
    if parent:
        service = service or str(parent.get('service') or '').strip()
        model = model or str(parent.get('model') or '').strip()
    return service, model


def _parent_model(parent_slug: str) -> Dict[str, str]:
    """Return the parent orchestrator's main service/model pair.

    Best effort: an unreadable orchestrator yields an empty pair, which the
    caller reports as invalid_args rather than as a provider failure.
    """
    try:
        from core.orchestrators import build_assistant_dicts
        strong, _ = build_assistant_dicts(parent_slug)
        return {
            'service': str(strong.get('service') or ''),
            'model': str(strong.get('model') or ''),
        }
    except Exception:
        return {'service': '', 'model': ''}


def invoke_once(req: InvokeRequest,
                *,
                parent_slug: str = 'dev_agent',
                base_dir: Optional[str] = None,
                lang: Optional[str] = None,
                send_request_fn=None) -> Dict[str, Any]:
    """Run one model call for an existing assistant or an ephemeral prompt.

    Args:
        req: the request. assistant_id and system are mutually exclusive;
            passing both returns invalid_args.
        parent_slug: orchestrator whose main model serves as the fallback
            when neither the request nor the assistant card resolves
            service/model.
        base_dir: parent workspace root used to resolve relative
            context_files paths.
        lang: UI language forwarded to send_request.
        send_request_fn: test seam; defaults to core.api_layer.send_request.

    Returns the tool-shaped result: ok with a result dict holding text,
    tokens and assistant metadata on success, or the single error shape
    (ok/error/error_code) on failure.
    """
    assistant_id = (req.assistant_id or '').strip() or None
    system = req.system

    if assistant_id and system is not None:
        return error_result(
            '',
            'assistant_id and system are mutually exclusive: pass an existing '
            'assistant id OR an ephemeral system prompt, not both.',
            ERR_INVALID_ARGS,
            conflicting=['assistant_id', 'system'],
        )

    # -- Existing assistant: prompt and model come from its card ----------
    card: Optional[Dict[str, Any]] = None
    assistant_meta: Dict[str, str] = {}
    if assistant_id:
        try:
            from core.assistants import get_assistant_by_id
            card = get_assistant_by_id(assistant_id)
        except Exception:
            card = None
        if not card:
            return error_result(
                '',
                'Unknown assistant id ' + str(assistant_id) + '.',
                ERR_UNKNOWN_ASSISTANT,
                assistant=assistant_id,
            )
        assistant_meta = {
            'id': str(card.get('id') or assistant_id),
            'name': str(card.get('name') or ''),
        }

    # -- Resolve service/model before building the card -------------------
    parent = {} if (assistant_id and card) else _parent_model(parent_slug)
    service, model = _resolve_service_model(req, card, parent)
    if not service or not model:
        missing = []
        if not service:
            missing.append('service')
        if not model:
            missing.append('model')
        return error_result(
            '',
            'Cannot resolve ' + ' and '.join(missing) + ': pass it in the '
            'call, or configure it on the assistant or the orchestrator.',
            ERR_INVALID_ARGS,
            missing=missing,
        )

    # -- Build the minimal card: no id, no slug, no tools -----------------
    send_card: Dict[str, Any] = {'service': service, 'model': model}
    if card:
        if card.get('temperature') is not None:
            send_card['temperature'] = float(card['temperature'])
        send_card['text'] = str(card.get('text') or '')
        if req.max_tokens is not None:
            send_card['max_tokens'] = int(req.max_tokens)
        elif card.get('max_tokens') is not None:
            send_card['max_tokens'] = int(card['max_tokens'])
    else:
        send_card['text'] = str(system or '')
        if req.max_tokens is not None:
            send_card['max_tokens'] = int(req.max_tokens)
    # Defensive: only the documented keys reach the transport.
    send_card = {k: v for k, v in send_card.items() if k in _ALLOWED_CARD_KEYS}

    # -- Assemble the user message: prompt + data-fenced file context -----
    file_ctx, skipped = _read_context_files(req.context_files, base_dir=base_dir)
    user_message = str(req.user or '')
    if file_ctx:
        try:
            from core.prompt_guard import wrap_data
            protected = wrap_data(file_ctx, source='file_context')
        except Exception:
            protected = file_ctx
        if user_message.strip():
            user_message = (user_message + '\n\n---\n'
                            '**Контекст из файлов:**\n' + protected)
        else:
            user_message = ('Проанализируй содержимое файла согласно системному '
                            'промпту.\n\n---\n**Контекст:**\n' + protected)

    # -- Execute: one model call, no history, empty file_context ----------
    usage: Dict[str, int] = {}

    def _on_usage(payload: Dict[str, Any]) -> None:
        if isinstance(payload, dict):
            for k, v in payload.items():
                if isinstance(v, (int, float)):
                    usage[str(k)] = int(v)

    if send_request_fn is None:
        from core.api_layer import send_request as send_request_fn

    try:
        text = send_request_fn(
            user_message=user_message,
            assistant=send_card,
            file_context='',          # assistant attachments stay off
            history=[],
            lang=lang,
            usage_callback=_on_usage,
        )
    except Exception as exc:
        return _map_invoke_error(exc, assistant_id)

    result: Dict[str, Any] = {
        'text': str(text or ''),
        'tokens': dict(usage),
    }
    if assistant_meta:
        result['assistant'] = assistant_meta
    if skipped:
        result['skipped_files'] = skipped
    return {'ok': True, 'result': result}


def _map_invoke_error(exc: Exception, assistant_id: Optional[str]) -> Dict[str, Any]:
    """Map a transport failure onto the orchestration error shape.

    ContextWindowError becomes context_too_large - the call is refused, never
    truncated. Everything else is reported as provider_error with the
    exception class in the message, because the parent model can act on the
    cause but must not see a raw traceback.
    """
    details: Dict[str, Any] = {}
    if assistant_id:
        details['assistant'] = assistant_id
    try:
        from core.api_errors import ContextWindowError
    except Exception:  # pragma: no cover - defensive
        ContextWindowError = None  # type: ignore[assignment]
    if ContextWindowError is not None and isinstance(exc, ContextWindowError):
        return error_result(
            '',
            'Context too large: ' + str(exc),
            ERR_CONTEXT_TOO_LARGE,
            window=getattr(exc, 'window', 0),
            tokens=getattr(exc, 'tokens', 0),
            **details,
        )
    return error_result(
        '',
        'Provider call failed: ' + type(exc).__name__ + ': ' + str(exc),
        ERR_PROVIDER_ERROR,
        **details,
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
