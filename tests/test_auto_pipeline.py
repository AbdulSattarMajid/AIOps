import json
from dataclasses import asdict
from pathlib import Path

import fakeredis
import pytest

from aiops.agent.fake_llm import FakeLLM
from aiops.agent.graph import run_agent, summarize
from aiops.delivery.github_pr import deliver
from aiops.ingest.schemas import LogEvent
from aiops.limits import RedisLimits
from aiops.sandbox.runner import SandboxResult
from aiops.storage.redis_store import QUEUE_STREAM, RedisStore
from aiops.dedup.engine import DedupEngine
from aiops.worker import worker
from aiops.worker.worker import drain_once


ROOT = "mock_services/sample_app"
TRACE = """Traceback (most recent call last):
  File "/app/users.py", line 19, in get_user_email
    return user.email
AttributeError: 'NoneType' object has no attribute 'email'"""
GOOD_DIFF = """--- a/users.py
+++ b/users.py
@@ -17,3 +17,5 @@
 def get_user_email(user_id):
     user = find_user(user_id)
+    if user is None:
+        raise LookupError(f"user {user_id} not found")
     return user.email
"""
NO_PATCH = "NO_PATCH: needs human review"


class FakeSandbox:
    def baseline(self):
        return SandboxResult(
            "failed", 1, "FAILED test_users.py::test_missing_user - failed",
            0.1, 0, 1, failed_tests=["test_users.py::test_missing_user"],
        )

    def verify(self, _rel_path, _new_source):
        return SandboxResult(
            "passed", 0, "1 passed", 0.1, 1, 0,
        )


class FakeGitHub:
    def __init__(self):
        self.created_prs = []
        self.source = (Path(ROOT) / "users.py").read_text(encoding="utf-8")

    def default_branch(self):
        return "main"

    def branch_sha(self, branch):
        assert branch == "main"
        return "base-sha"

    def branch_exists(self, _branch):
        return False

    def create_branch(self, _name, _sha):
        return None

    def get_file(self, _path, _ref):
        return self.source, "file-sha"

    def update_file(self, _path, _message, _content, _sha, _branch):
        return None

    def find_open_pr(self, _branch):
        return None

    def create_pr(self, _title, _body, head, base, draft):
        self.created_prs.append((head, base, draft))
        return "https://github.test/draft/1"


def _client_with_events(count=1):
    client = fakeredis.FakeRedis(decode_responses=True)
    event = LogEvent(
        timestamp="2026-11-04T10:00:00Z",
        service_name="user-service",
        error_class="AttributeError",
        stack_trace=TRACE,
    )
    pipe = client.pipeline(transaction=False)
    payload = json.dumps(event.model_dump(mode="json"))
    for _ in range(count):
        pipe.xadd(QUEUE_STREAM, {"event": payload})
    pipe.execute()
    return client


def _engine(client):
    return DedupEngine(store=RedisStore(client=client))


def _real_fake_analysis(monkeypatch, results):
    llm = FakeLLM({"def get_user_email": list(results)})
    sandbox = FakeSandbox()

    def analyze(incident, limits):
        return summarize(run_agent(
            llm,
            None,
            incident.sample_trace,
            incident.service_name,
            ROOT,
            incident_id=incident.id,
            sandbox=sandbox,
            limits=limits,
            signature=incident.signature,
        ))

    return llm, analyze


def _fake_delivery():
    github = FakeGitHub()

    def send(summary, incident):
        return asdict(deliver(summary, incident, github))

    return github, send


def test_auto_pipeline_flags_off_does_nothing(monkeypatch):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "off")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "off")
    client = _client_with_events()
    called = []

    assert drain_once(
        client, 1, engine=_engine(client),
        auto_analyzer=lambda *_: called.append("analysis"),
        auto_deliverer=lambda *_: called.append("delivery"),
    ) == 1
    assert called == []


def test_auto_analyze_without_auto_delivery(monkeypatch):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "off")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    client = _client_with_events()
    llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
    deliveries = []

    assert drain_once(
        client, 1, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=lambda *_: deliveries.append(True),
    ) == 1
    assert len(llm.calls) == 1
    assert deliveries == []


