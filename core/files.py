# -*- coding: utf-8 -*-
"""
core.files - file extraction, token estimation, context checking.
No streamlit imports.
"""

from core.fs import decode_bytes
from core.paths import SUPPORTED_UPLOAD_TYPES, TEXT_FILE_EXTENSIONS

# Percentage of non-ASCII bytes above which we consider text "mostly non-ASCII"
# (used by the upload-decoding checks).
_NONASCII_THRESHOLD = 0.2

# Maximum number of tokens allowed for an uploaded file's extracted text.
# Files whose extracted text exceeds this limit are rejected as too large.
MAX_UPLOAD_TOKENS = 500_000


# Maximum extracted-text length for inline attachments. Files larger than this
# are saved into the dialog's files folder; only metadata + a preview are
# passed to the orchestrator (see build_attachment_metadata / build_attachments_context).
MAX_INLINE_UPLOAD_CHARS = 60_000

# Maximum size of a single dialog attachment saved into history/<tid>/files.
# The platform stores raw bytes only and never parses uploads; the orchestrator
# decides itself when and how to extract content (read_thread_file for text,
# run_code with zipfile/PIL/... for other formats).
MAX_THREAD_FILE_BYTES = 100 * 1024 * 1024


def get_file_uploader_types() -> list:
    """Return the supported file upload type list."""
    return list(SUPPORTED_UPLOAD_TYPES)


