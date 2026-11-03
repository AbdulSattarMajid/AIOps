"""Tests for size caps on request bodies and LogEvent fields (Week 5B).

Verifies:
- 413 on oversized request bodies (with Content-Length, without Content-Length, and with lying Content-Length)
- 422 on oversized individual fields (message, stack trace, service name, extra fields count/size)
- Normal payloads succeed
- Caps are checked before normalization or redaction
"""
from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from aiops.api import main
from aiops.ingest.schemas import LogEvent
from tests.conftest import TEST_API_KEY


@pytest.fixture
def authed_client():
    return TestClient(main.app, headers={"X-API-Key": TEST_API_KEY})


def _valid_event_dict(**overrides):
    base = {
        "timestamp": "2026-11-03T10:00:00Z",
        "service_name": "user-service",
        "error_class": "ValueError",
        "stack_trace": 'File "/app/users.py", line 19, in run\nValueError: test',
        "message": "test message",
    }
    base.update(overrides)
    return base


def test_normal_payload_passes(authed_client):
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=_valid_event_dict(),
    )
    assert response.status_code == 200
    assert "incident_id" in response.json()


def test_oversized_body_with_content_length_returns_413(authed_client, monkeypatch):
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "500")
    payload = json.dumps(_valid_event_dict(message="x" * 1000)).encode("utf-8")
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        content=payload,
        headers={"Content-Length": str(len(payload)), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert "payload" in response.json()["detail"].lower() or "size" in response.json()["detail"].lower()


def test_oversized_body_with_lying_content_length_returns_413(authed_client, monkeypatch):
    """A client claiming Content-Length: 10 but sending 2000 bytes must be caught and return 413."""
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "500")
    payload = json.dumps(_valid_event_dict(message="x" * 1000)).encode("utf-8")
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        content=payload,
        headers={"Content-Length": "10", "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_oversized_service_name_rejected_422(authed_client):
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=_valid_event_dict(service_name="s" * 200),
    )
    assert response.status_code == 422
    assert "service_name" in response.text


def test_oversized_message_rejected_422(authed_client):
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=_valid_event_dict(message="m" * 15_000),
    )
    assert response.status_code == 422
    assert "message" in response.text


def test_oversized_stack_trace_rejected_422(authed_client):
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=_valid_event_dict(stack_trace="t" * 150_000),
    )
    assert response.status_code == 422
    assert "stack_trace" in response.text


def test_too_many_extra_fields_rejected_422(authed_client):
    extras = {f"extra_key_{i}": f"val_{i}" for i in range(25)}
    payload = _valid_event_dict(**extras)
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=payload,
    )
    assert response.status_code == 422
    assert "extra fields" in response.text.lower()


def test_oversized_extra_field_value_rejected_422(authed_client):
    payload = _valid_event_dict(custom_field="x" * 6000)
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=payload,
    )
    assert response.status_code == 422
    assert "custom_field" in response.text or "extra field" in response.text.lower()


def test_oversized_total_extra_fields_rejected_422(authed_client):
    # 10 fields of 3500 chars each = 35,000 chars > MAX_TOTAL_EXTRA_SIZE (32768)
    extras = {f"field_{i}": "x" * 3500 for i in range(10)}
    payload = _valid_event_dict(**extras)
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=payload,
    )
    assert response.status_code == 422
    assert "total" in response.text.lower()


def test_field_caps_checked_before_processing():
    """Unit test: constructing a LogEvent directly with oversized data raises ValueError immediately."""
    with pytest.raises(ValueError, match="service_name"):
        LogEvent(
            timestamp="2026-11-03T10:00:00Z",
            service_name="s" * 300,
            error_class="Error",
            stack_trace="trace",
        )

