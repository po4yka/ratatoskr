#!/usr/bin/env python3
"""Test URL normalization and deduplication hashing."""

from app.core.url_utils import compute_dedupe_hash, normalize_url


def main() -> None:
    test_urls = [
        "https://Example.com/Article?utm_source=test",
        "https://example.com/article",
        "https://example.com/article/",
    ]

    for url in test_urls:
        normalized = normalize_url(url)
        hash_val = compute_dedupe_hash(normalized)
        print(f"Original: {url}")
        print(f"Normalized: {normalized}")
        print(f"Hash: {hash_val}\n")


if __name__ == "__main__":
    main()
