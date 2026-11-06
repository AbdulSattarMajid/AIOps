from __future__ import annotations

import json
import logging
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aiops_sdk


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    with aiops_sdk._rate_limit_lock:
        aiops_sdk._recent_reports.clear()


def _wait_for(event: threading.Event) -> None:
    assert event.wait(timeout=2), "background report was not completed"


def test_route_exception_returns_500_and_sends_one_report(monkeypatch, tmp_path):
    received: list[dict[str, str]] = []
    sent = threading.Event()

    def fake_send(self, payload):
        received.append(payload)
        sent.set()

    monkeypatch.setattr(aiops_sdk._Reporter, "_send_safely", fake_send)
    app = FastAPI()

    @app.get("/fail")
    def fail():
        raise RuntimeError("route failed")

    aiops_sdk.install(
        app,
        api_url="http://127.0.0.1:8000",
        api_key="test-key",
        service_name="shop-service",
        app_root=tmp_path,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/fail")

    _wait_for(sent)
    assert response.status_code == 500
    assert len(received) == 1
    assert received[0]["service_name"] == "shop-service"
    assert received[0]["error_class"] == "RuntimeError"
    assert received[0]["message"] == "route failed"
    assert "RuntimeError: route failed" in received[0]["stack_trace"]


def test_sender_failures_and_slow_send_do_not_delay_response(
    monkeypatch, tmp_path, caplog
):
    finished = threading.Event()
    key = "do-not-expose-this-key"

    def slow_failing_send(self, payload):
        time.sleep(0.25)
        finished.set()
        raise RuntimeError(f"sender failed for {key}")

    monkeypatch.setattr(aiops_sdk._Reporter, "_send_safely", slow_failing_send)
    app = FastAPI()

    @app.get("/fail")
    def fail():
        raise ValueError("failure")

    aiops_sdk.install(
        app,
        api_url="http://127.0.0.1:8000",
        api_key=key,
        service_name="shop-service",
        app_root=tmp_path,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        started = time.monotonic()
        response = client.get("/fail")
        elapsed = time.monotonic() - started

    _wait_for(finished)
    assert response.status_code == 500
    assert elapsed < 0.2
    assert key not in caplog.text


def test_traceback_rewrites_only_paths_inside_app_root(monkeypatch, tmp_path):
    received: list[dict[str, str]] = []
    sent = threading.Event()

    def fake_send(self, payload):
        received.append(payload)
        sent.set()

    monkeypatch.setattr(aiops_sdk._Reporter, "_send_safely", fake_send)
    root = tmp_path / "application"
    root.mkdir()
    inside = root / "handlers.py"

    def fail():
        raise RuntimeError("path check")

    fail_inside_app = types.FunctionType(
        fail.__code__.replace(co_filename=str(inside)), globals()
    )
    try:
        fail_inside_app()
    except RuntimeError as exc:
        aiops_sdk.install(
            FastAPI(),
            api_url="http://127.0.0.1:8000",
            api_key="test-key",
            service_name="shop-service",
            app_root=root,
        )
        aiops_sdk.report_exception(exc)

    _wait_for(sent)
    trace = received[0]["stack_trace"]
    assert 'File "/app/handlers.py"' in trace
    assert str(Path(__file__).resolve()) in trace


def test_path_rewriting_leaves_frozen_and_relative_frames_unchanged(tmp_path):
    reporter = aiops_sdk._Reporter(
        api_url="http://127.0.0.1:8000",
        api_key="test-key",
        service_name="shop-service",
        app_root=tmp_path,
    )
    trace = (
        '  File "<frozen os>", line 1, in frozen_func\n'
        '  File "<string>", line 2, in dynamic_func\n'
        '  File "relative/module.py", line 3, in relative_func\n'
    )

    assert reporter._rewrite_app_paths(trace) == trace


@pytest.mark.parametrize("api_url", ["", "ftp://example.invalid", "//example.invalid"])
def test_install_rejects_non_http_urls_without_exposing_api_key(api_url):
    api_key = "private-test-key"

    with pytest.raises(ValueError) as error:
        aiops_sdk.install(
            FastAPI(),
            api_url=api_url,
            api_key=api_key,
            service_name="shop-service",
        )

    assert "http://" in str(error.value)
    assert api_key not in str(error.value)


def test_sends_schema_payload_to_logs_endpoint_with_api_key(monkeypatch, tmp_path):
    observed: dict[str, object] = {}
    sent = threading.Event()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["method"] = request.method
        observed["api_key"] = request.get_header("X-api-key")
        observed["timeout"] = timeout
        observed["payload"] = json.loads(request.data)
        sent.set()
        return FakeResponse()

    monkeypatch.setattr(aiops_sdk, "urlopen", fake_urlopen)
    aiops_sdk.install(
        FastAPI(),
        api_url="http://127.0.0.1:8000/",
        api_key="transport-test-key",
        service_name="shop-service",
        app_root=tmp_path,
    )

    try:
        raise RuntimeError("manual report")
    except RuntimeError as exc:
        aiops_sdk.report_exception(exc)

    _wait_for(sent)
    assert observed["url"] == "http://127.0.0.1:8000/api/v1/telemetry/logs"
    assert observed["method"] == "POST"
    assert observed["api_key"] == "transport-test-key"
    assert observed["timeout"] == 2
    assert set(observed["payload"]) == {
        "timestamp",
        "service_name",
        "error_class",
        "message",
        "stack_trace",
    }


def test_payload_excludes_request_and_environment_data(monkeypatch, tmp_path):
    received: list[dict[str, str]] = []
    sent = threading.Event()

    def fake_send(self, payload):
        received.append(payload)
        sent.set()

    monkeypatch.setattr(aiops_sdk._Reporter, "_send_safely", fake_send)
    app = FastAPI()

    @app.post("/fail")
    async def fail():
        raise RuntimeError("generic failure")

    aiops_sdk.install(
        app,
        api_url="http://127.0.0.1:8000",
        api_key="test-key",
        service_name="shop-service",
        app_root=tmp_path,
    )
    monkeypatch.setenv("SDK_TEST_SECRET", "environment-secret-value")

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/fail?query-secret=query-secret-value",
            headers={
                "X-Private-Header": "header-secret-value",
                "Cookie": "session=cookie-secret-value",
            },
            json={"body_secret": "body-secret-value"},
        )

    _wait_for(sent)
    payload_text = json.dumps(received[0])
    assert set(received[0]) == {
        "timestamp",
        "service_name",
        "error_class",
        "message",
        "stack_trace",
    }
    for private_value in (
        "environment-secret-value",
        "header-secret-value",
        "cookie-secret-value",
        "body-secret-value",
        "query-secret-value",
        "test-key",
    ):
        assert private_value not in payload_text


def test_rate_limit_drops_excess_reports(monkeypatch, tmp_path):
    received: list[dict[str, str]] = []
    done = threading.Event()
    lock = threading.Lock()

    def fake_send(self, payload):
        with lock:
            received.append(payload)
            if len(received) == 10:
                done.set()

    monkeypatch.setattr(aiops_sdk._Reporter, "_send_safely", fake_send)
    aiops_sdk.install(
        FastAPI(),
        api_url="http://127.0.0.1:8000",
        api_key="test-key",
        service_name="shop-service",
        app_root=tmp_path,
    )

    for index in range(25):
        try:
            raise RuntimeError(f"manual report {index}")
        except RuntimeError as exc:
            aiops_sdk.report_exception(exc)

    _wait_for(done)
    time.sleep(0.05)
    assert len(received) == 10


def test_api_key_never_appears_in_report_errors_or_logs(
    monkeypatch, tmp_path, caplog
):
    key = "private-api-key"

    def failing_send(self, payload):
        raise RuntimeError(f"transport error included {key}")

    monkeypatch.setattr(aiops_sdk._Reporter, "_send_safely", failing_send)
    aiops_sdk.install(
        FastAPI(),
        api_url="http://127.0.0.1:8000",
        api_key=key,
        service_name="shop-service",
        app_root=tmp_path,
    )
    caplog.set_level(logging.ERROR)

    try:
        raise RuntimeError("manual failure")
    except RuntimeError as exc:
        assert aiops_sdk.report_exception(exc) is None

    time.sleep(0.05)
    assert key not in caplog.text
