"""Redact secrets and personal data before they reach storage, models, or humans.

Findings never include matched values — only {type: count}.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional, Tuple

from aiops.ingest.schemas import LogEvent

Findings = Dict[str, int]

_PLACEHOLDER = "<REDACTED_{}>"
_ALREADY_REDACTED = re.compile(r"^<REDACTED_[A-Z0-9_]+>$")

# Bounded character classes: no nested unbounded quantifiers.
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"
    r"[A-Za-z0-9+/=\s]{0,20000}"
    r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----"
)
_DB_URL = re.compile(
    r"(?i)\b((?:jdbc:)?(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?"
    r"|redis|amqp|mssql|sqlserver|cockroachdb))"
    r"(://)([^/\s:@]{1,128}):([^/\s@]{1,256})@"
)
_URL_CREDS = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]{0,32})(://)([^/\s:@]{1,128}):([^/\s@]{1,256})@"
)
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\b"
)
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GROQ = re.compile(r"\bgsk_[A-Za-z0-9]{10,200}\b")
_GITHUB_PAT = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,255}\b")
_GITHUB_PAT_CLASSIC = re.compile(r"\bghp_[A-Za-z0-9]{10,200}\b")
_SLACK = re.compile(r"\bxoxb-[A-Za-z0-9-]{10,200}\b")
_STRIPE = re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{10,200}\b")
_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:(?:basic|bearer)\s+)?)"
    r"(?!<REDACTED_)(\S{8,4096})"
)
_BEARER = re.compile(
    r"(?i)(\bbearer\s+)(?!<REDACTED_)([A-Za-z0-9._\-+=/]{8,4096})"
)
_KV = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api_key|apikey|authorization|bearer)"
    r"(\s*[=:]\s*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>(?:(?!(?P=quote)).){1,512})(?P=quote)"
    r"|(?P<unquoted>[^\s,;]{1,512}))"
)
_SOURCE_KV = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api_key|apikey)"
    r"(\s*[=:]\s*)(?P<quote>[\"'])(?P<quoted>(?:(?!(?P=quote)).){1,512})(?P=quote)"
)
_EMAIL = re.compile(
    r"(?i)\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}\b"
)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"
)

_CORE_LOG_FIELDS = {"timestamp", "service_name", "error_class"}


def merge_findings(*groups: Findings) -> Findings:
    merged: Findings = {}
    for group in groups:
        for kind, count in group.items():
            merged[kind] = merged.get(kind, 0) + count
    return merged


def _placeholder(kind: str) -> str:
    return _PLACEHOLDER.format(kind.upper())


def _bump(findings: Findings, kind: str) -> str:
    findings[kind] = findings.get(kind, 0) + 1
    return _placeholder(kind)


def _sub_fixed(pattern: re.Pattern, text: str, findings: Findings, kind: str) -> str:
    def repl(_match: re.Match) -> str:
        return _bump(findings, kind)

    return pattern.sub(repl, text)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = ord(char) - 48
        if index % 2:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _redact_kv(pattern: re.Pattern, text: str, findings: Findings) -> str:
    def repl(match: re.Match) -> str:
        groups = match.groupdict()
        value = groups.get("quoted")
        if value is None:
            value = groups.get("unquoted") or ""
        if _ALREADY_REDACTED.match(value) or value.lower() == "bearer":
            return match.group(0)
        quote = match.group("quote") or ""
        token = _bump(findings, "secret")
        return f"{match.group(1)}{match.group(2)}{quote}{token}{quote}"

    return pattern.sub(repl, text)


def _redact_cards(text: str, findings: Findings) -> str:
    def repl(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if not 13 <= len(digits) <= 19 or not _luhn_ok(digits):
            return match.group(0)
        return _bump(findings, "credit_card")

    return _CARD.sub(repl, text)


def _redact_pipeline(text: str, generic_kv: bool) -> Tuple[str, Findings]:
    findings: Findings = {}
    text = _sub_fixed(_PEM, text, findings, "private_key")
    text = _DB_URL.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_bump(findings, 'connection_string')}@",
        text,
    )
    text = _URL_CREDS.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_bump(findings, 'url_credentials')}@",
        text,
    )
    text = _sub_fixed(_JWT, text, findings, "jwt")
    text = _sub_fixed(_AWS, text, findings, "aws_key")
    text = _sub_fixed(_GROQ, text, findings, "groq_key")
    text = _sub_fixed(_GITHUB_PAT, text, findings, "github_token")
    text = _sub_fixed(_GITHUB_PAT_CLASSIC, text, findings, "github_token")
    text = _sub_fixed(_SLACK, text, findings, "slack_token")
    text = _sub_fixed(_STRIPE, text, findings, "stripe_key")
    text = _AUTHORIZATION.sub(
        lambda m: m.group(1) + _bump(findings, "authorization"),
        text,
    )
    text = _BEARER.sub(
        lambda m: m.group(1) + _bump(findings, "authorization"),
        text,
    )
    if generic_kv:
        text = _redact_kv(_KV, text, findings)
    else:
        text = _redact_kv(_SOURCE_KV, text, findings)
    text = _sub_fixed(_EMAIL, text, findings, "email")
    text = _redact_cards(text, findings)
    text = _sub_fixed(_PHONE, text, findings, "phone")
    return text, findings


def redact_text(text: Optional[Any]) -> Tuple[str, Findings]:
    """Return (clean_text, findings). findings is {type: count} only."""
    if text is None:
        return "", {}
    if not isinstance(text, str):
        text = str(text)
    if text == "":
        return "", {}
    return _redact_pipeline(text, generic_kv=True)


def source_secret_findings(text: Optional[Any]) -> Findings:
    """High-confidence scan for secrets in source. Does not rewrite the file."""
    if not text:
        return {}
    if not isinstance(text, str):
        text = str(text)
    _, findings = _redact_pipeline(text, generic_kv=False)
    return findings


def redact_log_event(event: LogEvent) -> Tuple[LogEvent, Findings]:
    payload = event.model_dump()
    findings: Findings = {}
    for key, value in list(payload.items()):
        if key in _CORE_LOG_FIELDS or not isinstance(value, str):
            continue
        payload[key], found = redact_text(value)
        findings = merge_findings(findings, found)
    return LogEvent(**payload), findings


def redact_strings(values: Iterable[Optional[Any]]) -> Tuple[list, Findings]:
    cleaned = []
    findings: Findings = {}
    for value in values:
        text, found = redact_text(value)
        cleaned.append(text)
        findings = merge_findings(findings, found)
    return cleaned, findings
