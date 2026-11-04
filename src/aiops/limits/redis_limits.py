"""Cross-worker cooldown, concurrency slots, and token budgets."""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from redis.exceptions import RedisError, WatchError


class LimitStoreError(RuntimeError):
    """Raised when a limit cannot be safely checked or updated."""


def _positive_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


class RedisLimits:
    def __init__(
        self,
        client=None,
        prefix: str = "aiops:limits:",
        clock=time.time,
    ):
        self.client = client
        self.prefix = prefix
        self.clock = clock

    def _client(self):
        if self.client is not None:
            return self.client
        from aiops.storage import redis_store

        return redis_store.get_redis()

    def _key(self, suffix: str) -> str:
        return self.prefix + suffix

    def claim_cooldown(self, signature: str, seconds: Optional[int] = None) -> bool:
        """Claim a signature cooldown without putting signature text in Redis keys."""
        cooldown = seconds or _positive_setting(
            "ANALYSIS_COOLDOWN_SECONDS", 900
        )
        key = self._key(
            "cooldown:" + hashlib.sha256(signature.encode()).hexdigest()
        )
        client = self._client()
        now = float(self.clock())
        try:
            if client.set(key, str(now), nx=True, ex=cooldown):
                return True
        except RedisError:
            raise LimitStoreError(
                "Redis is unavailable; analysis cooldown cannot be checked."
            ) from None
        while True:
            pipe = client.pipeline()
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                if raw is None:
                    pipe.multi()
                    pipe.set(key, str(now), nx=True, ex=cooldown)
                    if pipe.execute()[0]:
                        return True
                    continue
                try:
                    claimed_at = float(raw)
                except (TypeError, ValueError):
                    claimed_at = now
                if now - claimed_at < cooldown:
                    return False
                pipe.multi()
                pipe.set(key, str(now), ex=cooldown)
                pipe.execute()
                return True
            except WatchError:
                continue
            except RedisError:
                raise LimitStoreError(
                    "Redis is unavailable; analysis cooldown cannot be checked."
                ) from None
            finally:
                pipe.reset()

    def acquire_slot(
        self,
        kind: str,
        cap: Optional[int] = None,
        lease_seconds: Optional[int] = None,
    ) -> Optional[str]:
        if kind not in {"analyses", "sandboxes"}:
            raise ValueError("Slot kind must be 'analyses' or 'sandboxes'.")
        setting = (
            "MAX_CONCURRENT_ANALYSES"
            if kind == "analyses" else "MAX_CONCURRENT_SANDBOXES"
        )
        limit = cap if cap is not None else _positive_setting(setting, 2)
        lease = lease_seconds or _positive_setting("SLOT_LEASE_SECONDS", 120)
        if limit < 1 or lease < 1:
            raise ValueError("Slot cap and lease must be positive integers.")
        key = self._key("slots:" + kind)
        token = uuid.uuid4().hex
        now = float(self.clock())
        expires_at = now + lease
        client = self._client()
        while True:
            pipe = client.pipeline()
            try:
                pipe.watch(key)
                held = pipe.zrangebyscore(key, f"({now}", "+inf")
                if len(held) >= limit:
                    return None
                pipe.multi()
                pipe.zremrangebyscore(key, "-inf", now)
                pipe.zadd(key, {token: expires_at})
                pipe.expire(key, lease + 60)
                pipe.execute()
                return token
            except WatchError:
                continue
            except RedisError:
                raise LimitStoreError(
                    "Redis is unavailable; concurrency capacity cannot be checked."
                ) from None
            finally:
                pipe.reset()

    def release_slot(self, kind: str, token: str) -> None:
        if kind not in {"analyses", "sandboxes"}:
            raise ValueError("Slot kind must be 'analyses' or 'sandboxes'.")
        try:
            self._client().zrem(self._key("slots:" + kind), token)
        except RedisError:
            raise LimitStoreError(
                "Redis is unavailable; concurrency slot could not be released."
            ) from None

    def check_token_budget(self, incident_id: int) -> Optional[str]:
        incident_limit = _positive_setting("MAX_TOKENS_PER_INCIDENT", 30000)
        daily_limit = _positive_setting("DAILY_TOKEN_BUDGET", 500000)
        now = float(self.clock())
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        incident_key = self._key(f"tokens:incident:{incident_id}")
        daily_key = self._key(f"tokens:daily:{day}")
        client = self._client()
        while True:
            pipe = client.pipeline()
            try:
                pipe.watch(incident_key, daily_key)
                incident_used, daily_used = pipe.mget(
                    incident_key, daily_key
                )
                if int(incident_used or 0) >= incident_limit:
                    return "incident token budget reached"
                if int(daily_used or 0) >= daily_limit:
                    return "daily token budget reached"
                return None
            except WatchError:
                continue
            except (RedisError, TypeError, ValueError):
                raise LimitStoreError(
                    "Redis token budget is unavailable; analysis was stopped."
                ) from None
            finally:
                pipe.reset()

    def record_tokens(self, incident_id: int, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("Token usage cannot be negative.")
        now = float(self.clock())
        current = datetime.fromtimestamp(now, timezone.utc)
        day = current.date().isoformat()
        seconds_to_midnight = (
            (24 - current.hour) * 3600
            - current.minute * 60
            - current.second
        )
        incident_key = self._key(f"tokens:incident:{incident_id}")
        daily_key = self._key(f"tokens:daily:{day}")
        try:
            pipe = self._client().pipeline(transaction=True)
            pipe.incrby(incident_key, tokens)
            pipe.expire(incident_key, 8 * 24 * 60 * 60)
            pipe.incrby(daily_key, tokens)
            pipe.expire(daily_key, seconds_to_midnight + 24 * 60 * 60)
            pipe.execute()
        except RedisError:
            raise LimitStoreError(
                "Redis token usage could not be recorded; analysis was stopped."
            ) from None

    def token_usage(self, incident_id: int) -> tuple[int, int]:
        now = float(self.clock())
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        try:
            values = self._client().mget(
                self._key(f"tokens:incident:{incident_id}"),
                self._key(f"tokens:daily:{day}"),
            )
            return int(values[0] or 0), int(values[1] or 0)
        except RedisError:
            raise LimitStoreError(
                "Redis token usage is unavailable."
            ) from None

    def record_auto_analysis(self) -> None:
        self._increment_stat("auto_analyses")

    def record_cooldown_skip(self) -> None:
        self._increment_stat("cooldown_skips")

    def auto_analysis_stats(self) -> tuple[int, int]:
        try:
            values = self._client().mget(
                self._key("stats:auto_analyses"),
                self._key("stats:cooldown_skips"),
            )
            return int(values[0] or 0), int(values[1] or 0)
        except RedisError:
            raise LimitStoreError(
                "Redis analysis statistics are unavailable."
            ) from None

    def _increment_stat(self, name: str) -> None:
        try:
            self._client().incr(self._key("stats:" + name))
        except RedisError:
            raise LimitStoreError(
                "Redis analysis statistics could not be recorded."
            ) from None
