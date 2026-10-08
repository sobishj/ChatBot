"""Split text into ~500-token chunks with ~50 tokens of overlap.

Splitting respects structure: paragraphs/lines first, then sentences, then words,
so chunks rarely cut a fact in half. Token counts come from the embedding model's
tokenizer (passed in as ``count_tokens``), so limits are exact for that model.
"""

from __future__ import annotations

import re
from collections.abc import Callable

CHUNK_TOKENS = 500
OVERLAP_TOKENS = 50

# Sentence ends for Latin scripts, Devanagari danda, CJK and Arabic punctuation.
_SENTENCE_RE = re.compile(r"(?<=[.!?।॥。！？؟])\s+")


def _split_long(unit: str, count_tokens: Callable[[str], int], max_tokens: int) -> list[str]:
    """Split a unit that alone exceeds ``max_tokens``: by sentence, then by words."""
    pieces: list[str] = []
    sentences = _SENTENCE_RE.split(unit) if len(_SENTENCE_RE.split(unit)) > 1 else [unit]
    for sentence in sentences:
        if count_tokens(sentence) <= max_tokens:
            pieces.append(sentence)
            continue
        words = sentence.split()
        current: list[str] = []
        for word in words:
            current.append(word)
            if count_tokens(" ".join(current)) > max_tokens and len(current) > 1:
                current.pop()
                pieces.append(" ".join(current))
                current = [word]
        if current:
            pieces.append(" ".join(current))
    return pieces


def split_units(text: str, count_tokens: Callable[[str], int], max_tokens: int = CHUNK_TOKENS) -> list[str]:
    """Break text into small units (lines/paragraphs), each at most ``max_tokens``."""
    units: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if count_tokens(line) > max_tokens:
            units.extend(_split_long(line, count_tokens, max_tokens))
        else:
            units.append(line)
    return units


def chunk_text(
    text: str,
    count_tokens: Callable[[str], int],
    max_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[str]:
    """Return chunks of at most ~``max_tokens`` tokens, each starting with ~``overlap_tokens`` of the previous one."""
    units = split_units(text, count_tokens, max_tokens)
    sizes = [count_tokens(u) for u in units]
    chunks: list[str] = []
    current: list[int] = []  # indexes into units
    current_tokens = 0

    for i, size in enumerate(sizes):
        if current and current_tokens + size > max_tokens:
            chunks.append("\n".join(units[j] for j in current))
            # Start the next chunk with trailing units of this one (the overlap).
            overlap: list[int] = []
            overlap_size = 0
            for j in reversed(current):
                if overlap_size + sizes[j] > overlap_tokens:
                    break
                overlap.insert(0, j)
                overlap_size += sizes[j]
            if overlap_size + size > max_tokens:
                overlap, overlap_size = [], 0
            current, current_tokens = overlap, overlap_size
        current.append(i)
        current_tokens += size
    if current:
        chunks.append("\n".join(units[j] for j in current))
    return chunks
