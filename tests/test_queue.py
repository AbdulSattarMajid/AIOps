from concurrent.futures import ThreadPoolExecutor
import json
import os
import uuid

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.ingest.schemas import LogEvent
from aiops.security.auth import hash_api_key
from aiops.security.redaction import redact_log_event
from aiops.storage import redis_store
from aiops.storage.redis_store import (
    CONSUMER_GROUP,
    DEAD_LETTER_STREAM,
    QUEUE_STREAM,
    RedisStore,
    enqueue_event,
)
from aiops.worker.worker import drain_once
from tests.conftest import TEST_API_KEY


def _event(
    trace="RuntimeError: database connection failed in worker startup while opening primary storage pool",
    error="RuntimeError",
    timestamp="2026-11-04T10:00:00Z",
):
    return LogEvent(
        timestamp=timestamp,
        service_name="user-service",
        error_class=error,
        stack_trace=trace,
    )


def _enqueue_redacted(event, *, client=None, stream=QUEUE_STREAM):
    redacted, _ = redact_log_event(event)
    return enqueue_event(
        redacted.model_dump(mode="json"), client=client, stream=stream
    )


def _drain_all(client, engine, stream=QUEUE_STREAM, group=CONSUMER_GROUP):
    total = 0
    while True:
        handled = drain_once(
            client,
            100,
            engine=engine,
            stream_name=stream,
            group_name=group,
        )
        total += handled
        if handled == 0:
            return total


