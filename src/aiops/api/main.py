from __future__ import annotations

import os
from dataclasses import asdict
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from starlette.responses import JSONResponse

from aiops.agent.fake_llm import make_demo_llm
from aiops.agent.graph import run_agent, summarize
from aiops.agent.llm import GroqLLM, MissingAPIKeyError, MissingModelError
from aiops.dedup.engine import DedupEngine
from aiops.delivery.github_pr import DeliveryError, PyGithubClient, deliver
from aiops.delivery.slack import build_slack_text, post_slack
from aiops.ingest.schemas import LogEvent
from aiops.rag.store import RunbookStore
from aiops.sandbox.app_sandbox import AppSandbox
from aiops.sandbox.runner import is_docker_available
from aiops.security.auth import get_configured_hashes, redact_headers, verify_api_key
from aiops.security.ratelimit import (
    RateLimiter,
    default_limiter,
    get_rate_limit_config,
)
from aiops.storage.redis_store import RedisStoreError

load_dotenv()

# In production, documentation endpoints (/docs, /redoc, /openapi.json) are disabled
# to prevent exposing the full API surface and schemas to unauthorized callers.
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").lower() in ("true", "1", "yes")

app = FastAPI(
    title="Self-Healing AIOps",
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)
REPO_ROOT = os.getenv("REPO_ROOT", "mock_services/sample_app")

engine = DedupEngine()
_store = None
rate_limiter = default_limiter


def get_client_ip(scope: dict, headers_dict: dict[str, str]) -> str:
    """Extract real client IP address.

    Does NOT trust X-Forwarded-For unless the direct peer connection is in TRUSTED_PROXIES.
    This prevents attackers from forging X-Forwarded-For headers to evade rate limits.
    """
    peer_ip = "127.0.0.1"
    if scope.get("client"):
        peer_ip = str(scope["client"][0])

    trusted_proxies_raw = os.getenv("TRUSTED_PROXIES", "")
    trusted_proxies = {p.strip() for p in trusted_proxies_raw.split(",") if p.strip()}

    if peer_ip in trusted_proxies:
        forwarded = headers_dict.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()

    return peer_ip


class SecurityMiddleware:
    """Enforces authentication, rate limits, and size caps on /api/v1/* routes.

    Runs before body parsing or route dispatching.
    Leaves open endpoints (/health) untouched.
    """

    def __init__(self, app, limiter: Optional[RateLimiter] = None):
        self.app = app
        self._limiter = limiter

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter or rate_limiter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Health check and non-API endpoints remain open without auth/limits
        if not path.startswith("/api/v1"):
            await self.app(scope, receive, send)
            return

        headers_dict = {}
        for k, v in scope.get("headers", []):
            headers_dict[k.decode("latin1").lower()] = v.decode("latin1")

        # Determine real client IP without trusting spoofable headers by default
        client_ip = get_client_ip(scope, headers_dict)
        config = get_rate_limit_config()

        # 1. Lockout check on client IP for excessive failed auth attempts
        is_locked, retry_after = self.limiter.is_rate_limited(
            "failed_auth",
            client_ip,
            limit=config["failed_auth_limit"],
            window_seconds=config["window_seconds"],
        )
        if is_locked:
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many failed authentication attempts. Please try again later."},
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        # 2. Authentication check BEFORE body is read or parsed
        raw_key = headers_dict.get("x-api-key")
        is_valid, key_hash, error_reason = verify_api_key(raw_key)

        if error_reason == "no_hashes_configured":
            response = JSONResponse(
                status_code=503,
                content={"detail": "Service Unavailable: API authentication is not configured"},
            )
            await response(scope, receive, send)
            return

        if not is_valid:
            # Record failed auth for IP to stop brute-force key guessing
            self.limiter.record_failure(
                "failed_auth", client_ip, window_seconds=config["window_seconds"]
            )
            response = JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )
            await response(scope, receive, send)
            return

        # 3. Rate limiting per API key (using key hash as identity)
        if "/analyze" in path:
            bucket = "analyze"
            limit = config["analyze_limit"]
        else:
            bucket = "ingest"
            limit = config["ingest_limit"]

        allowed, retry_after = self.limiter.check_and_record(
            bucket, key_hash, limit=limit, window_seconds=config["window_seconds"]
        )
        if not allowed:
            response = JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please try again later."},
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        # 4. Request body size cap (using Content-Length and byte counting)
        max_body_bytes = int(os.getenv("MAX_REQUEST_BODY_BYTES", "1048576"))
        content_length_str = headers_dict.get("content-length")
        if content_length_str:
            try:
                content_length = int(content_length_str)
                if content_length > max_body_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Request body exceeds maximum allowed size"},
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass

        # Buffer and count bytes in case Content-Length was omitted or lied
        body = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"")
                body.extend(chunk)
                if len(body) > max_body_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Request body exceeds maximum allowed size"},
                    )
                    await response(scope, receive, send)
                    return
                if not msg.get("more_body", False):
                    break

        messages = [{"type": "http.request", "body": bytes(body), "more_body": False}]

        async def replay_receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


