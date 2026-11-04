import logging
import secrets

import httpx
import pytest

from aiops.ingest.schemas import LogEvent
from demo_ui.client import AiopsClient, load_config
from demo_ui.crash_reporter import report_crash


_API_KEY = secrets.token_urlsafe(24)


def _client(handler):
    return AiopsClient(
        "http://testserver",
        _API_KEY,
        transport=httpx.MockTransport(handler),
    )


def test_client_posts_log_with_key_and_accepts_202():
    def handler(request):
        assert request.headers["X-API-Key"] == _API_KEY
        assert request.url.path == "/api/v1/telemetry/logs"
        return httpx.Response(
            202, json={"status": "accepted", "event_id": "1-0"}
        )

    result = _client(handler).send_log({"service_name": "demo"})
    assert result.ok
    assert result.status_code == 202
    assert result.data["event_id"] == "1-0"


def test_client_maps_rate_limit_and_retry_after():
    client = _client(lambda _: httpx.Response(
        429, headers={"Retry-After": "17"}, json={"detail": "no slot"}
    ))
    result = client.queue_stats()
    assert not result.ok
    assert result.message == "rate limited, retry in 17 s"


def test_client_maps_unauthorized_without_detail():
    client = _client(lambda _: httpx.Response(
        401, json={"detail": "sensitive authorization detail"}
    ))
    result = client.health()
    assert result.message == "unauthorized"
    assert "sensitive" not in result.message


def test_client_maps_backend_unavailable():
    client = _client(lambda _: httpx.Response(
        503, json={"detail": "internal redis endpoint"}
    ))
    result = client.list_incidents()
    assert result.status_code == 503
    assert result.message == "backend unavailable"


def test_client_maps_connection_error_without_leaking_details(caplog):
    def handler(request):
        raise httpx.ConnectError(_API_KEY, request=request)

    with caplog.at_level(logging.DEBUG):
        result = _client(handler).health()
    assert result.message == "backend unavailable"
    assert _API_KEY not in repr(result)
    assert _API_KEY not in caplog.text


def test_missing_key_returns_clear_result_without_request():
    client = AiopsClient(
        "http://testserver",
        "",
        transport=httpx.MockTransport(
            lambda _: pytest.fail("request made without a configured key")
        ),
    )
    result = client.health()
    assert not result.ok
    assert result.message == "key not configured"


def test_analyze_uses_long_timeout():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "verified"})

    result = _client(handler).analyze(42)
    assert result.ok
    assert requests[0].url.path == "/api/v1/incidents/42/analyze"
    assert requests[0].extensions["timeout"]["read"] >= 120


def test_api_key_never_appears_in_repr_or_returned_messages(caplog):
    client = _client(lambda _: httpx.Response(
        503, json={"detail": _API_KEY}
    ))
    result = client.list_incidents()
    with caplog.at_level(logging.DEBUG):
        rendered = repr(client)
    assert _API_KEY not in rendered
    assert _API_KEY not in repr(result)
    assert _API_KEY not in result.message
    assert _API_KEY not in caplog.text


def test_config_loads_environment_then_secrets_file(tmp_path):
    secrets = tmp_path / "secrets.toml"
    secrets.write_text(
        f'AIOPS_DEMO_API_KEY = "{_API_KEY}"\n', encoding="utf-8"
    )
    assert load_config(
        {"AIOPS_API_URL": "http://local"}, secrets
    ) == ("http://local", _API_KEY)
    assert load_config(
        {"AIOPS_DEMO_API_KEY": "from-env"}, secrets
    ) == ("http://127.0.0.1:8000", "from-env")


def test_missing_configured_key_does_not_crash(tmp_path):
    assert load_config({}, tmp_path / "missing.toml") == (
        "http://127.0.0.1:8000",
        "",
    )


def test_get_incident_uses_real_incident_list_route():
    client = _client(lambda request: httpx.Response(
        200,
        json=[
            {"id": 8, "service_name": "user-service"},
            {"id": 9, "service_name": "payment-service"},
        ],
    ))
    result = client.get_incident(9)
    assert result.ok
    assert result.data["id"] == 9


def test_report_crash_contains_real_trace_and_valid_log_event():
    try:
        raise ValueError("sample failure")
    except ValueError as error:
        class CaptureClient:
            event = None

            def send_log(self, event_dict):
                self.event = event_dict
                return event_dict

        client = CaptureClient()
        report_crash(client, "user-service", error)

    event = LogEvent.model_validate(client.event)
    assert event.service_name == "user-service"
    assert event.error_class == "ValueError"
    assert event.message == "sample failure"
    assert "Traceback (most recent call last)" in event.stack_trace
    assert "test_demo_client.py" in event.stack_trace
    assert event.timestamp.endswith("Z")


def test_report_crash_trims_payload_fields_to_schema_limits():
    class LongError(Exception):
        pass

    try:
        raise LongError("x" * 20_000)
    except LongError as error:
        class CaptureClient:
            def send_log(self, payload):
                self.payload = payload

        client = CaptureClient()
        report_crash(client, "s" * 500, error)

    event = LogEvent.model_validate(client.payload)
    assert len(event.service_name) <= 128
    assert len(event.message) <= 10_000
    assert len(event.stack_trace) <= 100_000
