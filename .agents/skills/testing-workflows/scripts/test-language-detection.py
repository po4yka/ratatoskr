#!/usr/bin/env python3
"""Test language detection for English and Russian content."""

from app.core.lang import detect_language


def main() -> None:
    texts = [
        "This is an English text about technology.",
        "Это русский текст о технологиях.",
        "Mixed текст with both languages",
    ]

    for text in texts:
        lang = detect_language(text)
        print(f"Text: {text[:50]}...")
        print(f"Detected: {lang}\n")


if __name__ == "__main__":
    main()
