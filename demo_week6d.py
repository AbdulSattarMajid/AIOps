"""10,000-event storm demo using fakeredis by default or Redis with --real."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
import time
import uuid

import fakeredis
import redis
from fastapi.testclient import TestClient

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "true")
sys.path.insert(0, "src")

from aiops.agent.fake_llm import FakeLLM
from aiops.agent.graph import run_agent, summarize
from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.limits import RedisLimits
from aiops.security.auth import hash_api_key
from aiops.security.redaction import redact_log_event
from aiops.storage import redis_store
from aiops.storage.redis_store import (
    CONSUMER_GROUP,
    DEAD_LETTER_STREAM,
    QUEUE_STREAM,
    RedisStore,
)
from aiops.worker import worker
from aiops.worker.worker import drain_once


EVENT_COUNT = 10_000
API_KEY = "week6d-offline-demo-key"
PAYLOAD = {
    "timestamp": "2026-11-04T10:00:00Z",
    "service_name": "demo-service",
    "error_class": "AttributeError",
    "stack_trace": (
        "Traceback (most recent call last):\n"
        '  File "/app/users.py", line 19, in get_user_email\n'
        "    return user.email\n"
        "AttributeError: 'NoneType' object has no attribute 'email'"
    ),
}


def _drain(client, engine, stream, group, dead_stream, batch_size):
    def analyze(incident, token_limits):
        result = run_agent(
            llm,
            None,
            incident.sample_trace,
            incident.service_name,
            "mock_services/sample_app",
            incident_id=incident.id,
            limits=token_limits,
            signature=incident.signature,
        )
        return summarize(result)

    def drain_worker(index):
        handled = 0
        while True:
            amount = drain_once(
                client,
                batch_size,
                engine=engine,
                consumer_name=f"storm-demo-worker-{index}",
                stream_name=stream,
                group_name=group,
                dead_letter_stream=dead_stream,
                min_idle_ms=60000,
                block_ms=0,
                auto_analyzer=analyze,
            )
            if amount == 0:
                return handled
            handled += amount

    with ThreadPoolExecutor(max_workers=4) as pool:
        return sum(pool.map(drain_worker, range(4)))


def run_demo(use_real_redis: bool) -> int:
    global llm
    if use_real_redis:
        client = redis.Redis.from_url(
            os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=5,
        )
        try:
            client.ping()
        except redis.RedisError:
            print("real Redis is unavailable")
            client.close()
            return 1
        prefix = f"aiops:demo:storm:{uuid.uuid4().hex}:"
        stream = prefix + "events"
        group = prefix + "workers"
        dead_stream = prefix + "dead"
    else:
        client = fakeredis.FakeRedis(decode_responses=True)
        prefix = "aiops:"
        stream = QUEUE_STREAM
        group = CONSUMER_GROUP
        dead_stream = DEAD_LETTER_STREAM

    previous_stream = main.QUEUE_STREAM
    previous_dead = main.DEAD_LETTER_STREAM
    previous_get_redis = redis_store.get_redis
    previous_limits_factory = worker.RedisLimits
    try:
        redis_store.get_redis = lambda: client
        main.QUEUE_STREAM = stream
        main.DEAD_LETTER_STREAM = dead_stream
        if use_real_redis:
            worker.RedisLimits = lambda client=None: RedisLimits(
                client=client, prefix=prefix + "limits:"
            )
            store = RedisStore(client=client, prefix=prefix)
        else:
            store = RedisStore(client=client)
        os.environ["AIOPS_API_KEY_HASHES"] = hash_api_key(API_KEY)
        os.environ["AIOPS_INGEST_MODE"] = "queue"
        os.environ["AIOPS_AUTO_ANALYZE"] = "on"
        os.environ["RATE_LIMIT_INGEST_PER_MINUTE"] = "20000"
        os.environ["MAX_TOKENS_PER_INCIDENT"] = "30000"
        os.environ["DAILY_TOKEN_BUDGET"] = "500000"
        main.engine = DedupEngine(store=store)
        limits = RedisLimits(
            client=client,
            prefix=prefix + "limits:" if use_real_redis else "aiops:limits:",
        )
        global llm
        llm = FakeLLM({"def get_user_email": ["NO_PATCH: demo complete"]})
        statuses = Counter()
        accept_started = time.monotonic()
        with TestClient(
            main.app, headers={"X-API-Key": API_KEY}
        ) as api_client:
            for _ in range(EVENT_COUNT):
                response = api_client.post(
                    "/api/v1/telemetry/logs", json=PAYLOAD
                )
                statuses[response.status_code] += 1
        accept_elapsed = time.monotonic() - accept_started

        drain_started = time.monotonic()
        handled = _drain(
            client,
            main.engine,
            stream,
            group,
            dead_stream,
            batch_size=100,
        )
        drain_elapsed = time.monotonic() - drain_started
        incidents = list(main.engine.incidents.values())
        analyses, _skipped = limits.auto_analysis_stats()
        events_per_second = (
            handled / drain_elapsed if drain_elapsed > 0 else 0.0
        )
        print(f"HTTP status counts: {dict(sorted(statuses.items()))}")
        print(f"time to accept all events: {accept_elapsed:.3f}s")
        print(f"time to drain: {drain_elapsed:.3f}s")
        print(f"events per second: {events_per_second:.1f}")
        print(f"incident count: {len(incidents)}")
        print(
            f"incident occurrence count: "
            f"{incidents[0].count if incidents else 0}"
        )
        print(f"analyses run: {analyses}")
        print(f"queue length after: {client.xlen(stream)}")
        key_count = (
            client.dbsize()
            if not use_real_redis
            else len(list(client.scan_iter(match=prefix + "*")))
        )
        print(f"Redis key count: {key_count}")
        return 0
    finally:
        redis_store.get_redis = previous_get_redis
        worker.RedisLimits = previous_limits_factory
        main.QUEUE_STREAM = previous_stream
        main.DEAD_LETTER_STREAM = previous_dead
        if use_real_redis:
            keys = list(client.scan_iter(match=f"{prefix}*"))
            if keys:
                client.delete(*keys)
            client.close()


if __name__ == "__main__":
    real = "--real" in sys.argv[1:]
    raise SystemExit(run_demo(real))
