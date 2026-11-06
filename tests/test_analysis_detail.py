import fakeredis
import pytest
from fastapi.testclient import TestClient

from aiops.api import main
from aiops.agent.graph import summarize
from aiops.dedup.engine import DedupEngine, Incident
from aiops.ingest.schemas import LogEvent
from aiops.storage.redis_store import (
    DEFAULT_TTL_SECONDS,
    InMemoryStore,
    RedisStore,
    build_analysis_detail,
    save_analysis_detail_safely,
)
from aiops.worker import worker
from tests.conftest import TEST_API_KEY


def _incident():
    return Incident(
        id=71,
        service_name="shop-service",
        error_class="RuntimeError",
        signature="signature",
        first_seen="2026-11-06T10:00:00Z",
        last_seen="2026-11-06T10:00:00Z",
        count=1,
        sample_trace="RuntimeError: fake",
    )


@pytest.mark.parametrize("status", ["verified", "escalated"])
def test_worker_persists_analysis_summary_and_rejected_diff(monkeypatch, status):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "off")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    client = fakeredis.FakeRedis(decode_responses=True)
    store = InMemoryStore()
    processor = type("Processor", (), {"store": store})()
    credential = "gsk_" + "Z" * 24
    proposed = "x" * 1800 + f"\n{'api' + '_key'}={credential}"
    summary = {
        "status": status,
        "reason": f"analysis failed with {credential}" if status == "escalated" else "",
        "attempts": 2,
        "tokens_used": 23,
        "history": [
            {
                "attempt": 1,
                "stage": "diff",
                "error": f"rejected: {credential} " + "e" * 600,
            },
        ],
        "_analysis_history": [
            {
                "attempt": 1,
                "stage": "diff",
                "error": f"rejected: {credential} " + "e" * 600,
                "reply": proposed,
            },
        ],
        "_analysis_last_reply": "",
        "patch": f"--- patch {credential}",
        "function": "charge",
        "file": "demo_shop/payments.py",
        "baseline_tests_passed": 1,
        "baseline_tests_failed": 1,
        "baseline_failed_tests": ["test_payments.py::test_missing_key"],
        "after_failed_tests": [],
        "verification": {"status": "fix_confirmed"},
    }

    worker._maybe_auto_analyze(
        processor,
        _incident(),
        client,
        lambda *_: summary,
    )

    detail = store.get_analysis_detail(71)
    assert detail["status"] == status
    assert detail["attempts"] == 2
    assert detail["tokens_used"] == 23
    assert detail["rejected_attempts"][0]["attempt"] == 1
    assert detail["rejected_attempts"][0]["stage"] == "diff"
    assert len(detail["rejected_attempts"][0]["error"]) <= 500
    assert len(detail["rejected_attempts"][0]["proposed_diff"]) == 1500
    assert credential not in str(detail)
    assert "<REDACTED_GROQ_KEY>" in str(detail)
    assert detail["test_evidence"]["verification"]["status"] == "fix_confirmed"


def test_worker_persists_status_and_reason_if_analysis_raises(monkeypatch):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "off")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    client = fakeredis.FakeRedis(decode_responses=True)
    store = InMemoryStore()
    processor = type("Processor", (), {"store": store})()

    def fail(*_):
        raise RuntimeError("private failure text")

    worker._maybe_auto_analyze(
        processor, _incident(), client, fail,
    )

    assert store.get_analysis_detail(71) == {
        "status": "escalated",
        "reason": "Automatic analysis failed; inspect service health.",
    }


def test_analysis_detail_redacts_and_truncates_fields():
    credential = "gsk_" + "A" * 24
    detail = build_analysis_detail({
        "status": "escalated",
        "reason": f"failed {credential}",
        "attempts": 3,
        "tokens_used": 5,
        "history": [{
            "attempt": 2,
            "stage": "diff",
            "error": "e" * 600,
            "reply": "d" * 1600,
        }],
        "patch": "p" * 5000,
        "function": "run",
        "file": "app.py",
        "baseline_failed_tests": [f"test_x.py::{credential}"],
    })

    assert len(detail["patch"]) == 4000
    assert len(detail["rejected_attempts"][0]["error"]) == 500
    assert len(detail["rejected_attempts"][0]["proposed_diff"]) == 1500
    assert credential not in str(detail)
    assert "<REDACTED_GROQ_KEY>" in str(detail)


def test_analysis_detail_redacts_before_truncating():
    credential = (
        "gsk_" + "A1B2C3D4E5F6G7H8J9K0L1M2N3O4P5Q6R7S8T9"
    )
    summary = {
        "status": "escalated",
        "attempts": 1,
        "history": [{
            "attempt": 1,
            "stage": "diff",
            "error": "e" * 475 + "\n" + credential,
            "reply": "d" * 1475 + "\n" + credential,
        }],
        "patch": "p" * 3975 + "\n" + credential,
    }

    detail = build_analysis_detail(summary)
    stored_text = str(detail)

    assert len(detail["rejected_attempts"][0]["error"]) <= 500
    assert len(detail["rejected_attempts"][0]["proposed_diff"]) <= 1500
    assert len(detail["patch"]) <= 4000
    assert "<REDACTED_GROQ_KEY>" in stored_text
    assert not any(
        credential[index:index + 7] in stored_text
        for index in range(len(credential) - 6)
    )


def test_analysis_detail_uses_fields_from_real_summary():
    summary = summarize({
        "status": "escalated",
        "function_name": "load_payment_config",
        "rel_path": "demo_shop/payments.py",
    })

    detail = build_analysis_detail(summary)

    assert detail["function"] == "load_payment_config"
    assert detail["file"] == "demo_shop/payments.py"


