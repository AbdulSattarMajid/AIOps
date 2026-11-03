"""Rate limiting for API endpoints and failed authentication tracking.

TODO: In Week 6, migrate rate limiting state from in-memory dictionary to Redis.

Features:
- Sliding-window tracking per API key (using key hash, never raw key)
- Stricter rate limit for /analyze endpoint
- Client-IP rate limit for failed authentication attempts (brute-force defense)
- Thread-safe with automatic cleanup to prevent memory bloat
- Injectable clock for deterministic, sleep-free testing
"""
from __future__ import annotations

import collections
import math
import os
import threading
import time
from typing import Callable, Deque, Dict, Optional, Tuple


def get_rate_limit_config() -> Dict[str, int]:
    """Load rate limit numbers from environment with safe defaults."""
    return {
        "ingest_limit": int(os.getenv("RATE_LIMIT_INGEST_PER_MINUTE", "300")),
        "analyze_limit": int(os.getenv("RATE_LIMIT_ANALYZE_PER_MINUTE", "10")),
        "failed_auth_limit": int(os.getenv("RATE_LIMIT_FAILED_AUTH_PER_MINUTE", "5")),
        "window_seconds": int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
    }


class RateLimiter:
    """Thread-safe sliding-window rate limiter with an injectable clock."""

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock: Callable[[], float] = clock or time.time
        self._lock = threading.Lock()
        # Key: (bucket_type, identity) -> Deque of timestamps
        self._buckets: Dict[Tuple[str, str], Deque[float]] = {}

    def set_clock(self, clock: Callable[[], float]) -> None:
        """Inject a custom clock function for testing."""
        self._clock = clock or time.time

    def _cleanup_locked(self, now: float, max_age: float = 300.0) -> None:
        """Remove empty or stale buckets to bound memory usage."""
        stale_keys = []
        for key, timestamps in self._buckets.items():
            while timestamps and timestamps[0] <= now - max_age:
                timestamps.popleft()
            if not timestamps:
                stale_keys.append(key)
        for key in stale_keys:
            self._buckets.pop(key, None)

    def check_and_record(
        self,
        bucket_type: str,
        identity: str,
        limit: int,
        window_seconds: int = 60,
    ) -> Tuple[bool, int]:
        """Check if request is within limit and record current timestamp if allowed.

        Returns:
            (is_allowed, retry_after_seconds)
        """
        now = self._clock()
        cutoff = now - window_seconds
        key = (bucket_type, identity)

        with self._lock:
            # Periodic cleanup if map grows large
            if len(self._buckets) > 1000:
                self._cleanup_locked(now, max_age=float(window_seconds))

            timestamps = self._buckets.get(key)
            if timestamps is None:
                timestamps = collections.deque()
                self._buckets[key] = timestamps

            # Prune timestamps older than sliding window
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= limit:
                oldest = timestamps[0]
                retry_after = max(1, int(math.ceil(oldest + window_seconds - now)))
                return False, retry_after

            timestamps.append(now)
            return True, 0

    def is_rate_limited(
        self,
        bucket_type: str,
        identity: str,
        limit: int,
        window_seconds: int = 60,
    ) -> Tuple[bool, int]:
        """Check if an identity has reached the limit without recording a new timestamp.

        Useful for checking lockouts (e.g. failed auth) prior to processing.
        """
        now = self._clock()
        cutoff = now - window_seconds
        key = (bucket_type, identity)

        with self._lock:
            timestamps = self._buckets.get(key)
            if not timestamps:
                return False, 0

            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= limit:
                oldest = timestamps[0]
                retry_after = max(1, int(math.ceil(oldest + window_seconds - now)))
                return True, retry_after
            return False, 0

    def record_failure(
        self,
        bucket_type: str,
        identity: str,
        window_seconds: int = 60,
    ) -> None:
        """Record an event (e.g. failed authentication attempt) for sliding-window lockout."""
        now = self._clock()
        cutoff = now - window_seconds
        key = (bucket_type, identity)

        with self._lock:
            timestamps = self._buckets.get(key)
            if timestamps is None:
                timestamps = collections.deque()
                self._buckets[key] = timestamps

            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            timestamps.append(now)

    def reset(self) -> None:
        """Reset all rate limiter state."""
        with self._lock:
            self._buckets.clear()


# Default singleton instance
default_limiter = RateLimiter()
