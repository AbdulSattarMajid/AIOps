"""Small, dependency-free exception reporting SDK for AIOps."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

__all__ = ["install", "report_exception"]

_MAX_REPORTS_PER_SECOND = 10
_MAX_TRACEBACK_LENGTH = 20_000
_MAX_MESSAGE_LENGTH = 10_000
_MAX_SERVICE_NAME_LENGTH = 128
_LOGS_PATH = "/api/v1/telemetry/logs"
_rate_limit_lock = threading.Lock()
_recent_reports: deque[float] = deque()
_FRAME_LINE = re.compile(
    r"""^(\s*File )("[^"]*"|'[^']*')(\s*, line \d+, in .*)$""",
    re.MULTILINE,
)


class _Reporter:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        service_name: str,
        app_root: str | os.PathLike[str],
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._api_key = api_key
        self.service_name = service_name[:_MAX_SERVICE_NAME_LENGTH]
        self.app_root = Path(app_root).resolve()

    def report_exception(self, exc: BaseException) -> None:
        try:
            with _rate_limit_lock:
                now = time.monotonic()
                while _recent_reports and now - _recent_reports[0] >= 1:
                    _recent_reports.popleft()
                if len(_recent_reports) >= _MAX_REPORTS_PER_SECOND:
                    return
                _recent_reports.append(now)

            payload = self._build_payload(exc)
            thread = threading.Thread(
                target=self._send_in_background,
                args=(payload,),
                daemon=True,
                name="aiops-sdk-reporter",
            )
            thread.start()
        except Exception:  # noqa: BLE001 - reporting must not disrupt the host app
            # Reporting must never interfere with the host application's error handling.
            return

    def _build_payload(self, exc: BaseException) -> dict[str, str]:
        formatted = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        formatted = self._rewrite_app_paths(formatted)[-_MAX_TRACEBACK_LENGTH:]
        return {
            "timestamp": (
                datetime.now(timezone.utc)  # noqa: UP017 - supports older Python
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "service_name": self.service_name,
            "error_class": type(exc).__name__[:128],
            "message": str(exc)[:_MAX_MESSAGE_LENGTH],
            "stack_trace": formatted,
        }

    def _rewrite_app_paths(self, formatted: str) -> str:
        def rewrite_frame(match: re.Match[str]) -> str:
            filename = match.group(2)[1:-1]
            if filename.startswith("<"):
                return match.group(0)
            try:
                path = Path(filename)
                if not path.is_absolute():
                    return match.group(0)
                path = path.resolve()
                relative = path.relative_to(self.app_root)
            except (OSError, ValueError):
                return match.group(0)
            return f'{match.group(1)}"/app/{relative.as_posix()}"{match.group(3)}'

        return _FRAME_LINE.sub(rewrite_frame, formatted)

    def _send_safely(self, payload: dict[str, str]) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = Request(
                self.api_url + _LOGS_PATH,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self._api_key,
                },
                method="POST",
            )
            with urlopen(request, timeout=2):
                pass
        except Exception:  # noqa: BLE001 - transport failures are best-effort
            return

    def _send_in_background(self, payload: dict[str, str]) -> None:
        try:
            self._send_safely(payload)
        except Exception:  # noqa: BLE001 - never leak sender errors to the host app
            return


class _ExceptionReportingMiddleware:
    def __init__(self, app: Any, reporter: _Reporter) -> None:
        self.app = app
        self.reporter = reporter

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            self.reporter.report_exception(exc)
            raise


_default_reporter: _Reporter | None = None
_default_reporter_lock = threading.Lock()


def install(
    app: Any,
    *,
    api_url: str,
    api_key: str,
    service_name: str,
    app_root: str | os.PathLike[str] | None = None,
) -> None:
    """Install exception reporting middleware on a FastAPI/ASGI application."""
    if not api_url.startswith(("http://", "https://")):
        raise ValueError("api_url must start with http:// or https://")
    reporter = _Reporter(
        api_url=api_url,
        api_key=api_key,
        service_name=service_name,
        app_root=app_root if app_root is not None else os.getcwd(),
    )
    app.add_middleware(_ExceptionReportingMiddleware, reporter=reporter)
    global _default_reporter
    with _default_reporter_lock:
        _default_reporter = reporter


def report_exception(exc: BaseException) -> None:
    """Report an exception manually using the most recently installed settings."""
    with _default_reporter_lock:
        reporter = _default_reporter
    if reporter is not None:
        reporter.report_exception(exc)
