#!/usr/bin/env python3
"""Generate a new random API key and its SHA-256 hash.

Prints the key ONCE and its hash.
Does NOT write to any file.
"""
from __future__ import annotations

import hashlib
import secrets


def main() -> int:
    key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()

    print("=" * 60)
    print("NEW AIOPS API KEY GENERATED")
    print("=" * 60)
    print("API Key (store this now; it will NEVER be shown again):")
    print(key)
    print("\nSHA-256 Hash:")
    print(key_hash)
    print("\nConfiguration instructions:")
    print("Put ONLY the hash in .env (never the raw key):")
    print(f"AIOPS_API_KEY_HASHES={key_hash}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

