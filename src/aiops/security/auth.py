"""Authentication layer for API endpoints.

Requires X-API-Key header on /api/v1/* endpoints.
Keys are validated against SHA-256 hashes configured in AIOPS_API_KEY_HASHES.
Never logs, echoes, or stores plain keys.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SENSITIVE_HEADERS = {"x-api-key", "authorization"}


def hash_api_key(key: str) -> str:
    """Compute SHA-256 hex digest of raw API key."""
    if not isinstance(key, str):
        key = str(key)
    return hashlib.sha256(key.encode("utf-8")).hexdigest().lower()


def get_configured_hashes() -> List[str]:
    """Retrieve list of valid SHA-256 hashes from environment."""
    raw = os.getenv("AIOPS_API_KEY_HASHES", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def verify_api_key(raw_key: Optional[str]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Verify an incoming API key against configured SHA-256 hashes.

    Uses constant-time comparison via hmac.compare_digest.
    Fails closed: if no hashes are configured, returns ('no_hashes_configured').

    Returns:
        (is_valid, matched_hash_or_incoming_hash, error_reason)
    """
    configured = get_configured_hashes()
    if not configured:
        return False, None, "no_hashes_configured"

    if not raw_key or not isinstance(raw_key, str) or not raw_key.strip():
        return False, None, "missing_or_empty"

    incoming_hash = hash_api_key(raw_key.strip())
    matched = False
    matched_hash = None
    for target in configured:
        # Constant-time comparison against each configured hash
        if hmac.compare_digest(incoming_hash, target):
            matched = True
            matched_hash = target

    if matched:
        return True, matched_hash, None
    return False, incoming_hash, "invalid_key"


def redact_headers(headers: Any) -> Dict[str, str]:
    """Redact sensitive headers such as X-API-Key and Authorization for logging."""
    redacted: Dict[str, str] = {}
    if isinstance(headers, dict):
        items = headers.items()
    elif hasattr(headers, "items"):
        items = headers.items()
    elif isinstance(headers, (list, tuple)):
        items = headers
    else:
        items = []

    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            k, v = item
        else:
            continue
        key_str = k.decode("latin1") if isinstance(k, bytes) else str(k)
        val_str = v.decode("latin1") if isinstance(v, bytes) else str(v)
        if key_str.lower() in _SENSITIVE_HEADERS:
            redacted[key_str] = "<REDACTED>"
        else:
            redacted[key_str] = val_str
    return redacted