def test_analysis_detail_save_failures_are_swallowed():
    class BrokenStore:
        def record_analysis_detail(self, _incident_id, _detail):
            raise RuntimeError("backend unavailable")

    save_analysis_detail_safely(
        BrokenStore(),
        1,
        {"status": "escalated", "reason": "failed", "attempts": 0},
    )


def test_redis_analysis_detail_has_seven_day_expiry():
    client = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(client=client)
    store.record_analysis_detail(71, {"status": "escalated", "reason": "fake"})

    assert store.get_analysis_detail(71) == {
        "status": "escalated",
        "reason": "fake",
    }
    assert client.ttl("aiops:analysis-detail:71") == DEFAULT_TTL_SECONDS


def test_analysis_detail_endpoint_requires_auth_and_returns_404_or_saved_detail():
    old_engine = main.engine
    main.engine = DedupEngine(store=InMemoryStore())
    try:
        with TestClient(main.app) as client:
            response = client.get("/api/v1/incidents/71/analysis")
            assert response.status_code == 401

        with TestClient(
            main.app, headers={"X-API-Key": TEST_API_KEY}
        ) as client:
            response = client.get("/api/v1/incidents/71/analysis")
            assert response.status_code == 404
            main.engine.store.record_analysis_detail(
                71, {"status": "escalated", "reason": "fake"}
            )
            response = client.get("/api/v1/incidents/71/analysis")
            assert response.status_code == 200
            assert response.json() == {
                "status": "escalated",
                "reason": "fake",
            }
    finally:
        main.engine = old_engine


def test_manual_analysis_persists_summarized_detail(monkeypatch, tmp_path):
    store = InMemoryStore()
    old_engine = main.engine
    old_repo_root = main.REPO_ROOT
    main.engine = DedupEngine(store=store)
    main.REPO_ROOT = str(tmp_path)
    incident, _ = main.engine.process(LogEvent(
        timestamp="2026-11-06T10:00:00Z",
        service_name="shop-service",
        error_class="RuntimeError",
        stack_trace="RuntimeError: fake",
    ))

    class Limits:
        def acquire_slot(self, _name):
            return "slot"

        def release_slot(self, _name, _slot):
            return None

    state = {
        "incident_id": incident.id,
        "status": "verified",
        "reason": "",
        "attempt": 2,
        "tokens_used": 17,
        "function_name": "charge",
        "rel_path": "demo_shop/payments.py",
        "patch": "fake patch",
        "file_source": "source that must not be stored",
        "history": [{
            "attempt": 1,
            "stage": "diff",
            "error": "path rejected",
            "reply": "bad proposed diff",
        }],
        "last_reply": "final proposed diff",
    }
    main.app.dependency_overrides[main.get_llm] = lambda: object()
    main.app.dependency_overrides[main.get_store] = lambda: None
    main.app.dependency_overrides[main.get_sandbox] = lambda: None
    main.app.dependency_overrides[main.get_limits] = lambda: Limits()
    monkeypatch.setattr(main, "run_agent", lambda *_args, **_kwargs: state)
    try:
        with TestClient(
            main.app, headers={"X-API-Key": TEST_API_KEY}
        ) as client:
            response = client.post(
                f"/api/v1/incidents/{incident.id}/analyze"
            )
            assert response.status_code == 200
            detail = client.get(
                f"/api/v1/incidents/{incident.id}/analysis"
            ).json()
        assert detail["status"] == "verified"
        assert detail["attempts"] == 2
        assert detail["tokens_used"] == 17
        assert detail["rejected_attempts"][0]["proposed_diff"] == (
            "bad proposed diff"
        )
        assert "file_source" not in detail
        assert "source that must not be stored" not in str(detail)
    finally:
        main.engine = old_engine
        main.REPO_ROOT = old_repo_root
        main.app.dependency_overrides.clear()


def test_manual_analysis_crash_saves_only_status_and_reason(monkeypatch, tmp_path):
    store = InMemoryStore()
    old_engine = main.engine
    old_repo_root = main.REPO_ROOT
    main.engine = DedupEngine(store=store)
    main.REPO_ROOT = str(tmp_path)
    incident, _ = main.engine.process(LogEvent(
        timestamp="2026-11-06T10:00:00Z",
        service_name="shop-service",
        error_class="RuntimeError",
        stack_trace="RuntimeError: fake",
    ))

    class Limits:
        def acquire_slot(self, _name):
            return "slot"

        def release_slot(self, _name, _slot):
            return None

    def fail_before_summary(*_args, **_kwargs):
        raise RuntimeError("private traceback and credentials")

    main.app.dependency_overrides[main.get_llm] = lambda: object()
    main.app.dependency_overrides[main.get_store] = lambda: None
    main.app.dependency_overrides[main.get_sandbox] = lambda: None
    main.app.dependency_overrides[main.get_limits] = lambda: Limits()
    monkeypatch.setattr(main, "run_agent", fail_before_summary)
    try:
        with TestClient(
            main.app,
            headers={"X-API-Key": TEST_API_KEY},
            raise_server_exceptions=False,
        ) as client:
            response = client.post(
                f"/api/v1/incidents/{incident.id}/analyze"
            )
        assert response.status_code == 500
        assert store.get_analysis_detail(incident.id) == {
            "status": "escalated",
            "reason": "Analysis failed before a summary was produced.",
        }
    finally:
        main.engine = old_engine
        main.REPO_ROOT = old_repo_root
        main.app.dependency_overrides.clear()
