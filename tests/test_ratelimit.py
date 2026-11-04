"""Tests for Rate Limiting layer (Week 5B).

Uses injectable clocks (zero sleep calls) to test sliding windows,
separate analyze limits, IP-based failed auth lockout, key isolation,
thread safety, proxy trust security, and memory cleanup.
"""
from __future__ import annotations

import concurrent.futures
import secrets
import time

import pytest
from fastapi.testclient import TestClient

from aiops.api import main
from aiops.security.auth import hash_api_key
from aiops.security.ratelimit import RateLimiter, default_limiter


class FakeClock:
    """Deterministic, injectable clock that advances explicitly."""

    def __init__(self, start: float = 1_000_000.0):
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


@pytest.fixture
def fake_clock():
    clock = FakeClock()
    default_limiter.set_clock(clock)
    yield clock
    default_limiter.set_clock(None)


def test_rate_limiter_nth_plus_one_gets_429():
    clock = FakeClock(100.0)
    limiter = RateLimiter(clock=clock)
    limit = 3
    window = 60

    # Requests 1 to 3 allowed
    for _ in range(limit):
        allowed, retry = limiter.check_and_record("api", "test-hash", limit, window)
        assert allowed is True
        assert retry == 0

    # 4th request (limit + 1) blocked with Retry-After
    allowed, retry = limiter.check_and_record("api", "test-hash", limit, window)
    assert allowed is False
    assert retry == 60


def test_rate_limiter_window_resets_with_fake_clock():
    clock = FakeClock(100.0)
    limiter = RateLimiter(clock=clock)
    limit = 2
    window = 30

    assert limiter.check_and_record("api", "id-1", limit, window)[0] is True
    assert limiter.check_and_record("api", "id-1", limit, window)[0] is True
    assert limiter.check_and_record("api", "id-1", limit, window)[0] is False

    # Advance clock past the window
    clock.advance(31.0)
    allowed, retry = limiter.check_and_record("api", "id-1", limit, window)
    assert allowed is True
    assert retry == 0


def test_two_keys_do_not_affect_each_other(fake_clock, monkeypatch):
    """Rate limits on Key A must not impact Key B."""
    key_a = "key-alpha-" + secrets.token_urlsafe(16)
    key_b = "key-beta-" + secrets.token_urlsafe(16)
    hash_a = hash_api_key(key_a)
    hash_b = hash_api_key(key_b)
    monkeypatch.setenv("AIOPS_API_KEY_HASHES", f"{hash_a},{hash_b}")
    monkeypatch.setenv("RATE_LIMIT_INGEST_PER_MINUTE", "2")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")

    client = TestClient(main.app)

    # Exhaust Key A limit
    r1 = client.get("/api/v1/incidents", headers={"X-API-Key": key_a})
    r2 = client.get("/api/v1/incidents", headers={"X-API-Key": key_a})
    r3 = client.get("/api/v1/incidents", headers={"X-API-Key": key_a})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert "Retry-After" in r3.headers

    # Key B is completely unaffected
    rb1 = client.get("/api/v1/incidents", headers={"X-API-Key": key_b})
    assert rb1.status_code == 200


def test_analyze_limit_is_stricter_than_ingestion(fake_clock, monkeypatch):
    """Analyze endpoint has a strictly lower rate limit than ingest."""
    key = "key-tier-" + secrets.token_urlsafe(16)
    key_hash = hash_api_key(key)
    monkeypatch.setenv("AIOPS_API_KEY_HASHES", key_hash)
    # Stricter analyze (2) vs ingest (10)
    monkeypatch.setenv("RATE_LIMIT_ANALYZE_PER_MINUTE", "2")
    monkeypatch.setenv("RATE_LIMIT_INGEST_PER_MINUTE", "10")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")

    client = TestClient(main.app)

    # Analyze hits 429 on request 3
    a1 = client.post("/api/v1/incidents/1/analyze", headers={"X-API-Key": key})
    a2 = client.post("/api/v1/incidents/1/analyze", headers={"X-API-Key": key})
    a3 = client.post("/api/v1/incidents/1/analyze", headers={"X-API-Key": key})
    assert a3.status_code == 429
    assert int(a3.headers["Retry-After"]) >= 1

    # But ingest on the same key still succeeds because it has a higher limit
    ingest_resp = client.get("/api/v1/incidents", headers={"X-API-Key": key})
    assert ingest_resp.status_code == 200