app.add_middleware(SecurityMiddleware)


def get_store():
    global _store
    if _store is None:
        _store = RunbookStore(path="chroma_data")
        _store.load_directory("runbooks/default")
    return _store


def get_llm():
    if os.getenv("AIOPS_LLM") == "fake":
        return make_demo_llm()
    try:
        return GroqLLM()
    except (MissingAPIKeyError, MissingModelError) as error:
        raise HTTPException(status_code=503, detail=str(error))


def get_sandbox():
    if os.getenv("AIOPS_SANDBOX", "on") != "off" and is_docker_available():
        return AppSandbox(REPO_ROOT)
    return None


def get_github(deliver: bool = Query(False)):
    if not deliver:
        return None
    token = os.getenv("GITHUB_TOKEN")
    repository = os.getenv("GITHUB_REPO")
    if not token or not repository:
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub delivery is not configured. Set GITHUB_TOKEN and "
                "GITHUB_REPO in .env."
            ),
        )
    try:
        return PyGithubClient(token, repository)
    except DeliveryError as error:
        raise HTTPException(status_code=503, detail=str(error))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/telemetry/logs")
def ingest_log(event: LogEvent):
    try:
        incident, is_new = engine.process(event)
    except RedisStoreError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {
        "incident_id": incident.id,
        "is_new": is_new,
        "occurrence_count": incident.count,
    }


@app.get("/api/v1/incidents")
def list_incidents():
    try:
        return [asdict(i) for i in engine.incidents.values()]
    except RedisStoreError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/v1/incidents/{incident_id}/analyze")
def analyze_incident(incident_id: int, llm=Depends(get_llm),
                     store=Depends(get_store),
                     sandbox=Depends(get_sandbox),
                     deliver_requested: bool = Query(False, alias="deliver"),
                     github=Depends(get_github)):
    try:
        incident = engine.get_incident(incident_id)
    except RedisStoreError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    state = run_agent(
        llm,
        store,
        trace=incident.sample_trace,
        service_name=incident.service_name,
        repo_root=REPO_ROOT,
        incident_id=incident.id,
        sandbox=sandbox,
    )
    response = summarize(state)
    if deliver_requested:
        incident_data = {
            "id": incident.id,
            "service_name": incident.service_name,
            "signature": state.get("signature", ""),
            "occurrences": incident.count,
        }
        webhook = os.getenv("SLACK_WEBHOOK_URL", "")
        slack_callback = None
        if webhook:
            slack_callback = lambda url: post_slack(
                webhook, build_slack_text(response, incident_data, url)
            )
        delivery = deliver(
            response,
            incident_data,
            github,
            path_prefix=os.getenv("TARGET_REPO_PATH_PREFIX", ""),
            slack=slack_callback,
        )
        response["delivery"] = asdict(delivery)
    return response
