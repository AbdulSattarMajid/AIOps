"""Tests for aiops.db.models and aiops.db.session.

Two categories:
1. No-database tests  – verify the ORM metadata without touching Postgres.
2. Live-database test – skipped when Postgres is not reachable; uses an
   isolated throwaway schema so it never touches real tables.
"""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from aiops.db.models import Analysis, Base, Delivery, FixAttempt, Incident
from aiops.db.session import get_engine, get_session_factory, _coerce_url


# ===========================================================================
# No-database: metadata & schema tests
# ===========================================================================

class TestMetadata:
    """The ORM metadata must declare exactly the four expected tables."""

    EXPECTED_TABLES = {"incidents", "analyses", "fix_attempts", "deliveries"}

    def test_exactly_four_tables(self) -> None:
        assert set(Base.metadata.tables.keys()) == self.EXPECTED_TABLES

    def test_incidents_columns(self) -> None:
        cols = {c.name for c in Base.metadata.tables["incidents"].columns}
        assert cols >= {
            "id", "signature", "service_name", "error_class",
            "redacted_message", "crash_file", "crash_function",
            "occurrences", "first_seen", "last_seen", "created_at",
        }

    def test_analyses_columns(self) -> None:
        cols = {c.name for c in Base.metadata.tables["analyses"].columns}
        assert cols >= {
            "id", "incident_id", "category", "confidence", "status",
            "reason", "signals", "classifier_on", "created_at",
        }

    def test_fix_attempts_columns(self) -> None:
        cols = {c.name for c in Base.metadata.tables["fix_attempts"].columns}
        assert cols >= {
            "id", "analysis_id", "attempt_no", "diff_text", "validator_ok",
            "verification_status", "tests_before_passed", "tests_before_failed",
            "tests_after_passed", "tests_after_failed", "created_at",
        }

    def test_deliveries_columns(self) -> None:
        cols = {c.name for c in Base.metadata.tables["deliveries"].columns}
        assert cols >= {
            "id", "analysis_id", "pr_url", "branch", "slack_sent",
            "outcome", "decided_at", "created_at",
        }

    def test_incidents_signature_unique_constraint(self) -> None:
        table = Base.metadata.tables["incidents"]
        unique_cols = []
        for constraint in table.constraints:
            if isinstance(constraint, sa.UniqueConstraint):
                unique_cols.extend(c.name for c in constraint.columns)
        assert "signature" in unique_cols

    def test_deliveries_outcome_check_constraint_exists(self) -> None:
        table = Base.metadata.tables["deliveries"]
        check_constraints = [
            c for c in table.constraints
            if isinstance(c, sa.CheckConstraint)
        ]
        assert check_constraints, "deliveries must have at least one CheckConstraint"
        # At least one check constraint must mention the allowed values.
        texts = [str(c.sqltext) for c in check_constraints]
        assert any("merged" in t or "open" in t for t in texts), (
            f"No outcome check constraint found; constraints: {texts}"
        )

    def test_analyses_has_category_index(self) -> None:
        table = Base.metadata.tables["analyses"]
        index_cols = []
        for idx in table.indexes:
            index_cols.extend(c.name for c in idx.columns)
        assert "category" in index_cols

    def test_fk_analyses_to_incidents(self) -> None:
        table = Base.metadata.tables["analyses"]
        fk_targets = {fk.target_fullname for fk in table.foreign_keys}
        assert "incidents.id" in fk_targets

    def test_fk_fix_attempts_to_analyses(self) -> None:
        table = Base.metadata.tables["fix_attempts"]
        fk_targets = {fk.target_fullname for fk in table.foreign_keys}
        assert "analyses.id" in fk_targets

    def test_fk_deliveries_to_analyses(self) -> None:
        table = Base.metadata.tables["deliveries"]
        fk_targets = {fk.target_fullname for fk in table.foreign_keys}
        assert "analyses.id" in fk_targets


# ===========================================================================
# No-database: session helper
# ===========================================================================

class TestSessionHelper:
    def test_get_engine_returns_none_when_database_url_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert get_engine() is None

    def test_get_engine_does_not_raise_when_database_url_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        try:
            engine = get_engine()
        except Exception as exc:
            pytest.fail(f"get_engine() raised unexpectedly: {exc}")
        assert engine is None

    def test_get_session_factory_returns_none_for_no_engine(self) -> None:
        assert get_session_factory(None) is None

    def test_coerce_url_postgresql(self) -> None:
        url = _coerce_url("postgresql://user:pass@host/db")
        assert url.startswith("postgresql+psycopg://")
        assert "pass" in url  # coercion preserves content, just changes dialect

    def test_coerce_url_postgres_alias(self) -> None:
        url = _coerce_url("postgres://user:pass@host/db")
        assert url.startswith("postgresql+psycopg://")

    def test_coerce_url_already_psycopg(self) -> None:
        url = _coerce_url("postgresql+psycopg://user:pass@host/db")
        assert url == "postgresql+psycopg://user:pass@host/db"


