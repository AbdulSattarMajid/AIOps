from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import fakeredis
import pytest
import redis

from aiops.agent.fake_llm import FakeLLM
from aiops.agent.graph import run_agent, summarize
from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.ingest.schemas import LogEvent
from aiops.limits import LimitStoreError, RedisLimits
from aiops.security.redaction import redact_log_event
from aiops.storage import redis_store
from aiops.storage.redis_store import QUEUE_STREAM, RedisStore, enqueue_event
from aiops.worker import worker
from aiops.worker.worker import drain_once
from tests.conftest import TEST_API_KEY


ROOT = "mock_services/sample_app"
TRACE = """Traceback (most recent call last):
  File "/app/users.py", line 19, in get_user_email
    return user.email
AttributeError: 'NoneType' object has no attribute 'email'"""
NO_PATCH = "NO_PATCH: this case requires human review"


def _event(trace=TRACE, timestamp="2026-11-04T10:00:00Z"):
    return LogEvent(
        timestamp=timestamp,
        service_name="user-service",
        error_class="AttributeError",
        stack_trace=trace,
    )


def _queue_event(client, event=None):
    log = event or _event()
    redacted, _ = redact_log_event(log)
    return enqueue_event(
        redacted.model_dump(mode="json"), client=client, stream=QUEUE_STREAM
    )


def test_cooldown_blocks_repeat_then_allows_after_clock_and_dedup_count_rises():
    client = fakeredis.FakeRedis(decode_responses=True)
    now = [1_800_000_000.0]
    limits = RedisLimits(client=client, clock=lambda: now[0])
    engine = DedupEngine(store=RedisStore(client=client))

    first, _ = engine.process(_event())
    second, _ = engine.process(_event(timestamp="2026-11-04T10:01:00Z"))

    assert first.id == second.id
    assert second.count == 2
    assert limits.claim_cooldown(first.signature, seconds=900) is True
    assert limits.claim_cooldown(first.signature, seconds=900) is False
    now[0] += 901
    assert limits.claim_cooldown(first.signature, seconds=900) is True


def test_slots_enforce_cap_release_ownership_and_expiry():
    client = fakeredis.FakeRedis(decode_responses=True)
    now = [1_800_000_000.0]
    limits = RedisLimits(client=client, clock=lambda: now[0])

    first = limits.acquire_slot("analyses", cap=2, lease_seconds=10)
    second = limits.acquire_slot("analyses", cap=2, lease_seconds=10)
    assert first and second
    assert limits.acquire_slot("analyses", cap=2, lease_seconds=10) is None

    limits.release_slot("analyses", "not-the-owner")
    assert limits.acquire_slot("analyses", cap=2, lease_seconds=10) is None
    limits.release_slot("analyses", first)
    assert limits.acquire_slot("analyses", cap=2, lease_seconds=10)

    now[0] += 11
    assert limits.acquire_slot("analyses", cap=2, lease_seconds=10)


def test_twenty_threads_never_hold_more_than_two_slots():
    client = fakeredis.FakeRedis(decode_responses=True)
    limits = RedisLimits(client=client)
    held = 0
    maximum = 0
    completed = 0
    lock = threading.Lock()

    def contend(_):
        nonlocal held, maximum, completed
        token = None
        deadline = time.monotonic() + 5
        while token is None and time.monotonic() < deadline:
            token = limits.acquire_slot("analyses", cap=2, lease_seconds=30)
            if token is None:
                time.sleep(0.001)
        if token is None:
            return
        with lock:
            held += 1
            maximum = max(maximum, held)
        time.sleep(0.005)
        with lock:
            held -= 1
            completed += 1
        limits.release_slot("analyses", token)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(contend, range(20)))
    assert maximum <= 2
    assert held == 0
    assert completed == 20


def test_incident_budget_stops_before_a_later_llm_call(monkeypatch):
    monkeypatch.setenv("MAX_TOKENS_PER_INCIDENT", "1")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "100")
    llm = FakeLLM({"def get_user_email": ["invalid response"]})
    limits = RedisLimits(
        client=fakeredis.FakeRedis(decode_responses=True)
    )

    state = run_agent(
        llm,
        None,
        TRACE,
        "user-service",
        ROOT,
        incident_id=51,
        max_retries=3,
        limits=limits,
        signature="signature-51",
    )

    assert len(llm.calls) == 1
    assert state["status"] == "escalated"
    assert state["reason"] == "incident token budget reached"


def test_daily_budget_is_shared_and_resets_at_utc_date(monkeypatch):
    monkeypatch.setenv("MAX_TOKENS_PER_INCIDENT", "100")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "5")
    start = [
        datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc).timestamp()
    ]
    client = fakeredis.FakeRedis(decode_responses=True)
    limits = RedisLimits(client=client, clock=lambda: start[0])

    limits.record_tokens(1, 5)
    assert limits.check_token_budget(2) == "daily token budget reached"
    start[0] += 120
    assert limits.check_token_budget(2) is None