def test_failed_auth_limit_by_ip_stops_guessing(fake_clock, monkeypatch):
    """Excessive failed auth attempts from an IP lock it out with 429 when behind a trusted proxy."""
    key = "real-key-" + secrets.token_urlsafe(16)
    monkeypatch.setenv("AIOPS_API_KEY_HASHES", hash_api_key(key))
    monkeypatch.setenv("RATE_LIMIT_FAILED_AUTH_PER_MINUTE", "3")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    # Mark direct peer as trusted proxy so X-Forwarded-For is evaluated
    monkeypatch.setenv("TRUSTED_PROXIES", "testclient,127.0.0.1")

    client = TestClient(main.app)
    bad_headers = {
        "X-API-Key": "bad-attempt",
        "X-Forwarded-For": "203.0.113.42",
    }

    # 3 failed attempts return 401
    for _ in range(3):
        res = client.get("/api/v1/incidents", headers=bad_headers)
        assert res.status_code == 401

    # 4th attempt from the same IP is blocked with 429
    blocked = client.get("/api/v1/incidents", headers=bad_headers)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert "too many failed" in blocked.json()["detail"].lower()

    # Another IP is NOT locked out
    other_ip_headers = {
        "X-API-Key": key,
        "X-Forwarded-For": "203.0.113.99",
    }
    other_res = client.get("/api/v1/incidents", headers=other_ip_headers)
    assert other_res.status_code == 200

    # Advance clock: lockout on original IP clears
    fake_clock.advance(61.0)
    unlocked = client.get(
        "/api/v1/incidents",
        headers={"X-API-Key": key, "X-Forwarded-For": "203.0.113.42"},
    )
    assert unlocked.status_code == 200


def test_spoofed_forwarded_for_cannot_bypass_lockout_without_trusted_proxy(fake_clock, monkeypatch):
    """Without TRUSTED_PROXIES configured, changing X-Forwarded-For does NOT bypass failed auth lockout."""
    key = "real-key-" + secrets.token_urlsafe(16)
    monkeypatch.setenv("AIOPS_API_KEY_HASHES", hash_api_key(key))
    monkeypatch.setenv("RATE_LIMIT_FAILED_AUTH_PER_MINUTE", "3")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    # Ensure no proxies are trusted
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)

    client = TestClient(main.app)

    # Attacker attempts to spoof different X-Forwarded-For IPs on every attempt
    for i in range(3):
        res = client.get(
            "/api/v1/incidents",
            headers={"X-API-Key": "bad-key", "X-Forwarded-For": f"198.51.100.{i}"},
        )
        assert res.status_code == 401

    # 4th attempt with yet another spoofed IP is STILL blocked because real peer connection is locked out
    blocked = client.get(
        "/api/v1/incidents",
        headers={"X-API-Key": "bad-key", "X-Forwarded-For": "198.51.100.99"},
    )
    assert blocked.status_code == 429


def test_rate_limiter_cleanup_bounds_memory():
    clock = FakeClock(100.0)
    limiter = RateLimiter(clock=clock)

    # Insert 1200 unique identities
    for i in range(1200):
        limiter.check_and_record("api", f"client-{i}", 10, 30)

    assert len(limiter._buckets) >= 1000

    # Advance time and trigger check_and_record, which runs cleanup
    clock.advance(35.0)
    limiter.check_and_record("api", "new-client", 10, 30)

    # Stale buckets have been pruned
    assert len(limiter._buckets) < 500


def test_rate_limiter_thread_safety():
    clock = FakeClock(100.0)
    limiter = RateLimiter(clock=clock)
    limit = 50
    window = 60

    def worker():
        results = []
        for _ in range(10):
            allowed, _ = limiter.check_and_record("api", "shared-client", limit, window)
            results.append(allowed)
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker) for _ in range(10)]
        all_results = []
        for f in concurrent.futures.as_completed(futures):
            all_results.extend(f.result())

    # Exactly 50 should be True, 50 should be False
    assert sum(all_results) == 50
    assert len(all_results) == 100
