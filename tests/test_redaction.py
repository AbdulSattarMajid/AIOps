import json

import pytest
from fastapi.testclient import TestClient

from aiops.agent.fake_llm import FakeLLM, make_demo_llm
from aiops.agent.graph import run_agent
from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.delivery.pr_text import render_pr
from aiops.delivery.slack import build_slack_text
from aiops.ingest.schemas import LogEvent
from aiops.rag.store import RunbookStore
from aiops.sandbox.runner import SandboxResult
from aiops.security.redaction import redact_text, source_secret_findings


USERS_TRACE = """Traceback (most recent call last):
  File "/app/users.py", line 19, in get_user_email
    return user.email
AttributeError: 'NoneType' object has no attribute 'email'"""

WRONG_BUT_VALID_USERS_DIFF = """--- a/users.py
+++ b/users.py
@@ -17,3 +17,3 @@
 def get_user_email(user_id):
     user = find_user(user_id)
-    return user.email
+    return user.email if user is not None else None
"""

GOOD_USERS_DIFF = """--- a/users.py
+++ b/users.py
@@ -17,3 +17,5 @@
 def get_user_email(user_id):
     user = find_user(user_id)
+    if user is None:
+        raise LookupError(f"user {user_id} not found")
     return user.email
"""


def _secret(prefix, filler="A", length=24):
    return prefix + filler * length


@pytest.fixture
def client_and_store(tmp_path):
    main.engine = DedupEngine()
    main.app.dependency_overrides.clear()
    store = RunbookStore(path=str(tmp_path / "chroma"))
    store.load_directory("runbooks/default")
    main.app.dependency_overrides[main.get_store] = lambda: store
    main.app.dependency_overrides[main.get_sandbox] = lambda: None
    with TestClient(main.app) as client:
        yield client, store
    main.app.dependency_overrides.clear()


def test_none_and_empty_are_safe():
    assert redact_text(None) == ("", {})
    assert redact_text("") == ("", {})


def test_email_is_redacted():
    email = "oncall.user@" + "example.test"
    clean, findings = redact_text("failed for " + email)
    assert email not in clean
    assert "<REDACTED_EMAIL>" in clean
    assert findings == {"email": 1}
    assert email not in str(findings)


def test_key_value_secrets():
    password = "not-a-real-" + "value"
    for key in ("password", "passwd", "pwd", "secret", "token", "api_key", "apikey"):
        clean, findings = redact_text(f"{key}={password}")
        assert password not in clean
        assert findings["secret"] == 1
        assert "<REDACTED_SECRET>" in clean


def test_authorization_and_bearer_headers():
    token = "abc" + "def" * 8
    clean, findings = redact_text("Authorization: Bearer " + token)
    assert token not in clean
    assert findings.get("authorization") == 1


def test_jwt_is_redacted():
    jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    clean, findings = redact_text("header " + jwt)
    assert jwt not in clean
    assert findings == {"jwt": 1}


def test_aws_access_key():
    key = "AKIA" + "B" * 16
    clean, findings = redact_text(key)
    assert key not in clean
    assert findings == {"aws_key": 1}


def test_provider_api_keys():
    groq = _secret("gsk_")
    github = _secret("ghp_")
    pat = _secret("github_pat_", "A_", 12)
    slack = "xoxb-" + "1" * 12
    stripe = _secret("sk_live_")
    sample = " ".join([groq, github, pat, slack, stripe])
    clean, findings = redact_text(sample)
    for value in (groq, github, pat, slack, stripe):
        assert value not in clean
        assert value not in str(findings)
    assert findings["groq_key"] == 1
    assert findings["github_token"] == 2
    assert findings["slack_token"] == 1
    assert findings["stripe_key"] == 1


def test_pem_private_key_block():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "MIIB" + "x" * 40 + "\n"
        + "-----END PRIVATE KEY-----"
    )
    clean, findings = redact_text("key:\n" + pem)
    assert "MIIB" not in clean
    assert findings == {"private_key": 1}
    assert "<REDACTED_PRIVATE_KEY>" in clean


def test_url_credentials_and_connection_string():
    password = "dbp" + "ass99"
    url = "https://alice:" + password + "@example.test/path"
    conn = "postgres://svc:" + password + "@localhost/app"
    clean, findings = redact_text(url + " " + conn)
    assert password not in clean
    assert findings["url_credentials"] == 1
    assert findings["connection_string"] == 1


def test_credit_card_uses_luhn():
    valid = "4111" + "1111" * 3
    invalid = "4111" + "1111" * 2 + "1112"
    clean, findings = redact_text(f"pay {valid} skip {invalid}")
    assert valid not in clean
    assert invalid in clean
    assert findings == {"credit_card": 1}


def test_phone_conservative():
    phone = "555-123-4567"
    clean, findings = redact_text("call " + phone)
    assert phone not in clean
    assert findings == {"phone": 1}


def test_false_positives_stay_unchanged():
    text = (
        'File "/app/users.py", line 19, in get_user_email\n'
        "HTTP status 404 then 500\n"
        "timeout=30 request_id=abc\n"
        "AttributeError: 'NoneType' object has no attribute 'email'"
    )
    clean, findings = redact_text(text)
    assert clean == text
    assert findings == {}