def test_auto_analyze_off_and_on_with_cooldown_and_no_delivery(monkeypatch):
    client = redis_store.get_redis()
    engine = DedupEngine(store=RedisStore(client=client))
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "off")
    for _ in range(2):
        _queue_event(client)
    called = []
    assert drain_once(
        client, 2, engine=engine, auto_analyzer=lambda *_: called.append(1)
    ) == 2
    assert called == []

    client.flushdb()
    engine = DedupEngine(store=RedisStore(client=client))
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    llm = FakeLLM({"def get_user_email": [NO_PATCH]})
    results = []

    def analyze(incident, limits):
        state = run_agent(
            llm,
            None,
            incident.sample_trace,
            incident.service_name,
            ROOT,
            incident_id=incident.id,
            limits=limits,
            signature=incident.signature,
        )
        result = summarize(state)
        results.append(result)
        return result

    for _ in range(100):
        _queue_event(client)
    assert drain_once(
        client, 100, engine=engine, auto_analyzer=analyze, block_ms=0
    ) == 100
    limits = RedisLimits(client=client)
    assert len(llm.calls) == 1
    assert len(results) == 1
    assert "delivery" not in results[0]
    assert limits.auto_analysis_stats() == (1, 0)
    incident = next(iter(engine.incidents.values()))
    assert incident.count == 100


def test_auto_analyze_fails_closed_when_limits_redis_is_unavailable(
    monkeypatch,
):
    client = redis_store.get_redis()
    engine = DedupEngine(store=RedisStore(client=client))
    _queue_event(client)
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    called = []

    class UnavailableLimits:
        def __init__(self, client=None):
            pass

        def acquire_slot(self, kind):
            raise LimitStoreError("unavailable")

    monkeypatch.setattr(worker, "RedisLimits", UnavailableLimits)
    assert drain_once(
        client, 1, engine=engine, auto_analyzer=lambda *_: called.append(1)
    ) == 1
    assert called == []
    assert len(engine.incidents) == 1


def test_auto_analysis_failure_is_recorded_without_secret_or_raw_trace(
    monkeypatch, caplog
):
    client = redis_store.get_redis()
    engine = DedupEngine(store=RedisStore(client=client))
    secret = "fake-secret-" + "do-not-store"
    trace = TRACE + "\npassword=" + secret
    _queue_event(client, _event(trace=trace))
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")

    def fail(_incident, _limits):
        raise RuntimeError(secret + trace)

    assert drain_once(client, 1, engine=engine, auto_analyzer=fail) == 1
    incident = next(iter(engine.incidents.values()))
    result = engine.store.get_analysis_result(incident.id)
    assert result == {
        "status": "escalated",
        "reason": "Automatic analysis failed; inspect service health.",
    }
    assert secret not in str(result)
    assert TRACE not in str(result)
    assert secret not in caplog.text
    assert TRACE not in caplog.text


def test_manual_analysis_without_slot_returns_429(monkeypatch, authed_client):
    client = redis_store.get_redis()
    monkeypatch.setenv("MAX_CONCURRENT_ANALYSES", "1")
    main.engine = DedupEngine(store=RedisStore(client=client))
    incident, _ = main.engine.process(_event())
    limits = RedisLimits(client=client)
    held = limits.acquire_slot("analyses")
    assert held
    main.app.dependency_overrides[main.get_llm] = lambda: FakeLLM({})
    main.app.dependency_overrides[main.get_store] = lambda: None
    main.app.dependency_overrides[main.get_sandbox] = lambda: None
    try:
        response = authed_client.post(
            f"/api/v1/incidents/{incident.id}/analyze"
        )
    finally:
        limits.release_slot("analyses", held)
        main.app.dependency_overrides.clear()
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"


def test_sandbox_slot_exhaustion_keeps_patch_untested(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("MAX_CONCURRENT_SANDBOXES", "1")
    limits = RedisLimits(client=client)
    occupied = limits.acquire_slot("sandboxes")
    assert occupied

    class Sandbox:
        baseline_calls = 0

        def baseline(self):
            self.baseline_calls += 1
            raise AssertionError("sandbox should not run without a slot")

        def verify(self, _path, _source):
            raise AssertionError("sandbox should not run without a slot")

    sandbox = Sandbox()
    llm = FakeLLM({
        "def get_user_email": [
            "--- a/users.py\n+++ b/users.py\n@@ -17,3 +17,3 @@\n"
            " def get_user_email(user_id):\n"
            "     user = find_user(user_id)\n"
            "-    return user.email\n"
            "+    return user.email if user is not None else None\n"
        ]
    })
    state = run_agent(
        llm,
        None,
        TRACE,
        "user-service",
        ROOT,
        incident_id=99,
        sandbox=sandbox,
        limits=limits,
    )
    limits.release_slot("sandboxes", occupied)
    assert state["status"] == "patch_ready"
    assert "concurrency capacity" in state["reason"]
    assert sandbox.baseline_calls == 0


@pytest.mark.redis
def test_real_redis_analysis_slots_never_exceed_two(monkeypatch):
    url = os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
    client = redis.Redis.from_url(
        url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Real Redis at REDIS_URL is not reachable")

    prefix = f"aiops:test:limits:{uuid.uuid4().hex}:"
    limits = RedisLimits(client=client, prefix=prefix)
    held = 0
    maximum = 0
    completed = 0
    lock = threading.Lock()

    def contend(_):
        nonlocal held, maximum, completed
        token = None
        deadline = time.monotonic() + 10
        while token is None and time.monotonic() < deadline:
            token = limits.acquire_slot(
                "analyses", cap=2, lease_seconds=30
            )
            if token is None:
                time.sleep(0.002)
        if token is None:
            return
        with lock:
            held += 1
            maximum = max(maximum, held)
        time.sleep(0.01)
        with lock:
            held -= 1
            completed += 1
        limits.release_slot("analyses", token)

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(contend, range(20)))
        assert maximum <= 2
        assert held == 0
        assert completed == 20
    finally:
        keys = list(client.scan_iter(match=f"{prefix}*"))
        if keys:
            client.delete(*keys)
