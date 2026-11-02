import pytest
from fastapi.testclient import TestClient

from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.dedup.normalize import normalize_trace

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_engine():
    main.engine = DedupEngine()  # start every test with empty memory


def make_event(line=42, request_id="aaa", service="user-service", error="AttributeError"):
    return {
        "timestamp": "2026-11-02T10:00:00Z",
        "service_name": service,
        "error_class": error,
        "stack_trace": (
            f'File "/app/users.py", line {line}, in get_user_email\n'
            f"AttributeError: 'NoneType' object has no attribute 'email' id={request_id}"
        ),
    }


def test_normalize_removes_changing_parts():
    a = normalize_trace('line 42 at 2026-11-02T10:00:00Z id 0x7f3a')
    b = normalize_trace('line 97 at 2027-01-05T23:59:59Z id 0x9bb1')
    assert a == b


def test_100_identical_errors_make_one_incident():
    responses = [client.post("/api/v1/telemetry/logs", json=make_event()).json() for _ in range(100)]
    assert sum(r["is_new"] for r in responses) == 1
    assert responses[-1]["occurrence_count"] == 100
    assert len(client.get("/api/v1/incidents").json()) == 1


def test_same_error_with_different_line_and_ids_is_one_incident():
    for i in range(20):
        client.post("/api/v1/telemetry/logs", json=make_event(line=40 + i, request_id=str(i * 7919)))
    assert len(client.get("/api/v1/incidents").json()) == 1


def test_different_errors_make_different_incidents():
    client.post("/api/v1/telemetry/logs", json=make_event())
    other = {
        "timestamp": "2026-11-02T10:00:00Z",
        "service_name": "payment-service",
        "error_class": "KeyError",
        "stack_trace": "KeyError: 'STRIPE_API_KEY' in load_config",
    }
    client.post("/api/v1/telemetry/logs", json=other)
    assert len(client.get("/api/v1/incidents").json()) == 2


def test_same_error_in_different_services_stays_separate():
    client.post("/api/v1/telemetry/logs", json=make_event(service="user-service"))
    client.post("/api/v1/telemetry/logs", json=make_event(service="order-service"))
    assert len(client.get("/api/v1/incidents").json()) == 2


def test_bad_payload_is_rejected():
    response = client.post("/api/v1/telemetry/logs", json={"service_name": "x"})
    assert response.status_code == 422