def test_huge_input_is_safe():
    email = "bulk.user@" + "example.test"
    text = ("x" * 80000) + " " + email
    clean, findings = redact_text(text)
    assert email not in clean
    assert findings == {"email": 1}


def test_api_log_secret_never_reaches_incident_chroma_or_prompt(client_and_store):
    client, store = client_and_store
    password = "not-a-real-" + "hunter"
    email = "victim.user@" + "example.test"
    llm = make_demo_llm()
    main.app.dependency_overrides[main.get_llm] = lambda: llm
    response = client.post("/api/v1/telemetry/logs", json={
        "timestamp": "2026-11-03T10:00:00Z",
        "service_name": "user-service",
        "error_class": "AttributeError",
        "message": f"login failed for {email} password={password}",
        "stack_trace": (
            '  File "/app/users.py", line 19, in get_user_email\n'
            "    return user.email\n"
            f"AttributeError: 'NoneType' object has no attribute 'email' "
            f"password={password} email={email}"
        ),
        "note": f"extra password={password}",
    })
    assert response.status_code == 200
    incident_id = response.json()["incident_id"]
    stored = client.get("/api/v1/incidents").json()[0]
    blob = json.dumps(stored)
    assert password not in blob
    assert email not in blob
    assert stored["redaction"]
    chroma = store._collection.get(include=["documents", "metadatas"])
    chroma_blob = json.dumps(chroma, default=str)
    assert password not in chroma_blob
    assert email not in chroma_blob
    analyzed = client.post(f"/api/v1/incidents/{incident_id}/analyze")
    assert analyzed.status_code == 200
    prompts = "\n".join(user for _, user in llm.calls)
    assert password not in prompts
    assert email not in prompts


def test_secret_in_function_source_escalates_without_llm(tmp_path):
    groq = _secret("gsk_")
    (tmp_path / "leaky.py").write_text(
        "def run():\n"
        f'    key = "{groq}"\n'
        "    return key\n",
        encoding="utf-8",
    )
    llm = FakeLLM({"def run": ["NO_PATCH: should not run"]})
    trace = (
        '  File "/app/leaky.py", line 3, in run\n'
        "    return key\n"
        "RuntimeError: boom"
    )
    result = run_agent(llm, None, trace, "user-service", str(tmp_path))
    assert result["status"] == "escalated"
    assert result["reason"] == "secret found in source code"
    assert not llm.calls
    assert groq not in str(result["reason"])


def test_secret_in_test_output_is_not_in_next_prompt():
    groq = _secret("gsk_", "B")

    class FakeSandbox:
        def baseline(self):
            return SandboxResult(
                "failed", 1, "", 0.1, 4, 1,
                failed_tests=["test_users.py::test_missing_user"],
            )

        def verify(self, rel_path, new_source):
            return SandboxResult(
                "failed", 1,
                f"FAILURES {groq}",
                0.1, 4, 1,
                failed_tests=["test_users.py::test_missing_user"],
            )

    llm = FakeLLM({
        "def get_user_email": [WRONG_BUT_VALID_USERS_DIFF, GOOD_USERS_DIFF],
    })
    result = run_agent(
        llm, None, USERS_TRACE, "user-service", "mock_services/sample_app",
        sandbox=FakeSandbox(),
    )
    assert len(llm.calls) >= 2
    assert groq not in llm.calls[1][1]
    assert groq not in (result.get("last_test_output") or "")


def test_logs_that_differ_only_by_secret_share_signature():
    first = "pw-" + "ONE"
    second = "pw-" + "TWO"
    engine = DedupEngine()
    base = {
        "timestamp": "2026-11-03T10:00:00Z",
        "service_name": "user-service",
        "error_class": "AttributeError",
        "stack_trace": (
            'File "/app/users.py", line 19, in get_user_email\n'
            "AttributeError: missing user password={}"
        ),
    }
    incident_a, is_new_a = engine.process(
        LogEvent(**{**base, "stack_trace": base["stack_trace"].format(first)})
    )
    incident_b, is_new_b = engine.process(
        LogEvent(**{**base, "stack_trace": base["stack_trace"].format(second)})
    )
    assert is_new_a is True
    assert is_new_b is False
    assert incident_a.signature == incident_b.signature
    assert incident_a.id == incident_b.id


def test_secret_in_llm_patch_is_not_in_pr_or_slack_text():
    github = _secret("ghp_")
    incident = {
        "id": 9,
        "service_name": "user-service",
        "signature": "AttributeError: missing user email",
        "occurrences": 4,
    }
    summary = {
        "status": "verified",
        "patch": GOOD_USERS_DIFF + f"\n+ token = '{github}'",
        "file": "users.py",
        "function": "get_user_email",
        "verification": {"status": "fix_confirmed"},
        "attempts": 1,
        "model": "fake",
        "runbooks": [],
    }
    title, body = render_pr(summary, incident)
    slack = build_slack_text(
        summary,
        {**incident, "signature": "AttributeError " + github},
        "https://github.com/example/app/pull/42",
    )
    assert github not in title
    assert github not in body
    assert github not in slack
    assert "<REDACTED_GITHUB_TOKEN>" in body


def test_source_scan_ignores_normal_assignments():
    assert source_secret_findings("token = self.pool\nreturn user.email") == {}
    assert source_secret_findings('key = os.environ["STRIPE_API_KEY"]') == {}
