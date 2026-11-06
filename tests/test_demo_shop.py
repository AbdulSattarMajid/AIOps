from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import aiops_sdk
from demo_shop.app import create_app


@pytest.fixture(autouse=True)
def clear_sdk_rate_limit():
    with aiops_sdk._rate_limit_lock:
        aiops_sdk._recent_reports.clear()


@pytest.fixture
def reports(monkeypatch):
    sent = threading.Event()
    payloads: list[dict[str, str]] = []

    def fake_send(_reporter, payload):
        payloads.append(payload)
        sent.set()

    monkeypatch.setattr(aiops_sdk._Reporter, "_send_safely", fake_send)
    return payloads, sent


@pytest.fixture
def client(monkeypatch, reports):
    monkeypatch.setenv("AIOPS_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("AIOPS_API_KEY", "fake-shop-test-key")
    monkeypatch.delenv("SHOP_PAYMENT_KEY", raising=False)
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def _assert_friendly_500(response) -> None:
    assert response.status_code == 500
    assert "We hit a snag" in response.text
    assert "Traceback" not in response.text


def _wait_for_report(reports) -> list[dict[str, str]]:
    payloads, sent = reports
    assert sent.wait(timeout=2)
    return payloads


def test_home_health_and_happy_login_cart_profile(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}

    profile = client.post("/login", data={"username": "alice"}, follow_redirects=True)
    assert profile.status_code == 200
    assert "alice@example.test" in profile.text

    added = client.post(
        "/cart/add", data={"product_id": "1", "user_id": "1"}
    )
    assert added.status_code == 200
    assert "Canvas Tote" in added.text
    cart = client.get("/cart?user_id=1")
    assert cart.status_code == 200
    assert "Canvas Tote" in cart.text
    assert client.get("/profile/1").status_code == 200


@pytest.mark.parametrize("failure", ["deleted-profile", "missing-payment", "old-cart"])
def test_each_shop_crash_is_friendly_and_reported_once(client, reports, failure):
    if failure == "deleted-profile":
        client.post("/account/delete", data={"user_id": "1"})
        response = client.get("/profile/1")
        expected_class = "AttributeError"
    elif failure == "missing-payment":
        client.post("/cart/add", data={"product_id": "1", "user_id": "1"})
        response = client.post("/checkout", data={"user_id": "1"})
        expected_class = "KeyError"
    else:
        response = client.get("/cart?user_id=2")
        expected_class = "TypeError"

    _assert_friendly_500(response)
    payloads = _wait_for_report(reports)
    assert len(payloads) == 1
    assert payloads[0]["error_class"] == expected_class
    assert payloads[0]["service_name"] == "shop-service"


def test_shop_without_aiops_settings_works_without_sending(
    monkeypatch, reports
):
    monkeypatch.delenv("AIOPS_API_URL", raising=False)
    monkeypatch.delenv("AIOPS_API_KEY", raising=False)
    with TestClient(create_app(), raise_server_exceptions=False) as no_reporting_client:
        assert no_reporting_client.get("/").status_code == 200
        assert no_reporting_client.get("/health").status_code == 200

    assert reports[0] == []