def test_verified_auto_analysis_opens_one_draft_and_sends_slack(monkeypatch):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "on")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.test/fake")
    client = _client_with_events()
    llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
    github, delivery = _fake_delivery()
    messages = []

    assert drain_once(
        client, 1, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=delivery, slack_sender=messages.append,
    ) == 1
    assert len(llm.calls) == 1
    assert len(github.created_prs) == 1
    assert github.created_prs[0][1:] == ("main", True)
    assert len(messages) == 1
    assert "Incident 1 user-service" in messages[0]
    assert "AttributeError" in messages[0]
    assert "tests 0/1 -> 1/0" in messages[0]
    assert "https://github.test/draft/1" in messages[0]


def test_unverified_analysis_never_delivers_and_slack_requests_human(
    monkeypatch,
):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "on")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.test/fake")
    client = _client_with_events()
    _llm, analyze = _real_fake_analysis(monkeypatch, [NO_PATCH])
    deliveries = []
    messages = []

    assert drain_once(
        client, 1, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=lambda *_: deliveries.append(True),
        slack_sender=messages.append,
    ) == 1
    assert deliveries == []
    assert len(messages) == 1
    assert "needs a human" in messages[0]


def test_duplicate_events_run_one_analysis_and_at_most_one_pr(monkeypatch):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "on")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    client = _client_with_events(1000)
    llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF])
    github, delivery = _fake_delivery()

    assert drain_once(
        client, 1000, engine=_engine(client), auto_analyzer=analyze,
        auto_deliverer=delivery, block_ms=0,
    ) == 1000
    assert len(llm.calls) == 1
    assert len(github.created_prs) == 1


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        ("cooldown", "signature cooldown is active"),
        ("budget", "incident token budget reached"),
    ],
)
def test_limit_blocks_are_recorded_and_worker_continues(
    monkeypatch, block, reason
):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "off")
    client = _client_with_events(2)
    engine = _engine(client)
    calls = []

    class BlockingLimits(RedisLimits):
        def claim_cooldown(self, signature):
            if block == "cooldown":
                return False
            return super().claim_cooldown(signature)

        def check_token_budget(self, incident_id):
            if block == "budget":
                return "incident token budget reached"
            return None

    monkeypatch.setattr(worker, "RedisLimits", BlockingLimits)
    assert drain_once(
        client, 2, engine=engine,
        auto_analyzer=lambda *_: calls.append(True),
        block_ms=0,
    ) == 2
    assert calls == []
    incident = next(iter(engine.incidents.values()))
    result = engine.store.get_analysis_result(incident.id)
    assert result["status"] == "skipped"
    assert reason in result["reason"]
    assert client.xlen(QUEUE_STREAM) == 0


@pytest.mark.parametrize("failure", ["analysis", "github", "slack"])
def test_delivery_or_slack_error_does_not_stop_worker(
    monkeypatch, failure
):
    monkeypatch.setenv("AIOPS_AUTO_ANALYZE", "on")
    monkeypatch.setenv("AIOPS_AUTO_DELIVER_PR", "on")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.test/fake")
    client = _client_with_events(2)
    engine = _engine(client)
    _llm, analyze = _real_fake_analysis(monkeypatch, [GOOD_DIFF, GOOD_DIFF])

    def fail(*_args):
        raise RuntimeError("fake integration failure")

    github, delivery = _fake_delivery()
    if failure == "analysis":
        def analyze(_incident, _limits):
            raise RuntimeError("fake analysis failure")
    elif failure == "github":
        delivery = fail
    assert drain_once(
        client, 2, engine=engine, auto_analyzer=analyze,
        auto_deliverer=delivery,
        slack_sender=fail if failure == "slack" else (lambda _text: None),
        block_ms=0,
    ) == 2
    assert client.xlen(QUEUE_STREAM) == 0
    assert client.xpending(QUEUE_STREAM, "aiops-workers")["pending"] == 0
    incident = next(iter(engine.incidents.values()))
    result = engine.store.get_analysis_result(incident.id)
    assert result is not None
    if failure == "analysis":
        assert result["status"] == "escalated"
        assert result["reason"] == (
            "Automatic analysis failed; inspect service health."
        )
    elif failure == "github":
        assert result["reason"] == "Automatic PR delivery failed; inspect service health."
        assert github.created_prs == []
    else:
        assert result["reason"] == "Slack notification failed; inspect service health."
