from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import threading
import time
import uuid

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from aiops.agent.fake_llm import FakeLLM
from aiops.agent.graph import run_agent, summarize
from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.ingest.schemas import LogEvent
from aiops.limits import RedisLimits
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


_TRACE = """Traceback (most recent call last):
  File "/app/users.py", line 19, in get_user_email
    return user.email
AttributeError: 'NoneType' object has no attribute 'email'"""
_ROOT = "mock_services/sample_app"
_NO_PATCH = "NO_PATCH: storm demo requires no patch"


def _event(
    *,
    service="storm-service",
    error="AttributeError",
    trace=_TRACE,
    timestamp="2026-11-04T10:00:00Z",
):
    return LogEvent(
        timestamp=timestamp,
        service_name=service,
        error_class=error,
        stack_trace=trace,
    )


def _set_queue_mode(monkeypatch, limit=20000):
    monkeypatch.setenv("AIOPS_INGEST_MODE", "queue")
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "off")
    monkeypatch.setenv("RATE_LIMIT_INGEST_PER_MINUTE", str(limit))
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1048576")


def _post_storm(client, count, event=None):
    payload = (event or _event()).model_dump(mode="json")
    statuses = {}
    for _ in range(count):
        response = client.post("/api/v1/telemetry/logs", json=payload)
        statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
    return statuses


def _enqueue_many(client, events, stream=QUEUE_STREAM):
    pipe = client.pipeline(transaction=False)
    for event in events:
        redacted, _ = redact_log_event(event)
        pipe.xadd(
            stream,
            {"event": json.dumps(redacted.model_dump(mode="json"))},
        )
    return pipe.execute()


def _drain_workers(
    client,
    engine,
    *,
    workers=4,
    batch=100,
    stream=QUEUE_STREAM,
    group=CONSUMER_GROUP,
    dead_stream=DEAD_LETTER_STREAM,
    min_idle_ms=60000,
    auto_analyzer=None,
):
    def drain(index):
        handled = 0
        while True:
            amount = drain_once(
                client,
                batch,
                engine=engine,
                consumer_name=f"storm-worker-{index}",
                stream_name=stream,
                group_name=group,
                dead_letter_stream=dead_stream,
                min_idle_ms=min_idle_ms,
                block_ms=0,
                auto_analyzer=auto_analyzer,
            )
            if amount == 0:
                return handled
            handled += amount

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(drain, range(workers)))


def test_identical_10000_event_api_storm_is_counted_once(
    monkeypatch, authed_client
):
    _set_queue_mode(monkeypatch)
    client = redis_store.get_redis()
    engine = DedupEngine(store=RedisStore(client=client))
    monkeypatch.setattr(main, "engine", engine)

    statuses = _post_storm(authed_client, 10_000)
    assert statuses == {202: 10_000}
    assert client.xlen(QUEUE_STREAM) == 10_000
    assert _drain_workers(client, engine) == 10_000

    incidents = list(engine.incidents.values())
    assert len(incidents) == 1
    assert incidents[0].count == 10_000
    assert client.xpending(QUEUE_STREAM, CONSUMER_GROUP)["pending"] == 0
    assert client.xlen(DEAD_LETTER_STREAM) == 0
    assert client.xlen(QUEUE_STREAM) <= int(os.getenv("STREAM_MAX_LEN", "50000"))

    keys = list(client.scan_iter())
    processed = [key for key in keys if key.startswith("aiops:processed:")]
    # One expiring processed marker per acknowledged event makes crash replay idempotent.
    assert len(processed) == 10_000
    assert all(client.ttl(key) > 0 for key in processed)
    assert len(keys) <= 10_020


