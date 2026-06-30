#!/usr/bin/env python3
"""Test access control by checking ALLOWED_USER_IDS environment variable.

Demonstrates the same logic used by AccessController without requiring
full project dependencies.
"""

import os


def main() -> None:
    os.environ["ALLOWED_USER_IDS"] = "123456789,987654321"

    raw = os.environ.get("ALLOWED_USER_IDS", "")
    allowed = {int(uid.strip()) for uid in raw.split(",") if uid.strip()}

    # Test allowed user
    print(f"User 123456789 allowed: {123456789 in allowed}")

    # Test blocked user
    print(f"User 999999999 allowed: {999999999 in allowed}")

    print(f"\nAllowed set: {allowed}")


if __name__ == "__main__":
    main()
