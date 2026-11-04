"""Offline demo of Redis Streams ingestion and background deduplication."""
from collections import Counter
import os
import sys

import fakeredis
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.security.auth import hash_api_key
from aiops.storage import redis_store
from aiops.storage.redis_store import QUEUE_STREAM, RedisStore
from aiops.worker.worker import drain_once


def main_demo():
    client = fakeredis.FakeRedis(decode_responses=True)
    redis_store.get_redis = lambda: client

    api_key = "week6b-demo-key"
    os.environ["AIOPS_API_KEY_HASHES"] = hash_api_key(api_key)
    os.environ["AIOPS_INGEST_MODE"] = "queue"
    os.environ["RATE_LIMIT_INGEST_PER_MINUTE"] = "2000"
    main.engine = DedupEngine(store=RedisStore(client=client))

    payload = {
        "timestamp": "2026-11-04T10:00:00Z",
        "service_name": "demo-service",
        "error_class": "RuntimeError",
        "stack_trace": (
            "RuntimeError: database connection failed in worker startup "
            "while opening primary storage pool"
        ),
    }
    status_counts = Counter()
    with TestClient(
        main.app,
        headers={"X-API-Key": api_key},
    ) as api_client:
        for _ in range(1000):
            response = api_client.post(
                "/api/v1/telemetry/logs", json=payload
            )
            status_counts[response.status_code] += 1

    queue_before = client.xlen(QUEUE_STREAM)
    while drain_once(client, 100, engine=main.engine):
        pass
    queue_after = client.xlen(QUEUE_STREAM)

    print(f"HTTP status counts: {dict(sorted(status_counts.items()))}")
    print(f"queue length before draining: {queue_before}")
    print(f"queue length after draining: {queue_after}")
    print(f"incident count: {len(main.engine.incidents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_demo())