# ===========================================================================
# Live-database test (skipped when Postgres is not reachable)
# ===========================================================================

def _live_engine():
    """Return a live engine or None if DATABASE_URL is unset / unreachable."""
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        return None
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return engine
    except Exception:
        return None


@pytest.mark.skipif(
    _live_engine() is None,
    reason="DATABASE_URL is not set or Postgres is not reachable",
)
class TestLiveDatabase:
    """
    Creates a throwaway schema with a random name, runs all table operations
    inside it, then drops the schema.  Never touches real application tables.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def isolated_engine(cls):
        """Engine pointed at a one-off schema; dropped after the test class."""
        engine = _live_engine()
        if engine is None:
            pytest.skip("DATABASE_URL is not set or Postgres is not reachable")

        schema = f"test_{uuid.uuid4().hex}"

        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

        # Create tables in the throwaway schema using set_schema on a copy of metadata.
        schema_meta = sa.MetaData(schema=schema)
        for table in Base.metadata.sorted_tables:
            table.tometadata(schema_meta)
        schema_meta.create_all(bind=engine)

        yield engine, schema

        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()

    def test_insert_and_cascade_delete(self, isolated_engine) -> None:
        engine, schema = isolated_engine

        # We use raw Core INSERT/SELECT/DELETE to stay independent of session config.
        incidents_t = sa.Table(
            "incidents", sa.MetaData(),
            autoload_with=engine, schema=schema,
        )
        analyses_t = sa.Table(
            "analyses", sa.MetaData(),
            autoload_with=engine, schema=schema,
        )
        fix_attempts_t = sa.Table(
            "fix_attempts", sa.MetaData(),
            autoload_with=engine, schema=schema,
        )
        deliveries_t = sa.Table(
            "deliveries", sa.MetaData(),
            autoload_with=engine, schema=schema,
        )

        with engine.begin() as conn:
            # Insert incident
            inc_result = conn.execute(
                incidents_t.insert().values(
                    signature="TestError: boom",
                    service_name="test-svc",
                    error_class="TestError",
                    redacted_message="boom",
                    occurrences=1,
                ).returning(incidents_t.c.id)
            )
            inc_id = inc_result.scalar_one()

            # Insert analysis
            ana_result = conn.execute(
                analyses_t.insert().values(
                    incident_id=inc_id,
                    category="code_bug",
                    confidence="high",
                    status="escalated",
                    reason="test reason",
                    signals=["TestError"],
                    classifier_on=True,
                ).returning(analyses_t.c.id)
            )
            ana_id = ana_result.scalar_one()

            # Insert fix attempt
            conn.execute(
                fix_attempts_t.insert().values(
                    analysis_id=ana_id,
                    attempt_no=1,
                    diff_text="--- a\n+++ b\n",
                    validator_ok=True,
                    verification_status="fix_confirmed",
                    tests_before_passed=5,
                    tests_before_failed=1,
                    tests_after_passed=6,
                    tests_after_failed=0,
                )
            )

            # Insert delivery
            conn.execute(
                deliveries_t.insert().values(
                    analysis_id=ana_id,
                    pr_url="https://github.com/example/pr/1",
                    branch="fix/test-branch",
                    slack_sent=True,
                    outcome="open",
                )
            )

            # Verify all rows exist
            assert conn.execute(
                sa.select(sa.func.count()).select_from(incidents_t)
                .where(incidents_t.c.id == inc_id)
            ).scalar() == 1
            assert conn.execute(
                sa.select(sa.func.count()).select_from(analyses_t)
                .where(analyses_t.c.incident_id == inc_id)
            ).scalar() == 1
            assert conn.execute(
                sa.select(sa.func.count()).select_from(fix_attempts_t)
                .where(fix_attempts_t.c.analysis_id == ana_id)
            ).scalar() == 1
            assert conn.execute(
                sa.select(sa.func.count()).select_from(deliveries_t)
                .where(deliveries_t.c.analysis_id == ana_id)
            ).scalar() == 1

            # Delete the incident – children must cascade
            conn.execute(
                incidents_t.delete().where(incidents_t.c.id == inc_id)
            )

            # All children must be gone
            assert conn.execute(
                sa.select(sa.func.count()).select_from(analyses_t)
                .where(analyses_t.c.incident_id == inc_id)
            ).scalar() == 0
            assert conn.execute(
                sa.select(sa.func.count()).select_from(fix_attempts_t)
                .where(fix_attempts_t.c.analysis_id == ana_id)
            ).scalar() == 0
            assert conn.execute(
                sa.select(sa.func.count()).select_from(deliveries_t)
                .where(deliveries_t.c.analysis_id == ana_id)
            ).scalar() == 0