def test_post_is_accepted_before_worker_creates_incident(monkeypatch, authed_client):
    monkeypatch.setenv("AIOPS_INGEST_MODE", "queue")
    main.engine = DedupEngine()

    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=_event().model_dump(mode="json"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["event_id"]
    assert main.engine.incidents == {}
    assert redis_store.get_redis().xlen(QUEUE_STREAM) == 1

    handled = drain_once(redis_store.get_redis(), 10, engine=main.engine)
    assert handled == 1
    assert len(main.engine.incidents) == 1


def test_300_queued_identical_type_events_drain_to_three_incidents():
    client = fakeredis.FakeRedis(decode_responses=True)
    engine = DedupEngine(store=RedisStore(client=client))
    signatures = [
        "ValueError: alpha alpha alpha alpha payload decode failed checksum mismatch",
        "ValueError: zebra zebra zebra zebra storage configuration backend unavailable",
        "ValueError: quartz quartz quartz quartz parser unexpected token eof",
    ]
    for index in range(300):
        _enqueue_redacted(
            _event(
                trace=signatures[index // 100],
                error="ValueError",
                timestamp=f"2026-11-04T10:{index // 60:02d}:{index % 60:02d}Z",
            ),
            client=client,
        )

    assert client.xlen(QUEUE_STREAM) == 300
    assert _drain_all(client, engine) == 300
    assert len(engine.incidents) == 3
    assert sorted(incident.count for incident in engine.incidents.values()) == [
        100, 100, 100
    ]


def test_pending_message_is_reclaimed_and_processed_idempotently():
    client = fakeredis.FakeRedis(decode_responses=True)
    engine = DedupEngine(store=RedisStore(client=client))
    _enqueue_redacted(_event(), client=client)
    client.xgroup_create(QUEUE_STREAM, CONSUMER_GROUP, id="0", mkstream=True)

    messages = client.xreadgroup(
        CONSUMER_GROUP, "crashed-worker", {QUEUE_STREAM: ">"}, count=1
    )[0][1]
    message_id, fields = messages[0]
    event = LogEvent.model_validate(json.loads(fields["event"]))
    engine.process(event, event_id=message_id)

    handled = drain_once(
        client,
        1,
        engine=DedupEngine(store=RedisStore(client=client)),
        consumer_name="replacement-worker",
        min_idle_ms=0,
    )

    assert handled == 1
    incidents = engine.incidents
    assert len(incidents) == 1
    assert next(iter(incidents.values())).count == 1
    assert client.xlen(QUEUE_STREAM) == 0
    assert client.xpending(QUEUE_STREAM, CONSUMER_GROUP)["pending"] == 0


def test_poison_message_moves_to_dead_letter_after_five_attempts():
    client = fakeredis.FakeRedis(decode_responses=True)
    client.xadd(QUEUE_STREAM, {"event": "not-json"})
    engine = DedupEngine(store=RedisStore(client=client))

    for _ in range(5):
        drain_once(
            client,
            1,
            engine=engine,
            consumer_name="poison-worker",
            min_idle_ms=0,
            block_ms=0,
        )

    dead_letters = client.xrange(DEAD_LETTER_STREAM)
    assert len(dead_letters) == 1
    assert "not-json" not in str(dead_letters[0][1])
    assert "Processing failed after 5 delivery attempts." == dead_letters[0][1]["reason"]
    assert client.xlen(QUEUE_STREAM) == 0
    assert client.xpending(QUEUE_STREAM, CONSUMER_GROUP)["pending"] == 0


def test_full_stream_returns_503_with_retry_after(monkeypatch, authed_client):
    monkeypatch.setenv("AIOPS_INGEST_MODE", "queue")
    monkeypatch.setenv("STREAM_MAX_LEN", "1")
    main.engine = DedupEngine()
    payload = _event().model_dump(mode="json")

    first = authed_client.post("/api/v1/telemetry/logs", json=payload)
    second = authed_client.post("/api/v1/telemetry/logs", json=payload)

    assert first.status_code == 202
    assert second.status_code == 503
    assert second.headers["Retry-After"] == "5"
    assert redis_store.get_redis().xlen(QUEUE_STREAM) == 1


def test_redis_down_returns_503_with_retry_after(monkeypatch, authed_client):
    monkeypatch.setenv("AIOPS_INGEST_MODE", "queue")

    def unavailable():
        raise redis.ConnectionError("unavailable")

    monkeypatch.setattr(redis_store, "get_redis", unavailable)
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=_event().model_dump(mode="json"),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"


def test_redacted_password_is_not_in_stream_entry(monkeypatch, authed_client):
    monkeypatch.setenv("AIOPS_INGEST_MODE", "queue")
    password = "fake-password-" + "not-a-real-secret"
    event = _event(trace=f"RuntimeError: password={password}")

    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=event.model_dump(mode="json"),
    )

    assert response.status_code == 202
    entries = redis_store.get_redis().xrange(QUEUE_STREAM)
    assert len(entries) == 1
    assert password not in str(entries[0][1])
    assert "<REDACTED_SECRET>" in entries[0][1]["event"]


def test_unauthenticated_ingest_is_rejected_before_enqueue(monkeypatch):
    monkeypatch.setenv("AIOPS_INGEST_MODE", "queue")
    client = TestClient(main.app)
    response = client.post(
        "/api/v1/telemetry/logs",
        json=_event().model_dump(mode="json"),
    )

    assert response.status_code == 401
    assert redis_store.get_redis().xlen(QUEUE_STREAM) == 0


def test_queue_stats_endpoint_is_authenticated_and_reports_counts(
    monkeypatch, authed_client
):
    monkeypatch.setenv("AIOPS_INGEST_MODE", "queue")
    response = authed_client.get("/api/v1/queue/stats")

    assert response.status_code == 200
    assert response.json() == {
        "stream_length": 0,
        "pending_count": 0,
        "dead_letter_count": 0,
    }


def test_lock_acquire_timeout_returns_503_not_500(monkeypatch, authed_client):
    monkeypatch.setenv("AIOPS_INGEST_MODE", "inline")
    monkeypatch.setattr(redis_store, "DEDUP_LOCK_WAIT_SECONDS", 0.01)
    client = redis_store.get_redis()
    engine = DedupEngine(store=RedisStore(client=client))
    client.set(engine.store._lock_key("user-service"), "another-worker", ex=10)
    main.engine = engine

    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json=_event().model_dump(mode="json"),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "lock could not be acquired" in response.json()["detail"]


@pytest.mark.redis
def test_real_redis_concurrent_queue_and_four_workers(monkeypatch):
    url = os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
    client = redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Real Redis at REDIS_URL is not reachable")

    prefix = f"aiops:test:queue:{uuid.uuid4().hex}:"
    monkeypatch.setenv("STREAM_MAX_LEN", "50000")
    stream = prefix + "events"
    dead_stream = prefix + "dead"
    group = prefix + "workers"
    store = RedisStore(client=client, prefix=prefix, ttl_seconds=60)
    engine = DedupEngine(store=store)
    redacted, _ = redact_log_event(_event())
    payload = redacted.model_dump(mode="json")

    try:
        with ThreadPoolExecutor(max_workers=20) as producers:
            list(producers.map(
                lambda _: enqueue_event(payload, client=client, stream=stream),
                range(200),
            ))
        assert client.xlen(stream) == 200

        def drain_thread(index):
            handled = 0
            while True:
                count = drain_once(
                    client,
                    5,
                    engine=engine,
                    consumer_name=f"worker-{index}",
                    stream_name=stream,
                    group_name=group,
                    dead_letter_stream=dead_stream,
                    min_idle_ms=60_000,
                    block_ms=0,
                )
                if count == 0:
                    return handled
                handled += count

        with ThreadPoolExecutor(max_workers=4) as workers:
            handled = sum(workers.map(drain_thread, range(4)))

        incidents = engine.incidents
        assert handled == 200
        assert len(incidents) == 1
        assert next(iter(incidents.values())).count == 200
    finally:
        keys = list(client.scan_iter(match=f"{prefix}*"))
        if keys:
            client.delete(*keys)
