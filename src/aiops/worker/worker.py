"""Redis Streams consumer for queued telemetry events."""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict

import redis
from redis.exceptions import RedisError

from aiops.context.github_source import (
    GitHubSourceError,
    repository_source,
    uses_github_source,
)
from aiops.dedup.engine import DedupEngine
from aiops.ingest.schemas import LogEvent
from aiops.limits import LimitStoreError, RedisLimits
from aiops.storage.redis_store import (
    CONSUMER_GROUP,
    DEAD_LETTER_STREAM,
    QUEUE_STREAM,
    RedisStore,
    get_redis,
    save_analysis_detail_safely,
)

RECLAIM_IDLE_MS = int(os.getenv("STREAM_RECLAIM_IDLE_MS", "60000"))
MAX_DELIVERY_ATTEMPTS = int(os.getenv("STREAM_MAX_DELIVERY_ATTEMPTS", "5"))
DEFAULT_BATCH_SIZE = int(os.getenv("STREAM_BATCH_SIZE", "10"))
BLOCK_TIMEOUT_MS = int(os.getenv("STREAM_BLOCK_MS", "1000"))


def _ensure_group(client, stream_name: str, group_name: str) -> None:
    try:
        client.xgroup_create(
            stream_name, group_name, id="0", mkstream=True
        )
    except redis.ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise


def _delivery_counts(client, stream_name: str, group_name: str, message_ids):
    counts = {}
    for message_id in message_ids:
        pending = client.xpending_range(
            stream_name,
            group_name,
            min=message_id,
            max=message_id,
            count=1,
        )
        if pending:
            counts[message_id] = int(pending[0]["times_delivered"])
    return counts


def _dead_letter(
    client,
    stream_name: str,
    dead_letter_stream: str,
    group_name: str,
    message_id: str,
):
    pipe = client.pipeline(transaction=True)
    pipe.xadd(
        dead_letter_stream,
        {
            "source_stream": stream_name,
            "source_id": message_id,
            "reason": "Processing failed after 5 delivery attempts.",
        },
    )
    pipe.xack(stream_name, group_name, message_id)
    pipe.xdel(stream_name, message_id)
    pipe.execute()


def _ack_and_delete(client, stream_name: str, group_name: str, message_id: str):
    pipe = client.pipeline(transaction=True)
    pipe.xack(stream_name, group_name, message_id)
    pipe.xdel(stream_name, message_id)
    pipe.execute()


def _default_auto_analyzer(incident, limits):
    from aiops.api import main

    try:
        with repository_source(main.REPO_ROOT) as source:
            agent_options = {}
            if source.path_prefix is not None:
                agent_options["path_prefix"] = source.path_prefix
                sandbox = main.get_sandbox_for_repo(source.repo_root)
            else:
                sandbox = main.get_sandbox()
            state = main.run_agent(
                main.get_llm(),
                main.get_store(),
                trace=incident.sample_trace,
                service_name=incident.service_name,
                repo_root=source.repo_root,
                incident_id=incident.id,
                sandbox=sandbox,
                limits=limits,
                signature=incident.signature,
                **agent_options,
            )
        summary = main.summarize(state)
        if state.get("history") or state.get("last_reply"):
            summary["_analysis_history"] = state.get("history", [])
            summary["_analysis_last_reply"] = state.get("last_reply", "")
        return summary
    except GitHubSourceError as error:
        return {
            "status": "escalated",
            "reason": str(error),
            "signature": incident.signature,
        }


def _default_auto_deliverer(summary, incident):
    from aiops.delivery.github_pr import PyGithubClient, deliver

    token = os.getenv("GITHUB_TOKEN", "")
    repository = os.getenv("GITHUB_REPO", "")
    if not token or not repository:
        return {"status": "refused", "url": None}
    client = PyGithubClient(token, repository)
    result = deliver(
        summary,
        incident,
        client,
        path_prefix=(
            "" if uses_github_source()
            else os.getenv("TARGET_REPO_PATH_PREFIX", "")
        ),
    )
    return asdict(result)


