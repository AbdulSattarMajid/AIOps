"""Safe HTTP client for the local AIOps API."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from typing import Any, Optional

import httpx


@dataclass
class ApiResult:
    ok: bool
    status_code: Optional[int]
    message: str
    data: Any = None


class AiopsClient:
    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        timeout: float = 5,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self.timeout = timeout
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return (
            f"AiopsClient(base_url={self.base_url!r}, "
            f"api_key={'configured' if self._api_key else 'missing'}, "
            f"timeout={self.timeout!r})"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> ApiResult:
        if not self._api_key:
            return ApiResult(False, None, "key not configured")
        try:
            response = self._http.request(
                method,
                path,
                headers={"X-API-Key": self._api_key},
                json=json,
                timeout=timeout or self.timeout,
            )
        except httpx.RequestError:
            return ApiResult(False, None, "backend unavailable")
        status = response.status_code
        if status == 401:
            return ApiResult(False, status, "unauthorized")
        if status == 429:
            retry_after = response.headers.get("Retry-After", "5")
            try:
                retry_after = str(max(1, int(float(retry_after))))
            except (TypeError, ValueError):
                retry_after = "5"
            return ApiResult(
                False, status, f"rate limited, retry in {retry_after} s"
            )
        if status == 503 or status >= 500:
            return ApiResult(False, status, "backend unavailable")
        if status not in (200, 202):
            return ApiResult(False, status, "request failed")
        try:
            data = response.json()
        except ValueError:
            data = None
        return ApiResult(True, status, "ok", data)

    def health(self) -> ApiResult:
        return self._request("GET", "/health")

    def send_log(self, event_dict: dict) -> ApiResult:
        return self._request(
            "POST", "/api/v1/telemetry/logs", json=event_dict
        )

    def queue_stats(self) -> ApiResult:
        return self._request("GET", "/api/v1/queue/stats")

    def list_incidents(self) -> ApiResult:
        return self._request("GET", "/api/v1/incidents")

    def get_incident(self, incident_id: int) -> ApiResult:
        result = self.list_incidents()
        if not result.ok:
            return result
        incidents = result.data if isinstance(result.data, list) else []
        incident = next(
            (
                item for item in incidents
                if isinstance(item, dict) and item.get("id") == incident_id
            ),
            None,
        )
        if incident is None:
            return ApiResult(False, 404, "incident not found")
        return ApiResult(True, result.status_code, "ok", incident)

    def analyze(self, incident_id: int, deliver: bool = False) -> ApiResult:
        path = f"/api/v1/incidents/{int(incident_id)}/analyze"
        if deliver:
            path += "?deliver=true"
        return self._request("POST", path, timeout=max(120, self.timeout))

    def close(self) -> None:
        self._http.close()


def _read_secret_file(path: Path) -> str:
    try:
        with path.open("rb") as secret_file:
            values = tomllib.load(secret_file)
        value = values.get("AIOPS_DEMO_API_KEY", "")
        return value if isinstance(value, str) else ""
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return ""


def load_config(
    environ: Optional[dict[str, str]] = None,
    secrets_path: Optional[Path] = None,
) -> tuple[str, str]:
    environment = os.environ if environ is None else environ
    base_url = environment.get(
        "AIOPS_API_URL", "http://127.0.0.1:8000"
    )
    api_key = environment.get("AIOPS_DEMO_API_KEY", "")
    if not api_key:
        path = secrets_path or Path(".streamlit") / "secrets.toml"
        api_key = _read_secret_file(path)
    return base_url, api_key


def configured_client(
    environ: Optional[dict[str, str]] = None,
    secrets_path: Optional[Path] = None,
) -> tuple[AiopsClient, bool]:
    base_url, api_key = load_config(environ, secrets_path)
    return AiopsClient(base_url, api_key), bool(api_key)
