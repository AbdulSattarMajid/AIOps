"""The worker saves each crash and each analysis result to Postgres history.

Most tests replace the two history functions with recorders, so they need no
database.  The last tests use a real Postgres inside a throwaway schema and
skip themselves when it is not reachable.
"""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from aiops.db import history
from aiops.db.models import Base
from aiops.db.session import _coerce_url, get_engine
from aiops.worker import worker
from aiops.worker.worker import drain_once

from test_auto_pipeline import (
    GOOD_DIFF,
    _client_with_events,
    _engine,
    _fake_delivery,
    _real_fake_analysis,
)


@pytest.fixture(autouse=True)
def _fresh_pause():
    history._reset_pause()
    yield
    history._reset_pause()


@pytest.fixture()
def recorded(monkeypatch):
    """Replace the history functions with recorders."""
    calls = {"seen": [], "analysis": []}

    def seen(incident, **_kwargs):
        calls["seen"].append(incident)
        return True

    def analysis(incident, summary, **kwargs):
        calls["analysis"].append({"incident": incident, "summary": summary, **kwargs})
        return True

    monkeypatch.setattr(history, "save_incident_seen", seen)
    monkeypatch.setattr(history, "save_incident_history", analysis)
    return calls


def _turn_on(monkeypatch, *, deliver="on", slack="https://hooks.test/fake"):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", deliver)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", slack)


def test_verified_fix_is_saved_with_delivery_and_slack(monkeypatch, recorded):
    _turn_on(monkeypatch)
    monkeypatch.setenv("AIOPS_CLASSIFIER", "on")
    client = _client_with_events()
    _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
    _github, delivery = _fake_delivery()

    assert drain_once(
        client, 1, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=delivery, slack_sender=lambda _text: None, block_ms=0,
    ) == 1

    assert len(recorded["seen"]) == 1
    assert len(recorded["analysis"]) == 1
    saved = recorded["analysis"][0]
    assert saved["status"] == "verified"
    assert saved["delivery"]["pr_url"] == "https://github.test/draft/1"
    assert saved["delivery"]["branch"].startswith("aiops/fix-")
    assert saved["slack_sent"] is True
    assert saved["classifier_on"] is True
    assert saved["summary"]["patch"]
    assert saved["incident"].service_name == "user-service"


def test_classifier_flag_off_is_recorded_as_off(monkeypatch, recorded):
    _turn_on(monkeypatch)
    monkeypatch.delenv("AIOPS_CLASSIFIER", raising=False)
    client = _client_with_events()
    _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
    _github, delivery = _fake_delivery()

    drain_once(
        client, 1, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=delivery, slack_sender=lambda _text: None, block_ms=0,
    )
    assert recorded["analysis"][0]["classifier_on"] is False


def test_repeat_crashes_are_seen_each_time_but_analysed_once(
    monkeypatch, recorded
):
    _turn_on(monkeypatch)
    client = _client_with_events(3)
    _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF, GOOD_DIFF])
    _github, delivery = _fake_delivery()

    assert drain_once(
        client, 3, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=delivery, slack_sender=lambda _text: None, block_ms=0,
    ) == 3
    assert len(recorded["seen"]) == 3
    assert len(recorded["analysis"]) == 1


def test_with_analysis_off_the_crash_is_still_recorded(monkeypatch, recorded):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "off")
    client = _client_with_events()

    assert drain_once(
        client, 1, engine=_engine(client), block_ms=0,
        auto_analyzer=lambda *_: pytest.fail("analysis must not run"),
    ) == 1
    assert len(recorded["seen"]) == 1
    assert recorded["analysis"] == []


def test_a_skipped_analysis_is_not_saved_as_an_analysis(monkeypatch, recorded):
    _turn_on(monkeypatch)
    client = _client_with_events()
    # Fill every analysis slot so this incident is skipped.
    monkeypatch.setattr(
        worker.RedisLimits, "acquire_slot", lambda *_a, **_k: None
    )

    assert drain_once(
        client, 1, engine=_engine(client),
        auto_analyzer=lambda *_: pytest.fail("analysis must not run"),
        block_ms=0,
    ) == 1
    assert len(recorded["seen"]) == 1
    assert recorded["analysis"] == []


def test_slack_failure_is_recorded_as_not_sent(monkeypatch, recorded):
    _turn_on(monkeypatch)
    client = _client_with_events()
    _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
    _github, delivery = _fake_delivery()

    def fail(_text):
        raise RuntimeError("fake slack outage")

    drain_once(
        client, 1, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=delivery, slack_sender=fail, block_ms=0,
    )
    assert recorded["analysis"][0]["slack_sent"] is False
    assert recorded["analysis"][0]["status"] == "verified"


def test_no_slack_webhook_means_slack_sent_is_unknown(monkeypatch, recorded):
    _turn_on(monkeypatch, slack="")
    client = _client_with_events()
    _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
    _github, delivery = _fake_delivery()

    drain_once(
        client, 1, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=delivery, block_ms=0,
    )
    assert recorded["analysis"][0]["slack_sent"] is None


def test_a_failed_analysis_is_saved_as_escalated(monkeypatch, recorded):
    _turn_on(monkeypatch)
    client = _client_with_events()

    def broken(_incident, _limits):
        raise RuntimeError("fake analysis failure")

    drain_once(
        client, 1, engine=_engine(client), auto_analyzer=broken,
        auto_deliverer=lambda *_: {}, slack_sender=lambda _text: None,
        block_ms=0,
    )
    saved = recorded["analysis"][0]
    assert saved["status"] == "escalated"
    assert saved["reason"] == "Automatic analysis failed; inspect service health."


