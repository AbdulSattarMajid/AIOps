"""Tests for API-key authentication (Week 5B).

Keys are built dynamically at runtime (no plain keys in files).
Verifies constant-time comparison, fail-closed behavior, header redaction,
and that auth executes before body parsing.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aiops.api import main
from aiops.security.auth import (
    get_configured_hashes,
    hash_api_key,
    redact_headers,
    verify_api_key,
)


@pytest.fixture
def raw_test_client():
    """TestClient without any pre-set authentication headers."""
    return TestClient(main.app)


def test_valid_key_accepted(raw_test_client, monkeypatch):
    key = "valid-runtime-key-" + secrets.token_urlsafe(24)
    key_hash = hash_api_key(key)
    monkeypatch.setenv("AIOPS_API_KEY_HASHES", key_hash)

    response = raw_test_client.get(
        "/api/v1/incidents",
        headers={"X-API-Key": key},
    )
    assert response.status_code == 200


def test_missing_key_returns_401_generic(raw_test_client):
    response = raw_test_client.get("/api/v1/incidents")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_empty_key_returns_401_generic(raw_test_client):
    for empty in ("", "   ", "\t"):
        response = raw_test_client.get(
            "/api/v1/incidents",
            headers={"X-API-Key": empty},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}


def test_wrong_key_returns_401_generic(raw_test_client):
    wrong = "wrong-key-" + secrets.token_urlsafe(24)
    response = raw_test_client.get(
        "/api/v1/incidents",
        headers={"X-API-Key": wrong},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_no_hashes_configured_fails_closed(raw_test_client, monkeypatch):
    """When AIOPS_API_KEY_HASHES is unset or empty, all API requests fail closed."""
    for empty_val in ("", "   ", ",,,"):
        monkeypatch.setenv("AIOPS_API_KEY_HASHES", empty_val)
        response = raw_test_client.get(
            "/api/v1/incidents",
            headers={"X-API-Key": "any-key-" + "123"},
        )
        assert response.status_code in (401, 503)
        assert "not configured" in response.json().get("detail", "").lower()


def test_wrong_key_never_appears_in_response_or_logs(raw_test_client, caplog):
    secret_value = "leaky-secret-" + secrets.token_urlsafe(20)
    with caplog.at_level(logging.DEBUG):
        response = raw_test_client.get(
            "/api/v1/incidents",
            headers={"X-API-Key": secret_value},
        )
    assert response.status_code == 401
    assert secret_value not in response.text
    assert secret_value not in caplog.text


def test_auth_runs_before_body_parsing(raw_test_client):
    """An invalid request body with no key must return 401, NOT 422."""
    response = raw_test_client.post(
        "/api/v1/telemetry/logs",
        content=b"this is not even json {[]",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


def test_compare_digest_is_used(monkeypatch):
    key = "probe-key-" + secrets.token_urlsafe(16)
    key_hash = hash_api_key(key)
    monkeypatch.setenv("AIOPS_API_KEY_HASHES", key_hash)

    called = []
    real_compare = hmac.compare_digest

    def spy_compare(a, b):
        called.append((a, b))
        return real_compare(a, b)

    with patch("hmac.compare_digest", side_effect=spy_compare):
        valid, matched, _ = verify_api_key(key)
        assert valid is True
        assert len(called) >= 1
        assert matched == key_hash


def test_health_endpoint_remains_open_without_auth(raw_test_client):
    response = raw_test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_header_redaction_helper():
    headers = {
        "Host": "localhost",
        "X-API-Key": "secret-value-" + "abc",
        "Authorization": "Bearer token-" + "xyz",
        "Accept": "application/json",
    }
    redacted = redact_headers(headers)
    assert redacted["Host"] == "localhost"
    assert redacted["Accept"] == "application/json"
    assert redacted["X-API-Key"] == "<REDACTED>"
    assert redacted["Authorization"] == "<REDACTED>"
    assert "secret-value-" not in str(redacted)
    assert "token-" not in str(redacted)



def test_docs_and_openapi_endpoints_disabled_by_default(raw_test_client):
    """In production, /docs, /redoc, and /openapi.json are disabled to avoid schema leakage."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = raw_test_client.get(path)
        assert response.status_code == 404