def test_identical_storm_runs_one_auto_analysis_and_never_delivers(
    monkeypatch, authed_client
):
    _set_queue_mode(monkeypatch)
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("MAX_TOKENS_PER_INCIDENT", "30000")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "500000")
    client = redis_store.get_redis()
    engine = DedupEngine(store=RedisStore(client=client))
    monkeypatch.setattr(main, "engine", engine)
    llm = FakeLLM({"def get_user_email": [_NO_PATCH]})
    analysis_results = []

    def analyze(incident, limits):
        state = run_agent(
            llm,
            None,
            incident.sample_trace,
            incident.service_name,
            _ROOT,
            incident_id=incident.id,
            limits=limits,
            signature=incident.signature,
        )
        result = summarize(state)
        analysis_results.append(result)
        return result

    # The full 10,000-event API storm is covered above; its measured run takes
    # about 52s on fakeredis, so exercise auto-analysis on 500 events here.
    assert _post_storm(authed_client, 500) == {202: 500}
    assert client.xlen(QUEUE_STREAM) == 500
    assert llm.calls == []
    assert _drain_workers(
        client, engine, workers=1, auto_analyzer=analyze
    ) == 500

    limits = RedisLimits(client=client)
    incidents = list(engine.incidents.values())
    incident_used, daily_used = limits.token_usage(incidents[0].id)
    analyses, skipped = limits.auto_analysis_stats()
    assert len(incidents) == 1
    assert incidents[0].count == 500
    assert len(llm.calls) == analyses == 1
    assert skipped == 0
    assert analysis_results and all(
        "delivery" not in result for result in analysis_results
    )
    assert incident_used == daily_used < 30_000
    assert client.xlen(QUEUE_STREAM) == 0


def test_mixed_storm_creates_twenty_incidents_with_exact_counts():
    client = fakeredis.FakeRedis(decode_responses=True)
    engine = DedupEngine(store=RedisStore(client=client))
    errors = [
        ("ValueError", "alpha alpha alpha parser found invalid token"),
        ("TypeError", "bravo bravo bravo expected mapping but found scalar"),
        ("KeyError", "charlie charlie charlie missing dictionary field"),
        ("RuntimeError", "delta delta delta backend connection unavailable"),
    ]
    events = []
    for service_index in range(5):
        for error, trace in errors:
            events.extend(
                _event(
                    service=f"storm-service-{service_index}",
                    error=error,
                    trace=trace,
                    timestamp=f"2026-11-04T10:{i // 60:02d}:{i % 60:02d}Z",
                )
                for i in range(250)
            )
    random.Random(6).shuffle(events)

    for index, event in enumerate(events):
        engine.process(event, event_id=f"mixed-storm-{index}")

    incidents = list(engine.incidents.values())
    assert len(incidents) == 20
    assert all(incident.count == 250 for incident in incidents)


def test_stream_overload_returns_503_and_all_accepted_events_are_counted(
    monkeypatch, authed_client
):
    _set_queue_mode(monkeypatch, limit=20)
    monkeypatch.setenv("STREAM_MAX_LEN", "5")
    client = redis_store.get_redis()
    engine = DedupEngine(store=RedisStore(client=client))
    monkeypatch.setattr(main, "engine", engine)

    responses = [
        authed_client.post(
            "/api/v1/telemetry/logs",
            json=_event().model_dump(mode="json"),
        )
        for _ in range(8)
    ]
    accepted = [response for response in responses if response.status_code == 202]
    overflow = [response for response in responses if response.status_code == 503]
    # Accepted means the API returned 202 after the event was durably appended to Redis.
    assert len(accepted) == 5
    assert len(overflow) == 3
    assert all(response.headers["Retry-After"] == "5" for response in overflow)
    assert client.xlen(QUEUE_STREAM) == 5
    assert _drain_workers(client, engine) == len(accepted)
    incident = next(iter(engine.incidents.values()))
    assert incident.count == len(accepted)
    assert client.xpending(QUEUE_STREAM, CONSUMER_GROUP)["pending"] == 0


