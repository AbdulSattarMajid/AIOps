"""Build size-bounded LogEvent payloads from caught exceptions."""
from __future__ import annotations

from datetime import datetime, timezone
import traceback

from aiops.ingest.schemas import (
    MAX_MESSAGE_LENGTH,
    MAX_SERVICE_NAME_LENGTH,
    MAX_STACK_TRACE_LENGTH,
    LogEvent,
)


def report_crash(client, service_name: str, exc: Exception):
    trace = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "service_name": str(service_name)[:MAX_SERVICE_NAME_LENGTH],
        "error_class": type(exc).__name__[:128],
        "message": str(exc)[:MAX_MESSAGE_LENGTH],
        "stack_trace": trace[-MAX_STACK_TRACE_LENGTH:],
    }
    return client.send_log(payload)