def estimate_tokens(text: str) -> int:
    """Return an estimated token count with a character-class heuristic.

    The estimate is derived from the script mix rather than a BPE vocabulary:
    roughly 1 token per 4 ASCII characters (English averages ~4 chars per
    token) and 1 token per 2 characters above ASCII (Cyrillic and CJK pack
    more information per character and split into more tokens). The figure
    drives context-budget indicators and upload limits, where a proportional
    estimate is enough; it is not a billing counter.

    Returns at least 1.
    """
    if not text:
        return 1
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    nonascii_count = len(text) - ascii_count
    return max(1, ascii_count // 4 + nonascii_count // 2)


def check_upload_tokens(text: str, max_tokens: int = MAX_UPLOAD_TOKENS):
    """Return ``(ok, tokens)`` where ``ok`` is False when the extracted text
    exceeds *max_tokens* tokens.

    Parameters
    ----------
    text : str
        Extracted file content.
    max_tokens : int
        Maximum allowed token count (default: ``MAX_UPLOAD_TOKENS``).

    Returns
    -------
    (bool, int)
        A tuple: ``(tokens <= max_tokens, token_count)``.
    """
    tokens = estimate_tokens(text)
    return tokens <= max_tokens, tokens


def should_store_uploaded_file(text: str, max_chars: int = MAX_INLINE_UPLOAD_CHARS) -> bool:
    """Return True when an uploaded file's extracted text is too large for context."""
    return len(text or "") > max_chars


def build_attachment_metadata(file_name: str, content: str, preview_chars: int = 2000) -> dict:
    """Return an attachment record for one uploaded file.

    Small files are marked ``stored=False`` and keep the full text in
    ``content`` (they will be passed inline). Large files are marked
    ``stored=True``; the full text is still retained in ``content`` so it can
    be written to the dialog's file folder at send time, but only metadata +
    preview are exposed to the orchestrator via ``build_attachments_context``.
    """
    text = content or ""
    return {
        "name": file_name,
        "content": text,
        "path": "",
        "stored": should_store_uploaded_file(text),
        "chars": len(text),
        "tokens": estimate_tokens(text),
        "preview": text[:preview_chars],
    }


def build_attachments_context(attachments: list) -> tuple:
    """Return ``(context_text, joined_names)`` for a list of attachment records.

    Inline attachments (``stored=False``) are embedded in full. Stored
    attachments (``stored=True``) contribute only metadata (saved path, size,
    tokens) plus a small preview - the orchestrator decides when to read the
    full file from disk.
    """
    parts, names = [], []
    for f in attachments or []:
        name = f.get("name", "")
        names.append(name)
        if f.get("stored"):
            preview = f.get("preview", "")
            parts.append(
                f"### {name}\n"
                f"- saved to dialog file folder: {f.get('path', '')}\n"
                f"- size: {f.get('chars', 0)} chars, {f.get('tokens', 0)} tokens\n"
                f"- preview (first {len(preview)} chars):\n"
                f"```\n{preview}\n```"
            )
        else:
            parts.append(f"### {name}\n```\n{f.get('content', '')}\n```")
    return "\n\n".join(parts), ", ".join(names)


def build_saved_files_registry(attachments: list) -> str:
    """Return a compact registry of files saved in the dialog thread folder.

    The registry is small enough to be injected into EVERY user message so
    the orchestrator always knows which files belong to the dialog - even
    when economy mode has truncated the start of the conversation.

    Each entry: ``- name (path, N chars, ~N tokens)``
    """
    if not attachments:
        return ""
    lines = ["Сохранённые файлы диалога:"]
    for f in attachments:
        name = f.get("name", "?")
        path = f.get("path", "") or "?"
        chars = f.get("chars", 0)
        tokens = f.get("tokens", 0)
        lines.append(f"- {name} ({path}, {chars} chars, ~{tokens} tokens)")
    return "\n".join(lines)


def build_thread_files_notice(files: list, header: str = "Прикреплённые файлы диалога:") -> str:
    """Return a compact notice describing files saved in the dialog's files
    folder (``history/<tid>/files``).

    ``files`` is a list of records produced by
    ``core.threads_devagent.list_thread_files``: ``{name, path, bytes, is_text,
    probe}``. The notice is injected into the user message at send time AND
    into the per-request thread-context prefix, so the orchestrator always
    knows which files belong to the dialog. The platform never parses upload
    contents here - the orchestrator decides itself when and how to extract
    content (``read_thread_file`` for text, ``run_code`` for other formats).
    """
    if not files:
        return ""
    lines = [header]
    for f in files:
        name = f.get("name", "?")
        path = str(f.get("path", "") or "") or "?"
        size = f.get("bytes", -1)
        size_part = f"{size} bytes" if size >= 0 else "size unknown"
        marker = "text" if f.get("is_text") else "binary"
        lines.append(f"- {name} ({path}, {size_part}, {marker})")
    return "\n".join(lines)



def get_model_context_window(skill: dict, services: dict) -> int:
    """Return the context window size for the model referenced by *skill*."""
    svc_name = skill.get("service", "")
    svc      = services.get(svc_name, {})
    model_id = skill.get("model", "")
    for m in svc.get("models", []):
        if isinstance(m, dict) and m.get("id") == model_id:
            return int(m.get("context_window", svc.get("context_window_default", 128000)))
    return int(svc.get("context_window_default", 128000))


def check_context(sys_text: str, user_text: str, file_text: str,
                  skill: dict, services: dict) -> dict:
    """Return a dict describing token usage vs the model's context window."""
    sys_tok  = estimate_tokens(sys_text)
    usr_tok  = estimate_tokens(user_text)
    file_tok = estimate_tokens(file_text)
    total    = sys_tok + usr_tok + file_tok
    limit    = get_model_context_window(skill, services)
    effective_limit = int(limit * 0.9)
    ok              = total <= effective_limit
    excess_tokens   = max(0, total - effective_limit)
    excess_chars    = excess_tokens * 4
    return {
        "sys_tokens":   sys_tok,
        "usr_tokens":   usr_tok,
        "file_tokens":  file_tok,
        "total_tokens": total,
        "limit":        limit,
        "ok":           ok,
        "excess_chars": excess_chars,
    }


def ensure_optional_dependencies() -> list:
    """Return list of optional package names that are not installed."""
    required = {
        "pypdf":      "pypdf",
        "pypdfium2":  "pypdfium2",
    }
    missing = []
    for module_name, package_name in required.items():
        try:
            __import__(module_name)
        except Exception:
            missing.append(package_name)
    return missing


def extract_file_content(uploaded_file) -> str:
    """Extract text content from an uploaded file object (duck-typed: .name, .read())."""
    name = uploaded_file.name.lower()
    raw  = uploaded_file.read()

    if name.endswith(TEXT_FILE_EXTENSIONS):
        return decode_bytes(raw)

    if name.endswith(".docx"):
        from core.ooxml import extract_docx_text
        try:
            text, _warnings = extract_docx_text(raw)
            return text
        except Exception as e:
            raise RuntimeError(f"docx: {e}")

    if name.endswith(".pptx"):
        from core.ooxml import extract_pptx_text
        try:
            text, _warnings = extract_pptx_text(raw)
            return text
        except Exception as e:
            raise RuntimeError(f"pptx: {e}")

    if name.endswith(".xlsx"):
        from core.ooxml import extract_xlsx_text
        try:
            text, _warnings = extract_xlsx_text(raw)
            return text
        except Exception as e:
            raise RuntimeError(f"xlsx: {e}")

    if name.endswith(".pdf"):
        from core.document_text import extract_text_from_pdf
        try:
            text, warnings = extract_text_from_pdf(raw)
            if warnings and not text.strip():
                raise RuntimeError('; '.join(warnings))
            return text
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"pdf: {e}")

    raise RuntimeError("Unsupported file format")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
