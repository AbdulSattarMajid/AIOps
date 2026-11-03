import hashlib
import re
from typing import Dict, Tuple


_MENTION = re.compile(r"@(?!@)(?=[A-Za-z0-9_])")


def _safe_text(value: object, limit: int = 2000) -> str:
    text = ("" if value is None else str(value))[:limit]
    text = text.replace("```", "``\u200b`")
    return _MENTION.sub("@\u200b", text)


def _code(value: object, limit: int = 2000) -> str:
    return f"```\n{_safe_text(value, limit)}\n```"


def render_pr(summary: Dict, incident: Dict,
              unverified: bool = False) -> Tuple[str, str]:
    service = _safe_text(incident.get("service_name", "unknown"), 200)
    signature = " ".join(
        _safe_text(incident.get("signature", "unknown error"), 300).split()
    )
    title = f"AIOps fix: {service} - {signature}"
    if unverified:
        title = "[UNVERIFIED] " + title
    title = title[:100]

    verification = summary.get("verification") or {}
    if hasattr(verification, "__dict__"):
        verification = vars(verification)
    verification_status = verification.get("status", "not run")
    tests_passed = verification.get("tests_passed")
    tests_failed = verification.get("tests_failed")
    fixed = verification.get("fixed", [])
    newly_failing = verification.get("newly_failing", [])
    runbooks = summary.get("runbooks", [])
    if isinstance(runbooks, list):
        runbooks_text = ", ".join(str(item) for item in runbooks) or "(none)"
    else:
        runbooks_text = str(runbooks or "(none)")

    body = "\n\n".join([
        "## What happened\n"
        f"- Service: {_code(service, 200)}\n"
        f"- Error signature: {_code(signature, 300)}\n"
        f"- Occurrences: {_code(incident.get('occurrences', 'unknown'), 40)}",
        "## Where\n"
        f"- File: {_code(summary.get('file', ''), 300)}\n"
        f"- Function: {_code(summary.get('function', ''), 300)}",
        "## Proposed change\n```diff\n"
        f"{_safe_text(summary.get('patch', ''), 12000)}\n```",
        "## Test evidence\n"
        f"- Verification: {_code(verification_status, 100)}\n"
        f"- Tests passed: {_code(tests_passed, 40)}\n"
        f"- Tests failed: {_code(tests_failed, 40)}\n"
        f"- Tests fixed: {_code(', '.join(fixed), 1000)}\n"
        f"- Newly failing tests: {_code(', '.join(newly_failing), 1000)}",
        "## How it was produced\n"
        f"- Attempts: {_code(summary.get('attempts', ''), 40)}\n"
        f"- Model: {_code(summary.get('model', ''), 200)}\n"
        f"- Runbooks used: {_code(runbooks_text, 1000)}",
        "Generated automatically by AIOps. Review carefully. "
        "This PR is never auto-merged.",
    ])
    return title, body


def make_branch_name(incident_id: int, patch: str) -> str:
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:8]
    return f"aiops/fix-{incident_id}-{digest}"
