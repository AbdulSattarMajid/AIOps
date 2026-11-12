"""Classify incidents as code bugs or operational problems without an LLM call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

_CATEGORIES = frozenset(
    {"code_bug", "config_issue", "capacity_issue", "dependency_outage", "unknown"}
)
_CONFIDENCES = frozenset({"high", "medium", "low"})

# Matches an environment-variable-style name: uppercase letters, digits, underscores,
# at least 2 chars, starts with a letter.
_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

# Key-error message is usually the repr of the missing key, e.g. "'MY_VAR'" or '"MY_VAR"'.
_KEY_MSG_RE = re.compile(r"""['\"]([^'"]+)['\"]""")


@dataclass
class Classification:
    category: str          # one of _CATEGORIES
    confidence: str        # one of _CONFIDENCES
    reason: str
    signals: List[str] = field(default_factory=list)
    suggested_action: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _env_var_name(key_message: str, error_message: str) -> str:
    """Return a validated env-var name to interpolate into the action, or a fallback."""
    # First try the KeyError repr (e.g. "'SHOP_PAYMENT_KEY'" -> 'SHOP_PAYMENT_KEY')
    for candidate in _KEY_MSG_RE.findall(key_message):
        if _ENV_VAR_RE.match(candidate):
            return candidate
    # Fall back to scanning the error message for an UPPER_CASE word
    for word in re.findall(r"\b[A-Z][A-Z0-9_]{1,63}\b", error_message):
        if _ENV_VAR_RE.match(word):
            return word
    return "the missing variable"


def _is_environ_access(crash_line: str) -> bool:
    """True when the crash line reads from os.environ or environ[...]."""
    return bool(
        re.search(r"\bos\.environ\s*\[", crash_line)
        or re.search(r"\benviron\s*\[", crash_line)
    )


def _mentions_env_var(message: str) -> bool:
    """True when the message says a variable is not set, plus an UPPER_CASE name."""
    lower = message.lower()
    if not any(
        kw in lower
        for kw in ("environment variable", "env var", "is not set")
    ):
        return False
    return bool(re.search(r"\b[A-Z][A-Z0-9_]{1,63}\b", message))


def _capacity_keywords(message: str) -> List[str]:
    lower = message.lower()
    hits = []
    for kw in (
        "pool timeout",
        "queuepool limit",
        "too many connections",
        "too many clients",
        "connection pool exhausted",
        "no space left on device",
        "too many open files",
        "out of memory",
    ):
        if kw in lower:
            hits.append(kw)
    return hits


def _dependency_keywords(message: str) -> List[str]:
    lower = message.lower()
    hits = []
    for kw in (
        "connection refused",
        "name or service not known",
        "temporary failure in name resolution",
        "getaddrinfo failed",
        "max retries exceeded with url",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway",
    ):
        if kw in lower:
            hits.append(kw)
    return hits


_DEPENDENCY_EXCEPTION_PARTS = (
    "ConnectionRefused",
    "ConnectionReset",
    "ConnectTimeout",
    "ConnectError",
)
_CODE_BUG_EXCEPTIONS = frozenset({
    "AttributeError", "TypeError", "IndexError", "KeyError",
    "ZeroDivisionError", "ValueError", "NameError", "UnboundLocalError",
    "AssertionError", "RecursionError",
})

# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def classify_incident(
    exception_type: str,
    message: str,
    crash_line_text: str,
) -> Classification:
    """Classify an incident using rule-based heuristics.

    Rules are checked in order; the first high-confidence match wins.
    Never raises; unexpected input returns an *unknown* classification.
    """
    try:
        return _classify(exception_type or "", message or "", crash_line_text or "")
    except Exception:  # noqa: BLE001
        return Classification(
            category="unknown",
            confidence="low",
            reason="Classification failed due to unexpected input.",
        )


def _classify(exc: str, msg: str, crash: str) -> Classification:
    # ------------------------------------------------------------------
    # a) config_issue
    # ------------------------------------------------------------------
    if exc == "KeyError":
        if _is_environ_access(crash):
            name = _env_var_name(msg, msg)
            return Classification(
                category="config_issue",
                confidence="high",
                reason="KeyError on os.environ access in crash line.",
                signals=["KeyError", "os.environ access"],
                suggested_action=(
                    f"Set the environment variable {name} for this service and "
                    "restart it. No code change needed."
                ),
            )
        # NOT config: a plain dict/config KeyError with a non-env-var key is code_bug
        # (handled below in rule d)

    if _mentions_env_var(msg):
        name = _env_var_name("", msg)
        return Classification(
            category="config_issue",
            confidence="high",
            reason="Message mentions a missing environment variable.",
            signals=["missing env var in message"],
            suggested_action=(
                f"Set the environment variable {name} for this service and "
                "restart it. No code change needed."
            ),
        )

    # ------------------------------------------------------------------
    # b) capacity_issue
    # ------------------------------------------------------------------
    cap_hits = _capacity_keywords(msg)
    if cap_hits or exc == "MemoryError":
        signals = list(cap_hits) or ["MemoryError"]
        return Classification(
            category="capacity_issue",
            confidence="high",
            reason="Message indicates a resource-capacity problem.",
            signals=signals,
            suggested_action=(
                "Check connection pool size, memory or disk for this service "
                "and consider scaling."
            ),
        )

    # ------------------------------------------------------------------
    # c) dependency_outage
    # ------------------------------------------------------------------
    dep_hits = _dependency_keywords(msg)
    is_dep_exc = (
        any(part in exc for part in _DEPENDENCY_EXCEPTION_PARTS)
        or exc in ("ConnectionError", "gaierror")
    )
    if dep_hits or is_dep_exc:
        signals = dep_hits + ([exc] if is_dep_exc else [])
        return Classification(
            category="dependency_outage",
            confidence="high",
            reason="Exception or message indicates a downstream connectivity failure.",
            signals=signals,
            suggested_action=(
                "Check that the downstream service or database is running "
                "and reachable from this service."
            ),
        )

    # ------------------------------------------------------------------
    # d) code_bug
    # ------------------------------------------------------------------
    if exc in _CODE_BUG_EXCEPTIONS:
        return Classification(
            category="code_bug",
            confidence="high",
            reason=f"{exc} is a well-known code-bug exception.",
            signals=[exc],
            suggested_action="",
        )

    # ------------------------------------------------------------------
    # e) unknown
    # ------------------------------------------------------------------
    return Classification(
        category="unknown",
        confidence="low",
        reason="No rule matched the exception or message.",
        signals=[],
        suggested_action="",
    )
