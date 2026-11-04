"""Redis Streams consumer for queued telemetry events."""
from __future__ import annotations

import json
import os
import socket
import uuid

import redis
from redis.exceptions import RedisError

from aiops.dedup.engine import DedupEngine
from aiops.ingest.schemas import LogEvent
from aiops.limits import LimitStoreError, RedisLimits
from aiops.storage.redis_store import (
    CONSUMER_GROUP,
    DEAD_LETTER_STREAM,
    QUEUE_STREAM,
    RedisStore,
    get_redis,
)


RECLAIM_IDLE_MS = int(os.getenv("STREAM_RECLAIM_IDLE_MS", "60000"))
MAX_DELIVERY_ATTEMPTS = int(os.getenv("STREAM_MAX_DELIVERY_ATTEMPTS", "5"))
DEFAULT_BATCH_SIZE = int(os.getenv("STREAM_BATCH_SIZE", "10"))
BLOCK_TIMEOUT_MS = int(os.getenv("STREAM_BLOCK_MS", "1000"))


def _ensure_group(client, stream_name: str, group_name: str) -> None:
    try:
        client.xgroup_create(
            stream_name, group_name, id="0", mkstream=True
        )
    except redis.ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise


def _delivery_counts(client, stream_name: str, group_name: str, message_ids):
    counts = {}
    for message_id in message_ids:
        pending = client.xpending_range(
            stream_name,
            group_name,
            min=message_id,
            max=message_id,
            count=1,
        )
        if pending:
            counts[message_id] = int(pending[0]["times_delivered"])
    return counts


def _dead_letter(
    client,
    stream_name: str,
    dead_letter_stream: str,
    group_name: str,
    message_id: str,
):
    pipe = client.pipeline(transaction=True)
    pipe.xadd(
        dead_letter_stream,
        {
            "source_stream": stream_name,
            "source_id": message_id,
            "reason": "Processing failed after 5 delivery attempts.",
        },
    )
    pipe.xack(stream_name, group_name, message_id)
    pipe.xdel(stream_name, message_id)
    pipe.execute()


def _ack_and_delete(client, stream_name: str, group_name: str, message_id: str):
    pipe = client.pipeline(transaction=True)
    pipe.xack(stream_name, group_name, message_id)
    pipe.xdel(stream_name, message_id)
    pipe.execute()


def _default_auto_analyzer(incident, limits):
    from aiops.api import main

    state = main.run_agent(
        main.get_llm(),
        main.get_store(),
        trace=incident.sample_trace,
        service_name=incident.service_name,
        repo_root=main.REPO_ROOT,
        incident_id=incident.id,
        sandbox=main.get_sandbox(),
        limits=limits,
        signature=incident.signature,
    )
    return main.summarize(state)


def _maybe_auto_analyze(processor, incident, client, auto_analyzer):
    if os.getenv("AIOPS_AUTO_ANALYZE", "off").lower() != "on":
        return
    limits = RedisLimits(client=client)
    slot = None
    try:
        slot = limits.acquire_slot("analyses")
        if slot is None:
            return
        if not limits.claim_cooldown(incident.signature):
            limits.record_cooldown_skip()
            return
        limits.record_auto_analysis()
        result = auto_analyzer(incident, limits)
        status = str(result.get("status", "escalated"))
        raw_reason = result.get("reason", "")
        if raw_reason in {
            "incident token budget reached",
            "daily token budget reached",
        }:
            reason = raw_reason
        elif status == "escalated":
            reason = "Automatic analysis was escalated."
        else:
            reason = ""
        processor.store.record_analysis_result(
            incident.id, status, reason
        )
    except Exception:
        try:
            processor.store.record_analysis_result(
                incident.id,
                "escalated",
                "Automatic analysis failed; inspect service health.",
            )
        except Exception:
            pass
    finally:
        if slot is not None:
            try:
                limits.release_slot("analyses", slot)
            except LimitStoreError:
                pass


def drain_once(
    client,
    max_messages: int,
    *,
    engine: DedupEngine | None = None,
    consumer_name: str | None = None,
    stream_name: str = QUEUE_STREAM,
    group_name: str = CONSUMER_GROUP,
    dead_letter_stream: str = DEAD_LETTER_STREAM,
    min_idle_ms: int = RECLAIM_IDLE_MS,
    block_ms: int = 1,
    max_delivery_attempts: int = MAX_DELIVERY_ATTEMPTS,
    auto_analyzer=None,
) -> int:
    """Claim stale pending messages, then consume new messages and acknowledge successes."""
    if max_messages < 1:
        return 0
    _ensure_group(client, stream_name, group_name)
    consumer = consumer_name or (
        f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex}"
    )
    processor = engine or DedupEngine(store=RedisStore(client=client))

    claimed = client.xautoclaim(
        stream_name,
        group_name,
        consumer,
        min_idle_time=min_idle_ms,
        start_id="0-0",
        count=max_messages,
    )
    messages = list(claimed[1])
    if len(messages) < max_messages:
        remaining = max_messages - len(messages)
        options = {"count": remaining}
        if block_ms > 0:
            options["block"] = block_ms
        read = client.xreadgroup(
            group_name, consumer, {stream_name: ">"}, **options
        )
        for _stream, items in read:
            messages.extend(items)

    if not messages:
        return 0

    counts = _delivery_counts(
        client,
        stream_name,
        group_name,
        {message_id for message_id, _ in messages},
    )
    handled = 0
    for message_id, fields in messages:
        attempts = counts.get(message_id, 1)
        if attempts >= max_delivery_attempts:
            _dead_letter(
                client, stream_name, dead_letter_stream, group_name, message_id
            )
            handled += 1
            continue
        try:
            payload = json.loads(fields["event"])
            event = LogEvent.model_validate(payload)
            incident, _ = processor.process(event, event_id=message_id)
        except Exception:
            continue
        _maybe_auto_analyze(
            processor,
            incident,
            client,
            auto_analyzer or _default_auto_analyzer,
        )
        _ack_and_delete(client, stream_name, group_name, message_id)
        handled += 1
    return handled


def run_worker() -> None:
    client = get_redis()
    consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex}"
    engine = DedupEngine(store=RedisStore(client=client))
    try:
        _ensure_group(client, QUEUE_STREAM, CONSUMER_GROUP)
        while True:
            drain_once(
                client,
                DEFAULT_BATCH_SIZE,
                engine=engine,
                consumer_name=consumer,
                block_ms=BLOCK_TIMEOUT_MS,
            )
    except KeyboardInterrupt:
        return
    except RedisError:
        raise


if __name__ == "__main__":
    run_worker()
