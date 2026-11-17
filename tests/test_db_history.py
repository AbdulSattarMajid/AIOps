"""Tests for aiops.db.history (saving one analysis result to PostgreSQL).

The tests that need a real Postgres skip themselves when DATABASE_URL is not
set or the database is not reachable.  They work inside a throwaway schema
with a random name and never touch the real tables.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from aiops.db import history
from aiops.db.history import save_incident_history
from aiops.db.models import Base
from aiops.db.session import _coerce_url, get_engine
from aiops.dedup.engine import Incident

# A fake token built in pieces so no secret-looking text sits in the file.
FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _live_engine():
    if not os.environ.get("DATABASE_URL"):
        return None
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return engine
    except Exception:  # noqa: BLE001
        return None


def _incident(**overrides) -> Incident:
    values = dict(
        id=1,
        service_name="task-manager",
        error_class="AttributeError",
        signature="sig-abc123",
        first_seen="2026-11-17T07:00:00+00:00",
        last_seen="2026-11-17T07:05:00+00:00",
        count=3,
        sample_trace=(
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 10, in get\n'
            "AttributeError: 'NoneType' object has no attribute 'id'"
        ),
    )
    values.update(overrides)
    return Incident(**values)


def _verified_summary(**overrides) -> dict:
    summary = {
        "status": "verified",
        "reason": "",
        "attempts": 1,
        "function": "get_task",
        "file": "app/main.py",
        "patch": "--- a/app/main.py\n+++ b/app/main.py\n@@\n-x\n+y\n",
        "baseline_tests_passed": 5,
        "baseline_tests_failed": 2,
        "history": [],
        "verification": {
            "status": "fix_confirmed", "tests_passed": 7, "tests_failed": 0,
        },
        "classification": {
            "category": "code_bug", "confidence": "high",
            "reason": "AttributeError", "signals": ["AttributeError"],
            "suggested_action": "",
        },
    }
    summary.update(overrides)
    return summary


# ---------------------------------------------------------------------------
# No database needed
# ---------------------------------------------------------------------------

class TestWithoutDatabase:
    def test_returns_false_when_database_url_is_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert save_incident_history(
            _incident(), _verified_summary(), status="verified"
        ) is False

    def test_never_raises_when_the_database_is_unreachable(
        self, monkeypatch, caplog
    ) -> None:
        secret = "very-secret-pw-12345"
        monkeypatch.setenv(
            "DATABASE_URL", f"postgresql://someone:{secret}@127.0.0.1:1/nodb"
        )
        history._ENGINES.clear()
        with caplog.at_level(logging.WARNING, logger="aiops.db"):
            saved = save_incident_history(
                _incident(), _verified_summary(), status="verified"
            )
        assert saved is False
        assert "Postgres history not saved" in caplog.text
        assert secret not in caplog.text
        assert "127.0.0.1" not in caplog.text
        history._ENGINES.clear()

    def test_never_raises_on_odd_input(self, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert save_incident_history(None, None, status="") is False
        assert save_incident_history({}, {}, status="escalated") is False

    def test_helpers_are_forgiving(self) -> None:
        assert history._int_or_none("7") == 7
        assert history._int_or_none(None) is None
        assert history._int_or_none(True) is None
        assert history._parse_time("not a time") is None
        assert history._parse_time("2026-11-17T07:00:00Z").tzinfo is not None
        assert history._parse_time("2026-11-17T07:00:00").tzinfo is not None
        assert history._message_from_trace("KeyError", "x\nKeyError: 'a'") == (
            "KeyError: 'a'"
        )
        assert history._message_from_trace("KeyError", "") == "KeyError"

    def test_default_engine_is_cached_and_does_not_connect(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
        history._ENGINES.clear()
        first = history._default_engine()
        assert first is history._default_engine()
        assert first is not None
        history._ENGINES.clear()


# ---------------------------------------------------------------------------
# Real Postgres (skipped when it is not running)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    _live_engine() is None,
    reason="DATABASE_URL is not set or Postgres is not reachable",
)
class TestWithPostgres:
    @pytest.fixture()
    def pg(self):
        """An engine whose connections use a throwaway schema (search_path)."""
        base = _live_engine()
        schema = f"test_{uuid.uuid4().hex}"
        with base.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = sa.create_engine(
            _coerce_url(os.environ["DATABASE_URL"]),
            connect_args={"options": f"-c search_path={schema}"},
        )
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()
        with base.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()

    @staticmethod
    def _count(engine, table: str) -> int:
        with engine.connect() as conn:
            return conn.execute(
                sa.text(f"SELECT count(*) FROM {table}")
            ).scalar_one()

    @staticmethod
    def _rows(engine, query: str):
        with engine.connect() as conn:
            return conn.execute(sa.text(query)).mappings().all()

    def test_one_verified_fix_is_saved_everywhere(self, pg) -> None:
        saved = save_incident_history(
            _incident(), _verified_summary(), status="verified",
            delivery={"status": "opened", "pr_url": "https://example.test/pr/2",
                      "branch": "aiops/fix-1-abc"},
            slack_sent=True, engine=pg,
        )
        assert saved is True
        assert self._count(pg, "incidents") == 1
        assert self._count(pg, "analyses") == 1
        assert self._count(pg, "fix_attempts") == 1
        assert self._count(pg, "deliveries") == 1

        incident = self._rows(pg, "SELECT * FROM incidents")[0]
        assert incident["signature"] == "sig-abc123"
        assert incident["service_name"] == "task-manager"
        assert incident["redacted_message"].startswith("AttributeError:")
        assert incident["crash_file"] == "app/main.py"
        assert incident["crash_function"] == "get_task"
        assert incident["occurrences"] == 3

        analysis = self._rows(pg, "SELECT * FROM analyses")[0]
        assert analysis["category"] == "code_bug"
        assert analysis["confidence"] == "high"
        assert analysis["status"] == "verified"
        assert analysis["signals"] == ["AttributeError"]
        assert analysis["classifier_on"] is True

        attempt = self._rows(pg, "SELECT * FROM fix_attempts")[0]
        assert attempt["validator_ok"] is True
        assert attempt["verification_status"] == "fix_confirmed"
        assert (attempt["tests_before_passed"], attempt["tests_before_failed"]) == (5, 2)
        assert (attempt["tests_after_passed"], attempt["tests_after_failed"]) == (7, 0)

        delivery = self._rows(pg, "SELECT * FROM deliveries")[0]
        assert delivery["pr_url"] == "https://example.test/pr/2"
        assert delivery["branch"] == "aiops/fix-1-abc"
        assert delivery["slack_sent"] is True
        assert delivery["outcome"] == "open"

    def test_repeat_crash_keeps_one_incident_and_adds_an_analysis(self, pg) -> None:
        save_incident_history(
            _incident(count=3), _verified_summary(), status="verified", engine=pg
        )
        save_incident_history(
            _incident(count=9, last_seen="2026-11-17T09:00:00+00:00"),
            {"status": "escalated"}, status="escalated", reason="again",
            engine=pg,
        )
        assert self._count(pg, "incidents") == 1
        assert self._count(pg, "analyses") == 2
        row = self._rows(pg, "SELECT * FROM incidents")[0]
        assert row["occurrences"] == 9
        assert row["last_seen"] == datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
        assert row["first_seen"] == datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)
        # An empty crash location in the second save must not erase the first.
        assert row["crash_function"] == "get_task"

    def test_a_smaller_count_never_lowers_the_stored_count(self, pg) -> None:
        save_incident_history(
            _incident(count=9), _verified_summary(), status="verified", engine=pg
        )
        save_incident_history(
            _incident(count=1), _verified_summary(), status="verified", engine=pg
        )
        assert self._rows(pg, "SELECT occurrences FROM incidents")[0][
            "occurrences"] == 9

    def test_not_a_code_bug_is_saved_without_attempts_or_delivery(self, pg) -> None:
        summary = {
            "status": "escalated",
            "reason": "Not a code bug (config_issue): set SHOP_PAYMENT_KEY.",
            "classification": {
                "category": "config_issue", "confidence": "high",
                "reason": "missing env var", "signals": ["os.environ[...]"],
            },
        }
        assert save_incident_history(
            _incident(error_class="KeyError", signature="sig-cfg",
                      sample_trace="KeyError: 'SHOP_PAYMENT_KEY'"),
            summary, status="escalated", reason=summary["reason"], engine=pg,
        ) is True
        analysis = self._rows(pg, "SELECT * FROM analyses")[0]
        assert analysis["category"] == "config_issue"
        assert analysis["status"] == "escalated"
        assert self._count(pg, "fix_attempts") == 0
        assert self._count(pg, "deliveries") == 0

    def test_classifier_off_leaves_category_empty(self, pg) -> None:
        summary = _verified_summary()
        summary.pop("classification")
        save_incident_history(
            _incident(), summary, status="verified", engine=pg
        )
        analysis = self._rows(pg, "SELECT * FROM analyses")[0]
        assert analysis["category"] is None
        assert analysis["classifier_on"] is False

    def test_rejected_attempts_are_saved_with_their_diffs(self, pg) -> None:
        history_entries = [
            {"attempt": 1, "stage": "diff", "ok": False,
             "error": "bad hunk", "reply": "--- bad diff"},
            {"attempt": 2, "stage": "tests", "ok": False,
             "error": "tests failed", "reply": "--- diff two"},
        ]
        save_incident_history(
            _incident(), _verified_summary(attempts=3), status="verified",
            analysis_history=history_entries, engine=pg,
        )
        rows = self._rows(
            pg, "SELECT * FROM fix_attempts ORDER BY attempt_no")
        assert [r["attempt_no"] for r in rows] == [1, 2, 3]
        assert rows[0]["validator_ok"] is False
        assert rows[0]["diff_text"] == "--- bad diff"
        assert rows[1]["validator_ok"] is True
        assert rows[1]["verification_status"] == "failed"
        assert rows[2]["verification_status"] == "fix_confirmed"

    def test_secrets_are_never_stored(self, pg) -> None:
        summary = _verified_summary(
            patch=f"--- a/x\n+++ b/x\n@@\n-token = '{FAKE_TOKEN}'\n+ok\n"
        )
        incident = _incident(
            sample_trace=(
                "Traceback (most recent call last):\n"
                f"AttributeError: bad token {FAKE_TOKEN}"
            ),
        )
        save_incident_history(
            incident, summary, status="verified",
            reason=f"failed with {FAKE_TOKEN}", engine=pg,
            delivery={"pr_url": "https://example.test/pr/3", "branch": "b"},
        )
        for table in ("incidents", "analyses", "fix_attempts", "deliveries"):
            dump = str(self._rows(pg, f"SELECT * FROM {table}"))
            assert FAKE_TOKEN not in dump, table

    def test_missing_tables_give_a_clear_warning_and_no_crash(
        self, caplog
    ) -> None:
        base = _live_engine()
        schema = f"test_{uuid.uuid4().hex}"
        with base.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = sa.create_engine(
            _coerce_url(os.environ["DATABASE_URL"]),
            connect_args={"options": f"-c search_path={schema}"},
        )
        try:
            with caplog.at_level(logging.WARNING, logger="aiops.db"):
                saved = save_incident_history(
                    _incident(), _verified_summary(), status="verified",
                    engine=engine,
                )
            assert saved is False
            assert "alembic upgrade head" in caplog.text
        finally:
            engine.dispose()
            with base.begin() as conn:
                conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
            base.dispose()

    def test_a_failure_half_way_saves_nothing(self, pg, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(history, "_attempt_rows", boom)
        assert save_incident_history(
            _incident(), _verified_summary(), status="verified", engine=pg
        ) is False
        assert self._count(pg, "incidents") == 0
        assert self._count(pg, "analyses") == 0
