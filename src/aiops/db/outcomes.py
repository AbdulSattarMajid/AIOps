"""Record what a human decided about a draft PR (merged, closed, ignored).

This only writes down a decision.  Nothing here merges, closes or changes a
pull request on GitHub.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from aiops.db import history
from aiops.db.models import Delivery

VALID_OUTCOMES = ("open", "merged", "closed", "ignored")


class HistoryUnavailable(Exception):
    """The history database is not configured or cannot be reached."""


class DeliveryNotFound(Exception):
    """No saved delivery has the given PR link."""


def _normalise_url(pr_url: str) -> str:
    return (pr_url or "").strip().rstrip("/")


def record_outcome(
    pr_url: str, outcome: str, *, engine: Optional[Engine] = None
) -> Dict[str, Any]:
    """Set the outcome of every saved delivery that has this PR link.

    * ``open`` clears the decision time (a decision can be undone).
    * Any other outcome stores the current time as the decision time.

    Raises ValueError for an unknown outcome, DeliveryNotFound when the link
    is not saved, and HistoryUnavailable when the database cannot be used.
    The exception text never contains the database address or a password.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            "outcome must be one of: " + ", ".join(VALID_OUTCOMES)
        )
    url = _normalise_url(pr_url)
    if not url:
        raise ValueError("pr_url is required")

    try:
        if engine is None:
            engine = history._default_engine()
        if engine is None:
            raise HistoryUnavailable("DATABASE_URL is not set.")
        decided_at = (
            None if outcome == "open" else datetime.now(timezone.utc)
        )
        with Session(engine) as session:
            with session.begin():
                result = session.execute(
                    update(Delivery)
                    .where(Delivery.pr_url.in_([url, url + "/"]))
                    .values(outcome=outcome, decided_at=decided_at)
                )
                updated = result.rowcount or 0
    except HistoryUnavailable:
        raise
    except Exception as error:  # noqa: BLE001
        raise HistoryUnavailable(
            f"History database error ({type(error).__name__})."
        ) from None

    if updated == 0:
        raise DeliveryNotFound("No saved delivery has that PR link.")
    return {
        "pr_url": url,
        "outcome": outcome,
        "decided_at": decided_at.isoformat() if decided_at else None,
        "updated": updated,
    }
