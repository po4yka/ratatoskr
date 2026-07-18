#!/usr/bin/env python3
"""Test language detection for English and Russian content."""

import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from app.core.lang import detect_language

    cases = [
        ("This is an English text about technology.", "en"),
        ("Это русский текст о технологиях.", "ru"),
    ]

    for text, expected in cases:
        lang = detect_language(text)
        assert lang == expected, (text, expected, lang)
        print(f"Text: {text[:50]}...")
        print(f"Detected: {lang}\n")


if __name__ == "__main__":
    main()
