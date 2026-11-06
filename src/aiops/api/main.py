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
from aiops.context.github_source import GitHubSourceError, repository_source
from aiops.dedup.engine import DedupEngine
from aiops.delivery.github_pr import DeliveryError, PyGithubClient, deliver
from aiops.delivery.slack import build_slack_text, post_slack
from aiops.ingest.schemas import LogEvent
from aiops.limits import LimitStoreError, RedisLimits
from aiops.rag.store import RunbookStore
from aiops.sandbox.app_sandbox import AppSandbox
from aiops.sandbox.runner import is_docker_available
from aiops.security.auth import get_configured_hashes, redact_headers, verify_api_key
from aiops.security.ratelimit import (
    RateLimiter,
    default_limiter,
    get_rate_limit_config,
)
from aiops.security.redaction import redact_log_event
from aiops.storage import redis_store
from aiops.storage.redis_store import (
    DEAD_LETTER_STREAM,
    QUEUE_STREAM,
    RedisStoreError,
    enqueue_event,
    queue_stats,
)

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
    return get_sandbox_for_repo(REPO_ROOT)


def get_sandbox_for_repo(repo_root):
    if os.getenv("AIOPS_SANDBOX", "on") != "off" and is_docker_available():
        return AppSandbox(repo_root)
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


def get_limits():
    try:
        return RedisLimits(client=redis_store.get_redis())
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Redis storm-protection limits are unavailable.",
            headers={"Retry-After": "5"},
        ) from None


def acquire_analysis_slot(limits: RedisLimits = Depends(get_limits)):
    try:
        slot = limits.acquire_slot("analyses")
    except LimitStoreError:
        raise HTTPException(
            status_code=503,
            detail="Redis analysis capacity is unavailable.",
            headers={"Retry-After": "5"},
        ) from None
    if slot is None:
        raise HTTPException(
            status_code=429,
            detail="Analysis capacity is full; retry later.",
            headers={"Retry-After": "5"},
        )
    try:
        yield limits, slot
    finally:
        try:
            limits.release_slot("analyses", slot)
        except LimitStoreError:
            pass


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/telemetry/logs")
def ingest_log(event: LogEvent):
    if os.getenv("AIOPS_INGEST_MODE", "queue").lower() != "inline":
        redacted_event, _ = redact_log_event(event)
        try:
            event_id = enqueue_event(
                redacted_event.model_dump(mode="json"),
                stream=QUEUE_STREAM,
            )
        except RedisStoreError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
                headers={"Retry-After": "5"},
            ) from error
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "event_id": event_id},
        )

    # Inline mode is for tests and local development; production defaults to queue.
    try:
        incident, is_new = engine.process(event)
    except RedisStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=str(error),
            headers={"Retry-After": "5"},
        ) from error
    return {
        "incident_id": incident.id,
        "is_new": is_new,
        "occurrence_count": incident.count,
    }


@app.get("/api/v1/queue/stats")
def get_queue_stats():
    try:
        return queue_stats(
            stream=QUEUE_STREAM,
            dead_letter_stream=DEAD_LETTER_STREAM,
        )
    except RedisStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=str(error),
            headers={"Retry-After": "5"},
        ) from error


@app.get("/api/v1/incidents")
def list_incidents():
    try:
        return [asdict(i) for i in engine.incidents.values()]
    except RedisStoreError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/v1/incidents/{incident_id}/analyze")
def analyze_incident(incident_id: int,
                     slot_info=Depends(acquire_analysis_slot),
                     llm=Depends(get_llm),
                     store=Depends(get_store),
                     sandbox=Depends(get_sandbox),
                     deliver_requested: bool = Query(False, alias="deliver"),
                     github=Depends(get_github)):
    limits, _slot = slot_info
    try:
        incident = engine.get_incident(incident_id)
    except RedisStoreError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    try:
        with repository_source(REPO_ROOT) as source:
            agent_options = {}
            analysis_sandbox = sandbox
            if source.path_prefix is not None:
                agent_options["path_prefix"] = source.path_prefix
                analysis_sandbox = get_sandbox_for_repo(source.repo_root)
            state = run_agent(
                llm,
                store,
                trace=incident.sample_trace,
                service_name=incident.service_name,
                repo_root=source.repo_root,
                incident_id=incident.id,
                sandbox=analysis_sandbox,
                limits=limits,
                signature=incident.signature,
                **agent_options,
            )
    except GitHubSourceError as error:
        return {
            "status": "escalated",
            "reason": str(error),
            "signature": incident.signature,
        }
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
            path_prefix=(
                "" if source.path_prefix is not None
                else os.getenv("TARGET_REPO_PATH_PREFIX", "")
            ),
            slack=slack_callback,
        )
        response["delivery"] = asdict(delivery)
    return response
