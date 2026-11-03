"""Week 5B demo: API-key authentication, rate limiting, and size caps.

Offline only (uses FastAPI TestClient).
Prints only status codes and short messages, NEVER prints keys.
"""
from __future__ import annotations

import json
import os
import secrets
import sys

sys.path.insert(0, "src")

from fastapi.testclient import TestClient

from aiops.api import main
from aiops.security.auth import hash_api_key
from aiops.security.ratelimit import default_limiter


def main_demo() -> int:
    # Set up test environment dynamically
    valid_key = "demo-key-" + secrets.token_urlsafe(24)
    valid_key_hash = hash_api_key(valid_key)

    os.environ["AIOPS_API_KEY_HASHES"] = valid_key_hash
    os.environ["RATE_LIMIT_INGEST_PER_MINUTE"] = "3"
    os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
    os.environ["MAX_REQUEST_BODY_BYTES"] = "500"

    default_limiter.reset()

    client = TestClient(main.app)

    print("=" * 60)
    print("WEEK 5B DEMO: API AUTHENTICATION, RATE LIMITING & SIZE CAPS")
    print("=" * 60)

    # 1. No key -> 401
    print("\n1. Request with NO API key:")
    resp1 = client.get("/api/v1/incidents")
    print(f"Status Code: {resp1.status_code}")
    print(f"Response:    {resp1.json()}")

    # 2. Wrong key -> 401
    print("\n2. Request with WRONG API key:")
    wrong_key = "wrong-key-" + secrets.token_urlsafe(24)
    resp2 = client.get("/api/v1/incidents", headers={"X-API-Key": wrong_key})
    print(f"Status Code: {resp2.status_code}")
    print(f"Response:    {resp2.json()}")

    # 3. Valid key -> 200 Accepted
    print("\n3. Request with VALID API key:")
    resp3 = client.get("/api/v1/incidents", headers={"X-API-Key": valid_key})
    print(f"Status Code: {resp3.status_code}")
    print(f"Response:    {resp3.json()}")

    # 4. Burst that hits 429
    print("\n4. Burst of requests hitting rate limit (limit=3/min):")
    client.get("/api/v1/incidents", headers={"X-API-Key": valid_key})  # req 2
    client.get("/api/v1/incidents", headers={"X-API-Key": valid_key})  # req 3
    resp4 = client.get("/api/v1/incidents", headers={"X-API-Key": valid_key})  # req 4 (blocked)
    print(f"Status Code: {resp4.status_code}")
    print(f"Response:    {resp4.json()}")
    print(f"Retry-After: {resp4.headers.get('Retry-After')} seconds")

    # Reset limiter window for step 5 demonstration of size cap
    default_limiter.reset()

    # 5. Oversized body -> 413
    print("\n5. Oversized request body (cap=500 bytes, sending ~1500 bytes):")
    oversized_data = json.dumps({
        "timestamp": "2026-11-03T10:00:00Z",
        "service_name": "user-service",
        "error_class": "RuntimeError",
        "stack_trace": "boom",
        "message": "x" * 1200,
    }).encode("utf-8")
    resp5 = client.post(
        "/api/v1/telemetry/logs",
        content=oversized_data,
        headers={
            "X-API-Key": valid_key,
            "Content-Length": str(len(oversized_data)),
            "Content-Type": "application/json",
        },
    )
    print(f"Status Code: {resp5.status_code}")
    print(f"Response:    {resp5.json()}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_demo())