def test_crashed_worker_batch_is_reclaimed_without_loss_or_double_count():
    client = fakeredis.FakeRedis(decode_responses=True)
    engine = DedupEngine(store=RedisStore(client=client))
    # A full 10,000-event API storm is covered by the first test; exercise the
    # crash/reclaim boundary on 500 messages to keep fake-Redis runtime bounded.
    _enqueue_many(client, [_event() for _ in range(500)])
    client.xgroup_create(QUEUE_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
    crashed_batch = client.xreadgroup(
        CONSUMER_GROUP, "crashed-storm-worker",
        {QUEUE_STREAM: ">"}, count=100,
    )[0][1]
    assert len(crashed_batch) == 100

    # Simulate a crash after some messages were committed but before the batch was acknowledged.
    for message_id, fields in crashed_batch[:50]:
        event = LogEvent.model_validate(json.loads(fields["event"]))
        engine.process(event, event_id=message_id)

    reclaimed = drain_once(
        client,
        100,
        engine=DedupEngine(store=RedisStore(client=client)),
        consumer_name="replacement-storm-worker",
        min_idle_ms=0,
        block_ms=0,
    )
    assert reclaimed == 100
    assert _drain_workers(client, engine) == 400
    incident = next(iter(engine.incidents.values()))
    assert incident.count == 500
    assert client.xpending(QUEUE_STREAM, CONSUMER_GROUP)["pending"] == 0


def test_new_engine_after_half_drain_preserves_total_count():
    client = fakeredis.FakeRedis(decode_responses=True)
    first_engine = DedupEngine(store=RedisStore(client=client))
    # Use 500 events for restart coverage; the 10,000-event API storm above
    # took about 60s on fakeredis.
    _enqueue_many(client, [_event() for _ in range(500)])
    assert drain_once(
        client, 250, engine=first_engine, block_ms=0
    ) == 250
    first_incident = next(iter(first_engine.incidents.values()))
    assert first_incident.count == 250

    restarted_engine = DedupEngine(store=RedisStore(client=client))
    assert _drain_workers(client, restarted_engine) == 250
    incidents = list(restarted_engine.incidents.values())
    assert len(incidents) == 1
    assert incidents[0].count == 500
    assert client.xpending(QUEUE_STREAM, CONSUMER_GROUP)["pending"] == 0


def test_secret_is_absent_from_keys_values_and_stream_during_storm():
    client = fakeredis.FakeRedis(decode_responses=True)
    secret = "fake-" + "credential-" + "storm-not-real"
    trace = "RuntimeError: pass" + "wd=" + secret
    event = _event(trace=trace)
    redacted, _ = redact_log_event(event)
    assert secret in trace
    assert secret not in redacted.stack_trace

    _enqueue_many(client, [event for _ in range(1_000)])
    assert client.xlen(QUEUE_STREAM) == 1_000
    assert all(secret not in key for key in client.scan_iter())
    for key in client.scan_iter():
        kind = client.type(key)
        if kind == "stream":
            entries = client.xrange(key)
            assert len(entries) == 1_000
            assert secret not in str(entries)
        elif kind == "list":
            assert all(secret not in item for item in client.lrange(key, 0, -1))
        elif kind == "hash":
            assert secret not in str(client.hgetall(key))
        elif kind == "set":
            assert secret not in str(client.smembers(key))
        elif kind == "zset":
            assert secret not in str(client.zrange(key, 0, -1))
        else:
            value = client.get(key)
            if value is not None:
                assert secret not in value


@pytest.mark.redis
def test_real_redis_two_thousand_event_storm(capsys):
    url = os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
    client = redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Real Redis at REDIS_URL is not reachable")

    prefix = f"aiops:test:storm:{uuid.uuid4().hex}:"
    stream = prefix + "events"
    dead_stream = prefix + "dead"
    group = prefix + "workers"
    engine = DedupEngine(store=RedisStore(client=client, prefix=prefix))
    event = _event()
    redacted, _ = redact_log_event(event)
    serialized = json.dumps(redacted.model_dump(mode="json"))
    try:
        pipe = client.pipeline(transaction=False)
        for _ in range(2_000):
            pipe.xadd(stream, {"event": serialized})
        pipe.execute()
        started = time.monotonic()
        handled = _drain_workers(
            client,
            engine,
            stream=stream,
            group=group,
            dead_stream=dead_stream,
        )
        elapsed = time.monotonic() - started
        incidents = list(engine.incidents.values())
        assert handled == 2_000
        assert len(incidents) == 1
        assert incidents[0].count == 2_000
        assert client.xpending(stream, group)["pending"] == 0
        with capsys.disabled():
            print(f"real Redis storm elapsed: {elapsed:.3f}s")
    finally:
        keys = list(client.scan_iter(match=f"{prefix}*"))
        if keys:
            client.delete(*keys)