def test_history_errors_never_stop_the_worker(monkeypatch):
    _turn_on(monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("history is broken")

    monkeypatch.setattr(history, "save_incident_seen", explode)
    monkeypatch.setattr(history, "save_incident_history", explode)
    client = _client_with_events(2)
    engine = _engine(client)
    _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF, GOOD_DIFF])
    github, delivery = _fake_delivery()
    messages = []

    assert drain_once(
        client, 2, engine=engine, auto_analyzer=analyze,
        auto_deliverer=delivery, slack_sender=messages.append, block_ms=0,
    ) == 2
    assert len(github.created_prs) == 1
    assert len(messages) == 1
    assert worker.QUEUE_STREAM and client.xlen(worker.QUEUE_STREAM) == 0
    incident = next(iter(engine.incidents.values()))
    assert engine.store.get_analysis_result(incident.id)["status"] == "verified"


def test_without_database_url_nothing_is_saved_and_nothing_breaks(monkeypatch):
    _turn_on(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = _client_with_events()
    _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
    github, delivery = _fake_delivery()

    assert drain_once(
        client, 1, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=delivery, slack_sender=lambda _text: None, block_ms=0,
    ) == 1
    assert len(github.created_prs) == 1


def test_run_worker_loads_dotenv_before_connecting(monkeypatch):
    order = []
    monkeypatch.setattr(
        "dotenv.load_dotenv", lambda *a, **k: order.append("dotenv")
    )

    def stop():
        order.append("redis")
        raise RuntimeError("stop here")

    monkeypatch.setattr(worker, "get_redis", stop)
    with pytest.raises(RuntimeError, match="stop here"):
        worker.run_worker()
    assert order == ["dotenv", "redis"]


# ---------------------------------------------------------------------------
# Real Postgres (skipped when it is not running)
# ---------------------------------------------------------------------------

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


@pytest.mark.skipif(
    _live_engine() is None,
    reason="DATABASE_URL is not set or Postgres is not reachable",
)
class TestWorkerWithPostgres:
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
        # The worker uses the default engine; point it at the throwaway schema.
        monkeypatch.setattr(history, "_default_engine", lambda: engine)
        yield engine
        engine.dispose()
        with base.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()

    @staticmethod
    def _rows(engine, query):
        with engine.connect() as conn:
            return conn.execute(sa.text(query)).mappings().all()

    def test_three_identical_crashes_make_one_incident_and_one_analysis(
        self, monkeypatch, pg
    ):
        _turn_on(monkeypatch)
        client = _client_with_events(3)
        _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF, GOOD_DIFF])
        _github, delivery = _fake_delivery()

        assert drain_once(
            client, 3, engine=_engine(client), auto_analyzer=analyze,
            auto_deliverer=delivery, slack_sender=lambda _text: None,
            block_ms=0,
        ) == 3

        incidents = self._rows(pg, "SELECT * FROM incidents")
        assert len(incidents) == 1
        assert incidents[0]["service_name"] == "user-service"
        assert incidents[0]["error_class"] == "AttributeError"
        assert incidents[0]["occurrences"] == 3
        assert incidents[0]["crash_function"] == "get_user_email"

        analyses = self._rows(pg, "SELECT * FROM analyses")
        assert len(analyses) == 1
        assert analyses[0]["status"] == "verified"

        attempts = self._rows(pg, "SELECT * FROM fix_attempts")
        assert len(attempts) == 1
        assert attempts[0]["verification_status"] == "fix_confirmed"
        assert (attempts[0]["tests_before_passed"],
                attempts[0]["tests_before_failed"]) == (0, 1)
        assert (attempts[0]["tests_after_passed"],
                attempts[0]["tests_after_failed"]) == (1, 0)

        deliveries = self._rows(pg, "SELECT * FROM deliveries")
        assert len(deliveries) == 1
        assert deliveries[0]["pr_url"] == "https://github.test/draft/1"
        assert deliveries[0]["slack_sent"] is True
        assert deliveries[0]["outcome"] == "open"

    def test_analysis_off_still_records_the_incident(self, monkeypatch, pg):
        monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "off")
        client = _client_with_events(2)

        assert drain_once(
            client, 2, engine=_engine(client), block_ms=0
        ) == 2
        incidents = self._rows(pg, "SELECT * FROM incidents")
        assert len(incidents) == 1
        assert incidents[0]["occurrences"] == 2
        assert self._rows(pg, "SELECT * FROM analyses") == []

    def test_a_dead_database_does_not_stop_the_fix(self, monkeypatch, pg):
        _turn_on(monkeypatch)
        pg.dispose()
        monkeypatch.setattr(
            history, "_default_engine",
            lambda: sa.create_engine(
                "postgresql+psycopg://x:y@127.0.0.1:1/none",
                connect_args={"connect_timeout": 1},
            ),
        )
        client = _client_with_events()
        _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
        github, delivery = _fake_delivery()
        messages = []

        assert drain_once(
            client, 1, engine=_engine(client), auto_analyzer=analyze,
            auto_deliverer=delivery, slack_sender=messages.append, block_ms=0,
        ) == 1
        assert len(github.created_prs) == 1
        assert len(messages) == 1
