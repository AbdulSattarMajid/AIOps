"""Pure data shaping and friendly-state helpers for the dashboard."""
from __future__ import annotations

from typing import Any, Optional


_INCIDENT_FIELDS = (
    "id",
    "service_name",
    "error_class",
    "count",
    "first_seen",
    "last_seen",
    "status",
    "analysis_state",
    "redaction",
)
_DETAIL_FIELDS = (
    "id",
    "service_name",
    "error_class",
    "signature",
    "first_seen",
    "last_seen",
    "count",
    "sample_trace",
    "redaction",
    "status",
    "analysis_state",
)


def shape_incidents(incidents: Any) -> list[dict]:
    if not isinstance(incidents, list):
        return []
    shaped = [
        {field: item[field] for field in _INCIDENT_FIELDS if field in item}
        for item in incidents
        if isinstance(item, dict)
    ]
    return sorted(
        shaped,
        key=lambda item: (
            str(item.get("last_seen", "")),
            int(item.get("id", 0) or 0),
        ),
        reverse=True,
    )


def summarize_redaction(redaction: Any) -> str:
    if not isinstance(redaction, dict) or not redaction:
        return "No redacted values recorded."
    return ", ".join(
        f"{kind}: {count}"
        for kind, count in sorted(redaction.items())
        if isinstance(count, int)
    ) or "No redacted values recorded."


def incident_detail(incident: Any) -> dict:
    if not isinstance(incident, dict):
        return {}
    return {
        field: incident[field]
        for field in _DETAIL_FIELDS
        if field in incident
    }


def empty_state(incidents: Any) -> str:
    return "no incidents yet, run the target app" if not incidents else ""


def api_error_message(result: Any) -> str:
    if result is None:
        return "backend unavailable"
    message = getattr(result, "message", "backend unavailable")
    if message in {
        "key not configured",
        "unauthorized",
        "backend unavailable",
    } or message.startswith("rate limited, retry in "):
        return message
    if not getattr(result, "ok", False):
        return "backend unavailable"
    return ""


def queue_status(queue_result: Any) -> dict:
    if not getattr(queue_result, "ok", False):
        return {
            "stream_length": None,
            "pending_count": None,
            "dead_letter_count": None,
            "message": api_error_message(queue_result),
        }
    data = queue_result.data
    if not isinstance(data, dict):
        return {
            "stream_length": None,
            "pending_count": None,
            "dead_letter_count": None,
            "message": "backend unavailable",
        }
    return {
        "stream_length": data.get("stream_length"),
        "pending_count": data.get("pending_count"),
        "dead_letter_count": data.get("dead_letter_count"),
        "message": "",
    }


def verification_status(analysis: Any) -> str:
    if not isinstance(analysis, dict):
        return ""
    verification = analysis.get("verification")
    return (
        verification.get("status", "")
        if isinstance(verification, dict) else ""
    )


def delivery_button_enabled(analysis: Any) -> bool:
    return verification_status(analysis) in {"verified", "fix_confirmed"}


def format_analysis(analysis: Any) -> dict:
    if not isinstance(analysis, dict):
        return {}
    verification = analysis.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    history = analysis.get("history")
    rejected = []
    if isinstance(history, list):
        rejected = [
            {
                key: item[key]
                for key in ("attempt", "stage", "error")
                if key in item
            }
            for item in history
            if isinstance(item, dict) and item.get("ok") is False
        ]
    evidence = {
        "before_passed": analysis.get("baseline_tests_passed"),
        "before_failed": analysis.get("baseline_tests_failed"),
        "after_passed": verification.get("tests_passed"),
        "after_failed": verification.get("tests_failed"),
        "fixed": verification.get("fixed", []),
        "newly_failing": verification.get("newly_failing", []),
        "still_failing": verification.get("still_failing", []),
    }
    return {
        "status": analysis.get("status", ""),
        "verification_status": verification.get("status", ""),
        "attempts": analysis.get("attempts", 0),
        "tokens_used": analysis.get("tokens_used", 0),
        "reason": analysis.get("reason", ""),
        "rejected_attempts": rejected,
        "diff": analysis.get("patch", ""),
        "evidence": evidence,
        "target_repo": analysis.get("target_repo", ""),
    }
