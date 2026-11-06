"""Shared pytest fixtures and test configuration for AIOps."""
from __future__ import annotations

import hashlib
import os
import secrets
from typing import Generator

import fakeredis
import pytest
from fastapi.testclient import TestClient

from aiops.api import main
from aiops.security.auth import hash_api_key
from aiops.security.ratelimit import default_limiter
from aiops.storage import redis_store

# Generate a consistent test API key and hash for test sessions
TEST_API_KEY = "test-aiops-key-" + "valid-for-tests-secret"
TEST_API_KEY_HASH = hash_api_key(TEST_API_KEY)


@pytest.fixture(scope="session", autouse=True)
def configure_test_auth():
    """Ensure tests run with a valid configured API key hash in environment."""
    old_hashes = os.environ.get("AIOPS_API_KEY_HASHES")
    os.environ["AIOPS_API_KEY_HASHES"] = TEST_API_KEY_HASH
    yield
    if old_hashes is None:
        os.environ.pop("AIOPS_API_KEY_HASHES", None)
    else:
        os.environ["AIOPS_API_KEY_HASHES"] = old_hashes


@pytest.fixture(autouse=True)
def reset_rate_limiter_and_auth():
    """Reset rate limiter state and restore test API key before each test."""
    default_limiter.reset()
    default_limiter.set_clock(None)  # restore default time.time
    os.environ["AIOPS_API_KEY_HASHES"] = TEST_API_KEY_HASH
    yield
    default_limiter.reset()


@pytest.fixture(autouse=True)
def clear_developer_settings(monkeypatch):
    """Keep developer .env credentials and live integrations out of tests."""
    for name in (
        "SLACK_WEBHOOK_URL",
        "GITHUB_TOKEN",
        "GITHUB_REPO",
        "GROQ_API_KEY",
        "AIOPS_AUTO_ANALYZE",
        "AIOPS_AUTO_DELIVER_PR",
        "AUTO_DELIVER_PR",
        "AIOPS_LLM",
        "TARGET_REPO_PATH_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def reset_redis(monkeypatch, clear_developer_settings):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_store, "get_redis", lambda: client)
    monkeypatch.setenv("AIOPS_INGEST_MODE", "inline")
    yield
    client.flushdb()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Return headers with the valid test API key."""
    return {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def authed_client() -> Generator[TestClient, None, None]:
    """TestClient preconfigured with valid X-API-Key header."""
    with TestClient(main.app, headers={"X-API-Key": TEST_API_KEY}) as client:
        yield client
