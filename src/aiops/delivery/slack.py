import httpx


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
    return (
        f"AIOps fix for {service} ({error_type}); "
        f"verification: {status}. {safe_url}"
    )


def post_slack(webhook_url: str, text: str, post=None) -> None:
    if not webhook_url:
        return
    try:
        sender = post or httpx.post
        response = sender(
            webhook_url, json={"text": text}, timeout=10
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
    except Exception:
        # Slack is a notification only; a webhook outage must not undo delivery.
        return