def _pipeline_slack_text(incident, summary, pr_url):
    from aiops.security.redaction import redact_text

    patch_lines = [
        line[1:].strip()
        for line in str(summary.get("patch", "")).splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    diff_summary = " ".join(patch_lines)[:120] or "no patch"
    verification = summary.get("verification") or {}
    if hasattr(verification, "tests_passed"):
        tests_after = (
            verification.tests_passed,
            verification.tests_failed,
        )
    elif isinstance(verification, dict):
        tests_after = (
            verification.get("tests_passed"),
            verification.get("tests_failed"),
        )
    else:
        tests_after = (None, None)
    tests_before = (
        summary.get("baseline_tests_passed"),
        summary.get("baseline_tests_failed"),
    )
    destination = pr_url or "needs a human"
    text = (
        f"Incident {incident.get('id')} {incident.get('service_name', 'unknown')}; "
        f"{incident.get('error_class', 'unknown error')}; {diff_summary}; "
        f"tests {tests_before[0]}/{tests_before[1]} -> "
        f"{tests_after[0]}/{tests_after[1]}; {destination}"
    )
    safe_text, _ = redact_text(text)
    return safe_text.replace("@", "@\u200b")[:1000]


def _incident_display_signature(incident):
    traceback_lines = incident.sample_trace.rstrip().splitlines()
    if traceback_lines:
        exception_line = traceback_lines[-1].strip()
        if exception_line.startswith(f"{incident.error_class}:"):
            return exception_line
    return incident.error_class


def _record_auto_result(processor, incident, status, reason):
    try:
        processor.store.record_analysis_result(incident.id, status, reason)
    except Exception:
        pass


def _maybe_auto_analyze(
    processor,
    incident,
    client,
    auto_analyzer,
    auto_deliverer=None,
    slack_sender=None,
):
    if os.getenv("AIOPS_AUTO_ANALYZE", "off").lower() != "on":
        return
    limits = RedisLimits(client=client)
    slot = None
    try:
        try:
            slot = limits.acquire_slot("analyses")
        except LimitStoreError:
            _record_auto_result(
                processor, incident, "skipped",
                "Automatic analysis skipped; analysis capacity is unavailable.",
            )
            return
        if slot is None:
            _record_auto_result(
                processor, incident, "skipped",
                "Automatic analysis skipped; analysis capacity is full.",
            )
            return
        try:
            cooldown_claimed = limits.claim_cooldown(incident.signature)
        except LimitStoreError:
            _record_auto_result(
                processor, incident, "skipped",
                "Automatic analysis skipped; cooldown could not be checked.",
            )
            return
        if not cooldown_claimed:
            limits.record_cooldown_skip()
            _record_auto_result(
                processor, incident, "skipped",
                "Automatic analysis skipped; signature cooldown is active.",
            )
            return
        try:
            budget_reason = limits.check_token_budget(incident.id)
        except LimitStoreError:
            _record_auto_result(
                processor, incident, "skipped",
                "Automatic analysis skipped; token budget could not be checked.",
            )
            return
        if budget_reason:
            _record_auto_result(processor, incident, "skipped", budget_reason)
            return
        limits.record_auto_analysis()
        analysis_failed = False
        try:
            result = auto_analyzer(incident, limits)
            status = str(result.get("status", "escalated"))
        except Exception:
            result = {}
            status = "escalated"
            analysis_failed = True

        analysis_history = result.pop("_analysis_history", None)
        analysis_last_reply = result.pop("_analysis_last_reply", "")
        reason = ""
        if analysis_failed:
            reason = "Automatic analysis failed; inspect service health."
        elif result.get("reason") in {
            "incident token budget reached",
            "daily token budget reached",
        }:
            reason = result["reason"]
        elif status == "escalated":
            reason = result.get("reason") or "Automatic analysis was escalated."

        delivery = {}
        deliver_enabled = os.getenv(
            "AIOPS_AUTO_DELIVER_PR", os.getenv("AUTO_DELIVER_PR", "off")
        ).lower() == "on"
        incident_data = {
            "id": incident.id,
            "service_name": incident.service_name,
            "error_class": incident.error_class,
            "signature": _incident_display_signature(incident),
            "occurrences": incident.count,
        }
        if deliver_enabled and status == "verified":
            try:
                delivery = (auto_deliverer or _default_auto_deliverer)(
                    result, incident_data
                )
                if delivery.get("status") not in {"opened", "already_open"}:
                    reason = "Automatic PR delivery was refused."
            except Exception:
                delivery = {}
                reason = "Automatic PR delivery failed; inspect service health."

        has_summary = "attempts" in result
        detail_summary = dict(result) if has_summary else None
        if detail_summary is not None:
            detail_summary["status"] = status
            detail_summary["reason"] = reason or detail_summary.get("reason", "")
        save_analysis_detail_safely(
            processor.store,
            incident.id,
            detail_summary,
            history=analysis_history,
            last_reply=analysis_last_reply,
            status=status,
            reason=reason,
        )
        _record_auto_result(processor, incident, status, reason)
        webhook = os.getenv("SLACK_WEBHOOK_URL", "")
        if webhook:
            pr_url = (
                delivery.get("pr_url") or delivery.get("url")
                if delivery else None
            )
            message = _pipeline_slack_text(incident_data, result, pr_url)
            try:
                from aiops.delivery.slack import post_slack

                if slack_sender is not None:
                    slack_sender(message)
                elif not post_slack(webhook, message, raise_errors=True):
                    _record_auto_result(
                        processor, incident, status,
                        "Slack notification failed; inspect service health.",
                    )
            except Exception:
                _record_auto_result(
                    processor, incident, status,
                    "Slack notification failed; inspect service health.",
                )
    except Exception:
        _record_auto_result(
            processor, incident, "escalated",
            "Automatic analysis failed; inspect service health.",
        )
    finally:
        if slot is not None:
            try:
                limits.release_slot("analyses", slot)
            except LimitStoreError:
                pass


def drain_once(
    client,
    max_messages: int,
    *,
    engine: DedupEngine | None = None,
    consumer_name: str | None = None,
    stream_name: str = QUEUE_STREAM,
    group_name: str = CONSUMER_GROUP,
    dead_letter_stream: str = DEAD_LETTER_STREAM,
    min_idle_ms: int = RECLAIM_IDLE_MS,
    block_ms: int = 1,
    max_delivery_attempts: int = MAX_DELIVERY_ATTEMPTS,
    auto_analyzer=None,
    auto_deliverer=None,
    slack_sender=None,
) -> int:
    """Claim stale pending messages, then consume new messages and acknowledge successes."""
    if max_messages < 1:
        return 0
    _ensure_group(client, stream_name, group_name)
    consumer = consumer_name or (
        f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex}"
    )
    processor = engine or DedupEngine(store=RedisStore(client=client))

    claimed = client.xautoclaim(
        stream_name,
        group_name,
        consumer,
        min_idle_time=min_idle_ms,
        start_id="0-0",
        count=max_messages,
    )
    messages = list(claimed[1])
    if len(messages) < max_messages:
        remaining = max_messages - len(messages)
        options = {"count": remaining}
        if block_ms > 0:
            options["block"] = block_ms
        read = client.xreadgroup(
            group_name, consumer, {stream_name: ">"}, **options
        )
        for _stream, items in read:
            messages.extend(items)

    if not messages:
        return 0

    counts = _delivery_counts(
        client,
        stream_name,
        group_name,
        {message_id for message_id, _ in messages},
    )
    handled = 0
    for message_id, fields in messages:
        attempts = counts.get(message_id, 1)
        if attempts >= max_delivery_attempts:
            _dead_letter(
                client, stream_name, dead_letter_stream, group_name, message_id
            )
            handled += 1
            continue
        try:
            payload = json.loads(fields["event"])
            event = LogEvent.model_validate(payload)
            incident, is_new = processor.process(event, event_id=message_id)
        except Exception:
            continue
        if is_new:
            _maybe_auto_analyze(
                processor,
                incident,
                client,
                auto_analyzer or _default_auto_analyzer,
                auto_deliverer=auto_deliverer,
                slack_sender=slack_sender,
            )
        _ack_and_delete(client, stream_name, group_name, message_id)
        handled += 1
    return handled


def run_worker() -> None:
    client = get_redis()
    consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex}"
    engine = DedupEngine(store=RedisStore(client=client))
    try:
        _ensure_group(client, QUEUE_STREAM, CONSUMER_GROUP)
        while True:
            drain_once(
                client,
                DEFAULT_BATCH_SIZE,
                engine=engine,
                consumer_name=consumer,
                block_ms=BLOCK_TIMEOUT_MS,
            )
    except KeyboardInterrupt:
        return
    except RedisError:
        raise


if __name__ == "__main__":
    run_worker()
