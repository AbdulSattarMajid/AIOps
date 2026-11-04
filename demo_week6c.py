"""Offline demo of Redis-backed storm protection."""
from collections import Counter
import os
import sys

import fakeredis
from fastapi.testclient import TestClient

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "true")
sys.path.insert(0, "src")

from aiops.agent.fake_llm import FakeLLM
from aiops.agent.graph import run_agent, summarize
from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.limits import RedisLimits
from aiops.security.auth import hash_api_key
from aiops.storage import redis_store
from aiops.storage.redis_store import RedisStore
from aiops.worker.worker import drain_once


def main_demo():
    client = fakeredis.FakeRedis(decode_responses=True)
    redis_store.get_redis = lambda: client
    api_key = "week6c-offline-demo-key"
    os.environ["AIOPS_API_KEY_HASHES"] = hash_api_key(api_key)
    os.environ["AIOPS_INGEST_MODE"] = "queue"
    os.environ["AIOPS_AUTO_ANALYZE"] = "on"
    os.environ["RATE_LIMIT_INGEST_PER_MINUTE"] = "2000"
    os.environ["MAX_CONCURRENT_ANALYSES"] = "2"
    os.environ["MAX_TOKENS_PER_INCIDENT"] = "30000"
    os.environ["DAILY_TOKEN_BUDGET"] = "500000"

    engine = DedupEngine(store=RedisStore(client=client))
    main.engine = engine
    llm = FakeLLM({"def get_user_email": ["NO_PATCH: offline demo complete"]})
    payload = {
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
    status_counts = Counter()
    with TestClient(
        main.app, headers={"X-API-Key": api_key}
    ) as api_client:
        for _ in range(1000):
            response = api_client.post(
                "/api/v1/telemetry/logs", json=payload
            )
            status_counts[response.status_code] += 1

    def analyze(incident, limits):
        state = run_agent(
            llm,
            None,
            incident.sample_trace,
            incident.service_name,
            "mock_services/sample_app",
            incident_id=incident.id,
            limits=limits,
            signature=incident.signature,
        )
        return summarize(state)

    limits = RedisLimits(client=client)
    while drain_once(
        client, 100, engine=engine, auto_analyzer=analyze, block_ms=0
    ):
        pass

    incidents = list(engine.incidents.values())
    analyses_run, cooldown_skips = limits.auto_analysis_stats()
    used = 0
    remaining = min(
        int(os.environ["MAX_TOKENS_PER_INCIDENT"]),
        int(os.environ["DAILY_TOKEN_BUDGET"]),
    )
    if incidents:
        incident_used, daily_used = limits.token_usage(incidents[0].id)
        used = incident_used
        remaining = min(
            int(os.environ["MAX_TOKENS_PER_INCIDENT"]) - incident_used,
            int(os.environ["DAILY_TOKEN_BUDGET"]) - daily_used,
        )
    print(f"incidents created: {len(incidents)}")
    print(f"analyses actually run: {analyses_run}")
    print(f"events skipped by cooldown: {cooldown_skips}")
    print(f"tokens used: {used}")
    print(f"budget remaining: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_demo())
