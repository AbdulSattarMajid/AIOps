"""Redis-backed incident storage with an explicit in-memory test store."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from functools import lru_cache
from typing import Dict, List, Optional, Protocol, Tuple

import numpy as np
import redis
from redis.exceptions import RedisError, WatchError

DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_VECTORS_PER_SERVICE = 500
DEDUP_LOCK_TTL_SECONDS = 10
DEDUP_LOCK_WAIT_SECONDS = 15
QUEUE_STREAM = "aiops:events"
DEAD_LETTER_STREAM = "aiops:events:dead"
CONSUMER_GROUP = "aiops-workers"
DEFAULT_STREAM_MAX_LEN = 50000


class RedisStoreError(RuntimeError):
    """Raised when incident state cannot be reliably read or written."""


class DedupStore(Protocol):
    def process(
        self,
        event: Dict,
        signature: str,
        vector: List[float],
        threshold: float,
        event_id: Optional[str] = None,
    ) -> Tuple[Dict, bool]: ...

    def get_incident(self, incident_id: int) -> Optional[Dict]: ...

    def list_incidents(self) -> List[Dict]: ...


@lru_cache(maxsize=8)
def _redis_for_url(url: str):
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def get_redis():
    url = os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
    return _redis_for_url(url)


def _find_near_match(candidates, vector: List[float], threshold: float):
    if not candidates:
        return None
    matrix = np.asarray([candidate["vector"] for candidate in candidates])
    scores = matrix @ np.asarray(vector)
    best = int(np.argmax(scores))
    if scores[best] >= threshold:
        return int(candidates[best]["id"])
    return None


class RedisStore:
    def __init__(
        self,
        client=None,
        prefix: str = "aiops:",
        ttl_seconds: Optional[int] = None,
        max_vectors_per_service: int = MAX_VECTORS_PER_SERVICE,
    ):
        self.client = client
        self.prefix = prefix
        self.ttl_seconds = (
            int(os.getenv("INCIDENT_TTL_SECONDS") or DEFAULT_TTL_SECONDS)
            if ttl_seconds is None else ttl_seconds
        )
        self.max_vectors_per_service = max_vectors_per_service

    def _client(self):
        return self.client if self.client is not None else get_redis()

    def _incident_key(self, incident_id: int) -> str:
        return f"{self.prefix}incident:{incident_id}"

    def _signature_key(self, signature: str) -> str:
        return f"{self.prefix}signature:{signature}"

    def _service_key(self, service_name: str) -> str:
        service_hash = hashlib.sha256(service_name.encode()).hexdigest()
        return f"{self.prefix}service:{service_hash}:vectors"

    def _lock_key(self, service_name: str) -> str:
        service_hash = hashlib.sha256(service_name.encode()).hexdigest()
        return f"{self.prefix}lock:{service_hash}"

    def _load_incident(self, client, incident_id: int):
        raw = client.get(self._incident_key(incident_id))
        return json.loads(raw) if raw is not None else None

    @staticmethod
    def _bump(incident: Dict, event: Dict) -> Dict:
        incident["count"] += 1
        incident["last_seen"] = event["timestamp"]
        for kind, count in event["redaction"].items():
            incident["redaction"][kind] = (
                incident["redaction"].get(kind, 0) + count
            )
        return incident

    def _release_lock(self, client, lock_key: str, token: str) -> None:
        while True:
            pipe = client.pipeline()
            try:
                pipe.watch(lock_key)
                if pipe.get(lock_key) != token:
                    return
                pipe.multi()
                pipe.delete(lock_key)
                pipe.execute()
                return
            except WatchError:
                continue
            finally:
                pipe.reset()

    def process(
        self,
        event: Dict,
        signature: str,
        vector: List[float],
        threshold: float,
        event_id: Optional[str] = None,
    ) -> Tuple[Dict, bool]:
        service_name = event["service_name"]
        lock_key = self._lock_key(service_name)
        token = uuid.uuid4().hex
        deadline = time.monotonic() + DEDUP_LOCK_WAIT_SECONDS
        client = None
        acquired = False
        try:
            client = self._client()
            while not client.set(
                lock_key, token, nx=True, ex=DEDUP_LOCK_TTL_SECONDS
            ):
                if time.monotonic() >= deadline:
                    raise RedisStoreError(
                        "Redis deduplication lock could not be acquired."
                    )
                time.sleep(0.005)
            acquired = True

            processed_key = (
                f"{self.prefix}processed:{event_id}" if event_id else None
            )
            if processed_key:
                processed_id = client.get(processed_key)
                if processed_id is not None:
                    processed_incident = self._load_incident(
                        client, int(processed_id)
                    )
                    if processed_incident is not None:
                        return processed_incident, False

            signature_key = self._signature_key(signature)
            incident_id = client.get(signature_key)
            if incident_id is not None:
                incident = self._load_incident(client, int(incident_id))
                if incident is not None:
                    incident = self._bump(incident, event)
                    pipe = client.pipeline(transaction=True)
                    pipe.set(
                        self._incident_key(incident["id"]),
                        json.dumps(incident, separators=(",", ":")),
                        ex=self.ttl_seconds,
                    )
                    pipe.set(signature_key, incident["id"], ex=self.ttl_seconds)
                    if processed_key:
                        pipe.set(
                            processed_key, incident["id"], ex=self.ttl_seconds
                        )
                    pipe.execute()
                    return incident, False

            vectors_key = self._service_key(service_name)
            raw_candidates = client.lrange(
                vectors_key, -self.max_vectors_per_service, -1
            )
            decoded_candidates = [json.loads(raw) for raw in raw_candidates]
            candidate_values = client.mget([
                self._incident_key(int(candidate["id"]))
                for candidate in decoded_candidates
            ])
            candidates = [
                candidate for candidate, stored in zip(
                    decoded_candidates, candidate_values
                )
                if stored is not None
            ]

            near_id = _find_near_match(candidates, vector, threshold)
            if near_id is not None:
                incident = self._load_incident(client, near_id)
                if incident is not None:
                    incident = self._bump(incident, event)
                    pipe = client.pipeline(transaction=True)
                    pipe.set(
                        self._incident_key(near_id),
                        json.dumps(incident, separators=(",", ":")),
                        ex=self.ttl_seconds,
                    )
                    pipe.set(signature_key, near_id, ex=self.ttl_seconds)
                    if processed_key:
                        pipe.set(processed_key, near_id, ex=self.ttl_seconds)
                    pipe.execute()
                    return incident, False

            incident_id = int(client.incr(f"{self.prefix}next_id"))
            incident = {
                "id": incident_id,
                "service_name": service_name,
                "error_class": event["error_class"],
                "signature": signature,
                "first_seen": event["timestamp"],
                "last_seen": event["timestamp"],
                "count": 1,
                "sample_trace": event["sample_trace"],
                "redaction": dict(event["redaction"]),
            }
            pipe = client.pipeline(transaction=True)
            pipe.set(
                self._incident_key(incident_id),
                json.dumps(incident, separators=(",", ":")),
                ex=self.ttl_seconds,
            )
            pipe.set(signature_key, incident_id, ex=self.ttl_seconds)
            if processed_key:
                pipe.set(processed_key, incident_id, ex=self.ttl_seconds)
            pipe.rpush(
                vectors_key,
                json.dumps(
                    {"id": incident_id, "vector": vector},
                    separators=(",", ":"),
                ),
            )
            pipe.ltrim(vectors_key, -self.max_vectors_per_service, -1)
            pipe.expire(vectors_key, self.ttl_seconds)
            pipe.execute()
            return incident, True
        except RedisStoreError:
            raise
        except RedisError:
            raise RedisStoreError(
                "Redis storage is unavailable; incident state was not saved."
            ) from None
        finally:
            if acquired:
                try:
                    self._release_lock(client, lock_key, token)
                except RedisError:
                    raise RedisStoreError(
                        "Redis storage is unavailable; incident state may not "
                        "be safe to update."
                    ) from None

    def get_incident(self, incident_id: int) -> Optional[Dict]:
        try:
            return self._load_incident(self._client(), incident_id)
        except RedisError:
            raise RedisStoreError(
                "Redis storage is unavailable; incident state cannot be read."
            ) from None

    def list_incidents(self) -> List[Dict]:
        try:
            client = self._client()
            incidents = []
            for key in client.scan_iter(match=f"{self.prefix}incident:*"):
                raw = client.get(key)
                if raw is not None:
                    incidents.append(json.loads(raw))
            return sorted(incidents, key=lambda incident: incident["id"])
        except RedisError:
            raise RedisStoreError(
                "Redis storage is unavailable; incident state cannot be read."
            ) from None


class InMemoryStore:
    """Process-local test store; production defaults to RedisStore."""

    def __init__(self, max_vectors_per_service: int = MAX_VECTORS_PER_SERVICE):
        self.max_vectors_per_service = max_vectors_per_service
        self._incidents: Dict[int, Dict] = {}
        self._by_signature: Dict[str, int] = {}
        self._vectors: Dict[str, List[Dict]] = {}
        self._next_id = 1
        self._processed: Dict[str, int] = {}
        self._lock = threading.RLock()

    def process(
        self,
        event: Dict,
        signature: str,
        vector: List[float],
        threshold: float,
        event_id: Optional[str] = None,
    ) -> Tuple[Dict, bool]:
        with self._lock:
            if event_id and event_id in self._processed:
                incident_id = self._processed[event_id]
                return dict(self._incidents[incident_id]), False
            incident_id = self._by_signature.get(signature)
            if incident_id is not None:
                incident = RedisStore._bump(
                    self._incidents[incident_id], event
                )
                if event_id:
                    self._processed[event_id] = incident_id
                return incident, False

            candidates = self._vectors.get(event["service_name"], [])
            near_id = _find_near_match(candidates, vector, threshold)
            if near_id is not None:
                self._by_signature[signature] = near_id
                incident = RedisStore._bump(self._incidents[near_id], event)
                if event_id:
                    self._processed[event_id] = near_id
                return incident, False

            incident_id = self._next_id
            self._next_id += 1
            incident = {
                "id": incident_id,
                "service_name": event["service_name"],
                "error_class": event["error_class"],
                "signature": signature,
                "first_seen": event["timestamp"],
                "last_seen": event["timestamp"],
                "count": 1,
                "sample_trace": event["sample_trace"],
                "redaction": dict(event["redaction"]),
            }
            self._incidents[incident_id] = incident
            self._by_signature[signature] = incident_id
            if event_id:
                self._processed[event_id] = incident_id
            vectors = self._vectors.setdefault(event["service_name"], [])
            vectors.append({"id": incident_id, "vector": vector})
            del vectors[:-self.max_vectors_per_service]
            return incident, True

    def get_incident(self, incident_id: int) -> Optional[Dict]:
        with self._lock:
            incident = self._incidents.get(incident_id)
            return dict(incident) if incident is not None else None

    def list_incidents(self) -> List[Dict]:
        with self._lock:
            return [dict(item) for item in self._incidents.values()]


def _stream_max_len() -> int:
    return int(os.getenv("STREAM_MAX_LEN") or DEFAULT_STREAM_MAX_LEN)


def enqueue_event(event: Dict, client=None, stream: str = QUEUE_STREAM) -> str:
    """Append a redacted JSON event without silently trimming a full queue."""
    max_len = _stream_max_len()
    try:
        client = client if client is not None else get_redis()
        while True:
            pipe = client.pipeline()
            try:
                pipe.watch(stream)
                if pipe.xlen(stream) >= max_len:
                    raise RedisStoreError("The ingestion stream is full.")
                pipe.multi()
                event_id = pipe.xadd(
                    stream,
                    {"event": json.dumps(event, separators=(",", ":"))},
                    maxlen=max_len,
                    approximate=True,
                )
                return pipe.execute()[0]
            except WatchError:
                continue
            finally:
                pipe.reset()
    except RedisStoreError:
        raise
    except RedisError:
        raise RedisStoreError(
            "Redis queue is unavailable; the event was not enqueued."
        ) from None


def queue_stats(
    client=None,
    stream: str = QUEUE_STREAM,
    dead_letter_stream: str = DEAD_LETTER_STREAM,
) -> Dict[str, int]:
    try:
        client = client if client is not None else get_redis()
        try:
            client.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
        except redis.ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
        pending = client.xpending(stream, CONSUMER_GROUP)
        return {
            "stream_length": client.xlen(stream),
            "pending_count": int(pending.get("pending", 0)),
            "dead_letter_count": client.xlen(dead_letter_stream),
        }
    except RedisError:
        raise RedisStoreError(
            "Redis queue is unavailable; queue statistics cannot be read."
        ) from None
