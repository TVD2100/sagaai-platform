"""
core.rag_chunker - text chunking for RAG indexing.

Splits text into overlapping chunks by character budget while preserving
semantic boundaries where possible:

1. Paragraphs are the primary units (blank-line separated).
2. A paragraph longer than the chunk budget is split by sentence boundaries.
3. A sentence longer than the budget is split at word boundaries.

Chunks overlap by the configured number of characters (whole units are
included so the actual overlap may be slightly larger than requested).

No streamlit imports.
"""
import re


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;…])\s+")
_WORD_SPLIT = re.compile(r"\s+")


def _list_index(items, target, count):
    """Return a list of indexes of all occurrences of *target* in *items*."""
    return [i for i, item in enumerate(items) if item == target][:count]


def _split_units(text: str, chunk_size: int) -> list:
    """Split text into atomic units no longer than *chunk_size*.

    Unit hierarchy: paragraph → sentence → word. Returns a list of strings.
    """
    paragraphs = re.split(r"\n\s*\n+", text)
    units: list = []
    for para in paragraphs:
        para = str(para or "").strip()
        if not para:
            continue
        if len(para) <= chunk_size:
            units.append(para)
            continue
        # Paragraph too long: split by sentences.
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(para) if s.strip()]
        for sent in sentences:
            if len(sent) <= chunk_size:
                units.append(sent)
                continue
            # Sentence too long: split by words.
            words = [w for w in _WORD_SPLIT.split(sent) if w]
            buf = ""
            for word in words:
                candidate = f"{buf} {word}".strip() if buf else word
                if len(candidate) <= chunk_size:
                    buf = candidate
                    continue
                if buf:
                    units.append(buf)
                if len(word) > chunk_size:
                    # Word longer than the budget: hard split.
                    for pos in range(0, len(word), max(1, chunk_size)):
                        piece = word[pos:pos + chunk_size]
                        if piece:
                            units.append(piece)
                    buf = ""
                else:
                    buf = word
            if buf:
                units.append(buf)
    return units


def chunk_text(text: str, chunk_size: int = 1500, chunk_overlap: int = 150) -> list:
    """Split *text* into overlapping chunks of at most *chunk_size* chars.

    Returns a list of strings. Guarantees:
    - every chunk is non-empty and <= chunk_size (except oversized words);
    - chunks preserve paragraph/sentence/word boundaries;
    - consecutive chunks share roughly *chunk_overlap* characters.
    """
    if not text or not str(text).strip():
        return []
    chunk_size = max(1, int(chunk_size))
    chunk_overlap = max(0, int(chunk_overlap))
    units = _split_units(str(text), chunk_size)
    if not units:
        return []
    if chunk_overlap == 0:
        # Join units into chunks without overlap.
        chunks: list = []
        buf = ""
        for unit in units:
            candidate = f"{buf}\n{unit}".strip() if buf else unit
            if len(candidate) <= chunk_size:
                buf = candidate
                continue
            if buf:
                chunks.append(buf)
            buf = unit
        if buf:
            chunks.append(buf)
        return chunks

    chunks = []
    buf = ""
    for i, unit in enumerate(units):
        candidate = f"{buf}\n{unit}".strip() if buf else unit
        if len(candidate) <= chunk_size:
            buf = candidate
            continue
        # Finish the current chunk; start the next one with an overlap tail.
        if buf:
            chunks.append(buf)
        tail = []
        tail_len = 0
        for tail_unit in reversed(units[:i]):
            if tail_len + len(tail_unit) + 1 > chunk_overlap:
                break
            tail.insert(0, tail_unit)
            tail_len += len(tail_unit) + 1
        buf = "\n".join(tail + [unit]).strip()
        while len(buf) > chunk_size and tail:
            tail.pop(0)
            buf = "\n".join(tail + [unit]).strip() if tail else unit
    if buf:
        chunks.append(buf)
    return chunks
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
