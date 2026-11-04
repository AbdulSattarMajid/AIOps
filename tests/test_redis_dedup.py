import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest
import redis

from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.ingest.schemas import LogEvent
from aiops.storage.redis_store import RedisStore


def _event(
    trace="RuntimeError: database connection failed in worker startup while opening primary storage pool",
    service="user-service",
    error="RuntimeError",
    timestamp="2026-11-04T10:00:00Z",
):
    return LogEvent(
        timestamp=timestamp,
        service_name=service,
        error_class=error,
        stack_trace=trace,
    )


def test_same_error_twice_creates_one_incident_with_count_two():
    client = fakeredis.FakeRedis(decode_responses=True)
    engine = DedupEngine(store=RedisStore(client=client))

    first, first_is_new = engine.process(_event())
    second, second_is_new = engine.process(
        _event(timestamp="2026-11-04T10:01:00Z")
    )

    assert first_is_new is True
    assert second_is_new is False
    assert first.id == second.id
    assert second.count == 2
    assert len(engine.incidents) == 1


def test_different_services_stay_separate():
    engine = DedupEngine(store=RedisStore(
        client=fakeredis.FakeRedis(decode_responses=True)
    ))

    user_incident, user_new = engine.process(_event(service="user-service"))
    order_incident, order_new = engine.process(_event(service="order-service"))

    assert user_new and order_new
    assert user_incident.id != order_incident.id
    assert len(engine.incidents) == 2


def test_near_duplicate_matching_is_preserved():
    engine = DedupEngine(store=RedisStore(
        client=fakeredis.FakeRedis(decode_responses=True)
    ))
    first, first_new = engine.process(_event())
    second, second_new = engine.process(
        _event(trace=(
            "RuntimeError: database connection failed in worker startup "
            "while opening primary storage pools"
        ))
    )

    assert first_new is True
    assert second_new is False
    assert first.id == second.id
    assert second.count == 2
    assert len(engine.incidents) == 1


def test_300_identical_type_errors_create_three_incidents():
    client = fakeredis.FakeRedis(decode_responses=True)
    engine = DedupEngine(store=RedisStore(client=client))
    traces = [
        "ValueError: alpha alpha alpha alpha payload decode failed checksum mismatch",
        "ValueError: zebra zebra zebra zebra storage configuration backend unavailable",
        "ValueError: quartz quartz quartz quartz parser unexpected token eof",
    ]

    for index in range(300):
        engine.process(
            _event(
                trace=traces[index // 100],
                error="ValueError",
                timestamp=f"2026-11-04T10:{index // 60:02d}:{index % 60:02d}Z",
            )
        )

    assert len(engine.incidents) == 3
    assert sorted(incident.count for incident in engine.incidents.values()) == [
        100, 100, 100
    ]


def test_state_survives_a_new_engine_on_same_redis():
    client = fakeredis.FakeRedis(decode_responses=True)
    first_engine = DedupEngine(store=RedisStore(client=client))
    original, is_new = first_engine.process(_event())
    restarted_engine = DedupEngine(store=RedisStore(client=client))

    repeated, repeated_is_new = restarted_engine.process(
        _event(timestamp="2026-11-04T10:02:00Z")
    )

    assert is_new is True
    assert repeated_is_new is False
    assert repeated.id == original.id
    assert repeated.count == 2
    assert len(restarted_engine.incidents) == 1


def test_50_concurrent_identical_events_create_one_incident():
    engine = DedupEngine(store=RedisStore(
        client=fakeredis.FakeRedis(decode_responses=True)
    ))

    with ThreadPoolExecutor(max_workers=50) as workers:
        results = list(workers.map(lambda _: engine.process(_event()), range(50)))

    assert sum(is_new for _, is_new in results) == 1
    assert len({incident.id for incident, _ in results}) == 1
    incidents = engine.incidents
    assert len(incidents) == 1
    assert next(iter(incidents.values())).count == 50


def test_redacted_password_is_absent_from_all_redis_keys_and_values():
    client = fakeredis.FakeRedis(decode_responses=True)
    engine = DedupEngine(store=RedisStore(client=client))
    password = "fake-password-" + "not-a-real-secret"
    engine.process(_event(trace=f"RuntimeError: password={password}"))

    for key in client.scan_iter():
        assert password not in key
        value_type = client.type(key)
        if value_type == "string":
            assert password not in client.get(key)
        elif value_type == "list":
            assert all(password not in value for value in client.lrange(key, 0, -1))
        elif value_type == "hash":
            assert all(password not in value for value in client.hgetall(key).values())


def test_redis_unavailable_returns_api_503(monkeypatch, authed_client):
    from aiops.storage import redis_store

    main.engine = DedupEngine()

    def unavailable():
        raise redis.ConnectionError("Redis unavailable")

    monkeypatch.setattr(redis_store, "get_redis", unavailable)
    response = authed_client.post(
        "/api/v1/telemetry/logs",
        json={
            "timestamp": "2026-11-04T10:00:00Z",
            "service_name": "user-service",
            "error_class": "RuntimeError",
            "stack_trace": "RuntimeError: backend unavailable",
        },
    )

    assert response.status_code == 503
    assert "Redis storage is unavailable" in response.json()["detail"]


@pytest.mark.redis
def test_real_redis_can_store_an_incident():
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

    prefix = f"aiops:test:{uuid.uuid4().hex}:"
    store = RedisStore(client=client, prefix=prefix, ttl_seconds=60)
    try:
        incident, is_new = DedupEngine(store=store).process(_event())
        assert is_new is True
        assert incident.count == 1
    finally:
        keys = list(client.scan_iter(match=f"{prefix}*"))
        if keys:
            client.delete(*keys)
