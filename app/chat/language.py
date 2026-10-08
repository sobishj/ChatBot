"""Lightweight language detection for analytics (Stats → Languages).

Non-Latin scripts are identified by their Unicode block (reliable even for one word).
Latin-script text uses langdetect when long enough, otherwise the client's default.
The model itself is told to reply in the visitor's language; this is for statistics only.
"""

from __future__ import annotations

SCRIPT_RANGES: list[tuple[int, int, str]] = [
    (0x0D00, 0x0D7F, "ml"),  # Malayalam
    (0x0B80, 0x0BFF, "ta"),  # Tamil
    (0x0C80, 0x0CFF, "kn"),  # Kannada
    (0x0C00, 0x0C7F, "te"),  # Telugu
    (0x0900, 0x097F, "hi"),  # Devanagari (Hindi/Marathi/...)
    (0x0980, 0x09FF, "bn"),  # Bengali
    (0x0A80, 0x0AFF, "gu"),  # Gujarati
    (0x0A00, 0x0A7F, "pa"),  # Gurmukhi
    (0x0600, 0x06FF, "ar"),  # Arabic (also Urdu/Persian)
    (0x0590, 0x05FF, "he"),
    (0x0E00, 0x0E7F, "th"),
    (0x0400, 0x04FF, "ru"),  # Cyrillic
    (0x0370, 0x03FF, "el"),
    (0x3040, 0x30FF, "ja"),  # Hiragana/Katakana
    (0xAC00, 0xD7AF, "ko"),
    (0x4E00, 0x9FFF, "zh"),
]


def detect_language(text: str, default: str = "en") -> str:
    counts: dict[str, int] = {}
    latin = 0
    for ch in text:
        code = ord(ch)
        if ch.isalpha() and code < 0x0250:
            latin += 1
            continue
        for start, end, lang in SCRIPT_RANGES:
            if start <= code <= end:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if counts:
        best = max(counts, key=lambda k: counts[k])
        if counts[best] >= latin:
            return "ja" if best == "zh" and "ja" in counts else best
    if len(text.split()) >= 4 and latin >= 15:
        try:
            from langdetect import DetectorFactory, detect_langs

            DetectorFactory.seed = 0  # deterministic results
            guesses = detect_langs(text)
            if guesses and guesses[0].prob >= 0.8:
                return str(guesses[0].lang)
        except Exception:  # noqa: BLE001 - langdetect raises on odd input
            pass
    return default or "en"
