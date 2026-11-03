import hashlib
import re
from pathlib import PurePosixPath
from typing import Dict, Tuple


_MENTION = re.compile(r"@(?!@)(?=[A-Za-z0-9_])")
_BACKTICK_RUN = re.compile(r"`+")


def _safe_text(value: object, limit: int = 2000) -> str:
    text = ("" if value is None else str(value))[:limit]
    text = text.replace("```", "``\u200b`")
    return _MENTION.sub("@\u200b", text)


def _single_line(value: object, limit: int) -> str:
    return (
        _safe_text(value, limit)
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _inline(value: object, limit: int = 2000) -> str:
    text = _single_line(value, limit)
    longest_run = max(
        (len(match.group()) for match in _BACKTICK_RUN.finditer(text)),
        default=0,
    )
    delimiter = "`" * (longest_run + 1)
    if text and (
        text.startswith((" ", "`")) or text.endswith((" ", "`"))
    ):
        text = f" {text} "
    return f"{delimiter}{text}{delimiter}"


def _items(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _list_text(value: object, limit: int = 1000) -> str:
    items = _items(value)
    if not items:
        return " none"

    lines = []
    remaining = limit
    for item in items:
        if remaining <= 0:
            break
        text = _single_line(item, remaining)
        lines.append(f"  - {_inline(item, remaining)}")
        remaining -= len(text) + 10
    return "\n" + "\n".join(lines) if lines else " none"


def _unrelated_failures(summary: Dict, verification: Dict) -> list:
    baseline = set(_items(summary.get("baseline_failed_tests")))
    after = set(_items(summary.get("after_failed_tests")))
    if not after:
        after = (
            baseline - set(_items(verification.get("fixed")))
        ) | set(_items(verification.get("newly_failing")))

    file_path = str(summary.get("file", "")).replace("\\", "/")
    target_file = f"test_{PurePosixPath(file_path).stem}.py"
    return sorted(
        test for test in baseline & after
        if PurePosixPath(str(test).split("::", 1)[0]).name != target_file
    )


def render_pr(summary: Dict, incident: Dict,
              unverified: bool = False) -> Tuple[str, str]:
    service = _single_line(incident.get("service_name", "unknown"), 200)
    signature = _single_line(
        incident.get("signature", "unknown error"), 300
    )
    title = f"AIOps fix: {service} - {signature}"
    if unverified:
        title = "[UNVERIFIED] " + title
    title = title[:100]

    verification = summary.get("verification") or {}
    if hasattr(verification, "__dict__"):
        verification = vars(verification)
    verification_status = verification.get("status", "not run")
    baseline_passed = summary.get("baseline_tests_passed")
    baseline_failed_count = summary.get("baseline_tests_failed")
    tests_passed = verification.get("tests_passed")
    tests_failed = verification.get("tests_failed")
    unrelated = _unrelated_failures(summary, verification)

    body = "\n\n".join([
        "## What happened\n"
        f"- Service: {_inline(service, 200)}\n"
        f"- Error signature: {_inline(signature, 300)}\n"
        f"- Occurrences: {_inline(incident.get('occurrences', 'unknown'), 40)}",
        "## Where\n"
        f"- File: {_inline(summary.get('file', ''), 300)}\n"
        f"- Function: {_inline(summary.get('function', ''), 300)}",
        "## Proposed change\n```diff\n"
        f"{_safe_text(summary.get('patch', ''), 12000)}\n```",
        "## Test evidence\n"
        f"- Verification: {_inline(verification_status, 100)}\n"
        f"- Before the change: {_inline(baseline_passed if baseline_passed is not None else 'unknown', 40)} passed, "
        f"{_inline(baseline_failed_count if baseline_failed_count is not None else 'unknown', 40)} failed\n"
        f"- After the change: {_inline(tests_passed if tests_passed is not None else 'unknown', 40)} passed, "
        f"{_inline(tests_failed if tests_failed is not None else 'unknown', 40)} failed\n"
        f"- Fixed by this change:{_list_text(verification.get('fixed'))}\n"
        f"- Newly failing:{_list_text(verification.get('newly_failing'))}\n"
        "- Still failing, also failing before this change "
        f"(unrelated to this fix):{_list_text(unrelated)}",
        "## How it was produced\n"
        f"- Attempts: {_inline(summary.get('attempts', ''), 40)}\n"
        f"- Model: {_inline(summary.get('model', ''), 200)}\n"
        f"- Runbooks consulted:{_list_text(summary.get('runbooks'))}",
        "Generated automatically by AIOps. Review carefully. "
        "This PR is never auto-merged.",
    ])
    return title, body


def make_branch_name(incident_id: int, patch: str) -> str:
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:8]
    return f"aiops/fix-{incident_id}-{digest}"
