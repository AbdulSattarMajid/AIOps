"""Recording a human decision about a draft PR (merged, closed, ignored).

Tests that need a real Postgres skip themselves when it is not reachable, and
work inside a throwaway schema.
"""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from aiops.api import main
from aiops.db import history, outcomes
from aiops.db.models import Base
from aiops.db.outcomes import (
    DeliveryNotFound,
    HistoryUnavailable,
    record_outcome,
)
from aiops.db.session import _coerce_url, get_engine

from conftest import TEST_API_KEY

PR = "https://github.com/someone/repo/pull/3"
URL = "/api/v1/history/outcome"


@pytest.fixture(autouse=True)
def _fresh_pause():
    history._reset_pause()
    yield
    history._reset_pause()


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


def _post(client: TestClient, body: dict, *, key: str | None = TEST_API_KEY):
    headers = {"X-API-Key": key} if key else {}
    return client.post(URL, json=body, headers=headers)


# ---------------------------------------------------------------------------
# No database needed
# ---------------------------------------------------------------------------

class TestWithoutDatabase:
    def test_unknown_outcome_is_refused(self) -> None:
        with pytest.raises(ValueError):
            record_outcome(PR, "deleted")

    def test_empty_link_is_refused(self) -> None:
        with pytest.raises(ValueError):
            record_outcome("   ", "merged")

    def test_without_database_url_it_is_unavailable(self, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(HistoryUnavailable):
            record_outcome(PR, "merged")

    def test_a_dead_database_is_unavailable_and_leaks_nothing(
        self, monkeypatch
    ) -> None:
        secret = "very-secret-pw-12345"
        monkeypatch.setenv(
            "DATABASE_URL", f"postgresql://someone:{secret}@127.0.0.1:1/nodb"
        )
        history._ENGINES.clear()
        with pytest.raises(HistoryUnavailable) as caught:
            record_outcome(PR, "merged")
        assert secret not in str(caught.value)
        assert "127.0.0.1" not in str(caught.value)
        history._ENGINES.clear()

    def test_the_api_refuses_a_request_without_a_key(self) -> None:
        with TestClient(main.app) as client:
            response = _post(client, {"pr_url": PR, "outcome": "merged"}, key=None)
        assert response.status_code == 401

    def test_the_api_refuses_an_unknown_outcome(self, authed_client) -> None:
        response = authed_client.post(
            URL, json={"pr_url": PR, "outcome": "deleted"}
        )
        assert response.status_code == 422

    def test_the_api_refuses_a_missing_link(self, authed_client) -> None:
        response = authed_client.post(URL, json={"outcome": "merged"})
        assert response.status_code == 422

    def test_the_api_says_503_when_there_is_no_database(
        self, authed_client, monkeypatch
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        response = authed_client.post(
            URL, json={"pr_url": PR, "outcome": "merged"}
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "The history database is not available."

    def test_the_api_passes_the_decision_through(
        self, authed_client, monkeypatch
    ) -> None:
        seen = {}

        def fake(pr_url, outcome):
            seen.update(pr_url=pr_url, outcome=outcome)
            return {"pr_url": pr_url, "outcome": outcome,
                    "decided_at": "2026-11-17T12:00:00+00:00", "updated": 1}

        monkeypatch.setattr(main, "record_outcome", fake)
        response = authed_client.post(
            URL, json={"pr_url": PR, "outcome": "merged"}
        )
        assert response.status_code == 200
        assert response.json()["outcome"] == "merged"
        assert seen == {"pr_url": PR, "outcome": "merged"}

    def test_the_api_says_404_for_an_unknown_link(
        self, authed_client, monkeypatch
    ) -> None:
        def missing(_pr_url, _outcome):
            raise DeliveryNotFound("nope")

        monkeypatch.setattr(main, "record_outcome", missing)
        response = authed_client.post(
            URL, json={"pr_url": PR, "outcome": "closed"}
        )
        assert response.status_code == 404

    def test_the_module_never_touches_github(self) -> None:
        source = open(outcomes.__file__, encoding="utf-8").read().lower()
        for word in ("github", "requests", "httpx", "urllib"):
            assert f"import {word}" not in source
            assert f"from {word}" not in source


# ---------------------------------------------------------------------------
# Real Postgres (skipped when it is not running)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    _live_engine() is None,
    reason="DATABASE_URL is not set or Postgres is not reachable",
)
class TestWithPostgres:
    @pytest.fixture()
    def pg(self, monkeypatch):
        base = _live_engine()
        schema = f"test_{uuid.uuid4().hex}"
        with base.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = sa.create_engine(
            _coerce_url(os.environ["DATABASE_URL"]),
            connect_args={"options": f"-c search_path={schema}"},
        )
        Base.metadata.create_all(engine)
        monkeypatch.setattr(history, "_default_engine", lambda: engine)
        yield engine
        engine.dispose()
        with base.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()

    @staticmethod
    def _save(engine, pr_url: str = PR, signature: str = "sig-1") -> None:
        from aiops.dedup.engine import Incident

        incident = Incident(
            id=1, service_name="svc", error_class="KeyError",
            signature=signature, first_seen="2026-11-17T07:00:00+00:00",
            last_seen="2026-11-17T07:00:00+00:00", count=1,
            sample_trace="KeyError: 'a'",
        )
        assert history.save_incident_history(
            incident, {"status": "verified"}, status="verified",
            delivery={"pr_url": pr_url, "branch": "aiops/fix-1"},
            slack_sent=True, engine=engine,
        )

    @staticmethod
    def _deliveries(engine):
        with engine.connect() as conn:
            return conn.execute(
                sa.text("SELECT pr_url, outcome, decided_at FROM deliveries")
            ).mappings().all()

    def test_merged_is_recorded_with_a_decision_time(self, pg) -> None:
        self._save(pg)
        result = record_outcome(PR, "merged")
        assert result["updated"] == 1
        assert result["outcome"] == "merged"
        assert result["decided_at"] is not None
        row = self._deliveries(pg)[0]
        assert row["outcome"] == "merged"
        assert row["decided_at"] is not None

    def test_a_decision_can_be_undone(self, pg) -> None:
        self._save(pg)
        record_outcome(PR, "closed")
        result = record_outcome(PR, "open")
        assert result["decided_at"] is None
        row = self._deliveries(pg)[0]
        assert row["outcome"] == "open"
        assert row["decided_at"] is None

    def test_a_trailing_slash_still_matches(self, pg) -> None:
        self._save(pg)
        assert record_outcome(PR + "/", "ignored")["updated"] == 1
        assert self._deliveries(pg)[0]["outcome"] == "ignored"

    def test_an_unknown_link_changes_nothing(self, pg) -> None:
        self._save(pg)
        with pytest.raises(DeliveryNotFound):
            record_outcome("https://github.com/x/y/pull/99", "merged")
        assert self._deliveries(pg)[0]["outcome"] == "open"

    def test_only_the_matching_pr_is_changed(self, pg) -> None:
        self._save(pg, PR, "sig-1")
        self._save(pg, "https://github.com/someone/repo/pull/4", "sig-2")
        record_outcome(PR, "merged")
        outcomes_by_url = {r["pr_url"]: r["outcome"] for r in self._deliveries(pg)}
        assert outcomes_by_url[PR] == "merged"
        assert outcomes_by_url["https://github.com/someone/repo/pull/4"] == "open"

    def test_the_same_pr_saved_twice_is_updated_everywhere(self, pg) -> None:
        self._save(pg)
        self._save(pg)
        assert record_outcome(PR, "merged")["updated"] == 2

    def test_the_api_records_the_decision_end_to_end(
        self, authed_client, pg
    ) -> None:
        self._save(pg)
        response = authed_client.post(
            URL, json={"pr_url": PR, "outcome": "merged"}
        )
        assert response.status_code == 200
        assert response.json()["updated"] == 1
        assert self._deliveries(pg)[0]["outcome"] == "merged"

    def test_the_api_says_404_for_an_unknown_link_end_to_end(
        self, authed_client, pg
    ) -> None:
        response = authed_client.post(
            URL, json={"pr_url": "https://github.com/x/y/pull/9",
                       "outcome": "merged"},
        )
        assert response.status_code == 404
