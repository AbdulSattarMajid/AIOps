import httpx

from aiops.security.redaction import redact_text


def build_slack_text(summary: dict, incident: dict, pr_url: str) -> str:
    verification = summary.get("verification") or {}
    if hasattr(verification, "status"):
        verification = {"status": verification.status}
    service = str(incident.get("service_name", "unknown")).replace("@", "@\u200b")
    signature = str(incident.get("signature", "unknown error"))
    error_type = (
        signature.splitlines()[0].split(":", 1)[0].strip()
        .replace("@", "@\u200b")[:100]
    )
    status = str(verification.get("status", "not run")).replace("@", "@\u200b")
    safe_url = str(pr_url).replace("@", "@\u200b")
    text, _ = redact_text(
        f"AIOps fix for {service} ({error_type}); "
        f"verification: {status}. {safe_url}"
    )
    return text


def post_slack(webhook_url: str, text: str, post=None,
               raise_errors: bool = False) -> bool | None:
    if not webhook_url:
        return None
    try:
        sender = post or httpx.post
        response = sender(
            webhook_url, json={"text": text}, timeout=10
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        return True
    except Exception:
        # Slack is a notification only; a webhook outage must not undo delivery.
        if raise_errors:
            return False
        return None
