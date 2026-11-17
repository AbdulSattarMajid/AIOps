"""Save the result of one analysis to PostgreSQL (permanent history).

The one public function is :func:`save_incident_history`.  It is built so that
it can never get in the way of the real pipeline:

* With ``DATABASE_URL`` unset it does nothing and returns ``False``.
* Any database problem is logged as one short warning (the error class name
  only, never the URL or a password) and the function returns ``False``.
* Everything for one analysis is written in a single transaction, so either
  all of it is saved or none of it is.
* Text is redacted again before it is stored.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, func
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from aiops.db.models import Analysis, Delivery, FixAttempt, Incident
from aiops.db.session import _coerce_url
from aiops.security.redaction import redact_text

logger = logging.getLogger("aiops.db")

_MAX_MESSAGE = 2000
_MAX_REASON = 1000
_MAX_DIFF = 20000
_MAX_SIGNALS = 20
_CONNECT_TIMEOUT_SECONDS = 3

# After a connection or missing-table failure, stop trying for a while so a
# database outage cannot slow the worker down on every single crash.
_PAUSE_AFTER_FAILURE_SECONDS = 30

_ENGINES: Dict[str, Engine] = {}
_paused_until = 0.0


def _reset_pause() -> None:
    """Forget a previous failure (used by tests)."""
    global _paused_until
    _paused_until = 0.0


def _is_paused() -> bool:
    return time.monotonic() < _paused_until


def _pause() -> None:
    global _paused_until
    _paused_until = time.monotonic() + _PAUSE_AFTER_FAILURE_SECONDS


def _should_pause(error: Exception) -> bool:
    """Pause for outages and missing tables, not for a one-off bad value."""
    if isinstance(error, (OperationalError, InterfaceError)):
        return True
    original = getattr(error, "orig", None)
    return isinstance(error, DBAPIError) and (
        type(original).__name__ == "UndefinedTable"
    )


def _default_engine() -> Optional[Engine]:
    """A cached engine for DATABASE_URL, or None when it is not set."""
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        return None
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    engine = _ENGINES.get(key)
    if engine is None:
        engine = create_engine(
            _coerce_url(raw),
            pool_pre_ping=True,
            connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS},
        )
        _ENGINES[key] = engine
    return engine


def _get(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _clean(value: Any, limit: int) -> Optional[str]:
    """Redact secrets, trim and cut to *limit* characters."""
    if value is None:
        return None
    text, _ = redact_text(value if isinstance(value, str) else str(value))
    return text[:limit]


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _message_from_trace(error_class: str, sample_trace: str) -> str:
    """The readable last line of the trace, for example 'KeyError: 5'."""
    lines = (sample_trace or "").rstrip().splitlines()
    if lines:
        last = lines[-1].strip()
        if error_class and last.startswith(f"{error_class}:"):
            return last
    return error_class or ""


def _signature_for(incident: Any, error_class: str, message: str) -> str:
    signature = _get(incident, "signature", "")
    if isinstance(signature, str) and signature:
        return signature[:500]
    basis = f"{_get(incident, 'service_name', '')}|{error_class}|{message}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _signals(classification: Any) -> Optional[List[str]]:
    raw = _get(classification, "signals")
    if not isinstance(raw, (list, tuple)):
        return None
    return [_clean(item, 200) or "" for item in list(raw)[:_MAX_SIGNALS]]


def _attempt_rows(
    summary: Dict, status: str, analysis_history: Optional[List[Dict]]
) -> List[Dict[str, Any]]:
    """One row per rejected attempt, plus one for the final accepted patch."""
    rows: List[Dict[str, Any]] = []
    history = analysis_history
    if history is None:
        history = summary.get("history") or []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage")
        attempt_no = _int_or_none(entry.get("attempt")) or 0
        if stage == "diff":
            validator_ok, verification_status = False, None
        elif stage == "tests":
            validator_ok, verification_status = True, "failed"
        else:
            validator_ok, verification_status = None, None
        rows.append({
            "attempt_no": attempt_no,
            "diff_text": _clean(entry.get("reply"), _MAX_DIFF) or None,
            "validator_ok": validator_ok,
            "verification_status": verification_status,
        })

    patch = summary.get("patch")
    if isinstance(patch, str) and patch and status in {"verified", "patch_ready"}:
        verification = summary.get("verification")
        if not isinstance(verification, dict):
            verification = {}
        rows.append({
            "attempt_no": _int_or_none(summary.get("attempts")) or len(rows) + 1,
            "diff_text": _clean(patch, _MAX_DIFF),
            "validator_ok": True,
            "verification_status": _clean(verification.get("status"), 100),
            "tests_before_passed": _int_or_none(
                summary.get("baseline_tests_passed")),
            "tests_before_failed": _int_or_none(
                summary.get("baseline_tests_failed")),
            "tests_after_passed": _int_or_none(verification.get("tests_passed")),
            "tests_after_failed": _int_or_none(verification.get("tests_failed")),
        })
    return rows


def _upsert_incident(session: Session, incident: Any, summary: Dict) -> int:
    """Insert the incident, or update the existing row with the same signature."""
    error_class = _clean(_get(incident, "error_class", ""), 300) or ""
    message = _clean(
        _message_from_trace(error_class, _get(incident, "sample_trace", "") or ""),
        _MAX_MESSAGE,
    ) or ""
    signature = _signature_for(incident, error_class, message)
    count = _int_or_none(_get(incident, "count", None)) or 1
    first_seen = _parse_time(_get(incident, "first_seen"))
    last_seen = _parse_time(_get(incident, "last_seen"))

    insert = pg_insert(Incident).values(
        signature=signature,
        service_name=_clean(_get(incident, "service_name", ""), 300),
        error_class=error_class,
        redacted_message=message,
        crash_file=_clean(summary.get("file"), 500) or None,
        crash_function=_clean(summary.get("function"), 300) or None,
        occurrences=count,
        first_seen=first_seen,
        last_seen=last_seen,
    )
    table = Incident.__table__
    return session.execute(
        insert.on_conflict_do_update(
            constraint="uq_incidents_signature",
            set_={
                "service_name": insert.excluded.service_name,
                "error_class": insert.excluded.error_class,
                "redacted_message": insert.excluded.redacted_message,
                "crash_file": func.coalesce(
                    insert.excluded.crash_file, table.c.crash_file),
                "crash_function": func.coalesce(
                    insert.excluded.crash_function, table.c.crash_function),
                # GREATEST / LEAST ignore NULL, so a missing time never wins.
                "occurrences": func.greatest(
                    table.c.occurrences, insert.excluded.occurrences),
                "first_seen": func.least(
                    table.c.first_seen, insert.excluded.first_seen),
                "last_seen": func.greatest(
                    table.c.last_seen, insert.excluded.last_seen),
            },
        ).returning(table.c.id)
    ).scalar_one()


def _write(
    session: Session,
    incident: Any,
    summary: Dict,
    status: str,
    reason: str,
    delivery: Optional[Dict],
    slack_sent: Optional[bool],
    analysis_history: Optional[List[Dict]],
    classifier_on: Optional[bool],
) -> None:
    incident_id = _upsert_incident(session, incident, summary)

    classification = summary.get("classification")
    analysis = Analysis(
        incident_id=incident_id,
        category=_clean(_get(classification, "category"), 100),
        confidence=_clean(_get(classification, "confidence"), 50),
        status=_clean(status, 100),
        reason=_clean(reason, _MAX_REASON),
        signals=_signals(classification),
        classifier_on=(
            classifier_on if classifier_on is not None
            else classification is not None
        ),
    )
    session.add(analysis)
    session.flush()

    for row in _attempt_rows(summary, status, analysis_history):
        session.add(FixAttempt(analysis_id=analysis.id, **row))

    pr_url = _get(delivery, "pr_url") or _get(delivery, "url")
    branch = _get(delivery, "branch")
    if pr_url or branch:
        session.add(Delivery(
            analysis_id=analysis.id,
            pr_url=_clean(pr_url, 1000) or None,
            branch=_clean(branch, 300) or None,
            slack_sent=slack_sent,
            outcome="open",
        ))


def _run_in_transaction(work, engine: Optional[Engine]) -> bool:
    """Run *work(session)* in one transaction.  Never raises."""
    try:
        if _is_paused():
            return False
        if engine is None:
            engine = _default_engine()
        if engine is None:
            return False
        with Session(engine) as session:
            with session.begin():
                work(session)
        return True
    except Exception as error:  # noqa: BLE001 - history must never stop the pipeline
        original = getattr(error, "orig", None)
        if type(original).__name__ == "UndefinedTable":
            logger.warning(
                "Postgres history not saved: tables are missing. "
                "Run 'alembic upgrade head'."
            )
        else:
            logger.warning(
                "Postgres history not saved (%s).", type(error).__name__
            )
        if _should_pause(error):
            _pause()
        return False


def save_incident_seen(
    incident: Any, *, engine: Optional[Engine] = None
) -> bool:
    """Record that an incident exists (and its latest count and time).

    Used for every crash event, so repeats raise the count even when no new
    analysis is run.  Never raises.
    """
    return _run_in_transaction(
        lambda session: _upsert_incident(session, incident, {}), engine
    )


def save_incident_history(
    incident: Any,
    summary: Optional[Dict],
    *,
    status: str,
    reason: str = "",
    delivery: Optional[Dict] = None,
    slack_sent: Optional[bool] = None,
    analysis_history: Optional[List[Dict]] = None,
    classifier_on: Optional[bool] = None,
    engine: Optional[Engine] = None,
) -> bool:
    """Save one analysis result.  Returns True if saved, False otherwise.

    Never raises.  *incident* may be the dedup ``Incident`` object or a dict.
    *summary* is the dict from ``summarize()`` (may be None or empty).
    Pass *engine* only in tests; normally it comes from ``DATABASE_URL``.
    """
    return _run_in_transaction(
        lambda session: _write(
            session, incident, summary or {}, str(status or ""),
            reason or "", delivery, slack_sent, analysis_history,
            classifier_on,
        ),
        engine,
    )
