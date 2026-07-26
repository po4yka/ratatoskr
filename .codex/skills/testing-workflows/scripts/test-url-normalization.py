#!/usr/bin/env python3
"""Test URL normalization and deduplication hashing."""

import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from app.core.url_utils import compute_dedupe_hash, normalize_url

    test_urls = [
        "https://Example.com/Article?utm_source=test",
        "https://example.com/article",
        "https://example.com/article/",
    ]

    results = []
    for url in test_urls:
        normalized = normalize_url(url)
        hash_val = compute_dedupe_hash(normalized)
        results.append((normalized, hash_val))
        print(f"Original: {url}")
        print(f"Normalized: {normalized}")
        print(f"Hash: {hash_val}\n")

    assert results[0][0] == "https://example.com/Article"
    assert results[1] == results[2]


if __name__ == "__main__":
    main()
