"""Tests for aiops.agent.classifier and its integration with the graph."""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import pytest

from aiops.agent.classifier import Classification, classify_incident
from aiops.agent.fake_llm import FakeLLM
from aiops.agent.graph import build_graph, run_agent, summarize


# ---------------------------------------------------------------------------
# Parametrized table: 25+ cases
# ---------------------------------------------------------------------------

class Case(NamedTuple):
    label: str
    exc: str
    msg: str
    crash: str
    expected_category: str


TABLE: list[Case] = [
    # --- config_issue (rule a) ---
    Case(
        "config_keyerror_os_environ_bracket",
        "KeyError", "'SHOP_PAYMENT_KEY'",
        '    return {"payment_key": os.environ["SHOP_PAYMENT_KEY"]}',
        "config_issue",
    ),
    Case(
        "config_keyerror_environ_single_quote",
        "KeyError", "'DATABASE_URL'",
        "    url = os.environ['DATABASE_URL']",
        "config_issue",
    ),
    Case(
        "config_runtimeerror_env_var_not_set",
        "RuntimeError", "Environment variable API_KEY is not set",
        "",
        "config_issue",
    ),
    Case(
        "config_keyerror_os_environ_no_quotes_in_msg",
        "KeyError", "'SECRET_KEY'",
        "value = environ['SECRET_KEY']",
        "config_issue",
    ),
    Case(
        "config_message_env_var_missing",
        "EnvironmentError", "environment variable DB_PASSWORD is not set",
        "",
        "config_issue",
    ),
    # --- NOT config (code_bug) ---
    Case(
        "not_config_plain_dict",
        "KeyError", "'user_id'",
        '    return data["user_id"]',
        "code_bug",
    ),
    Case(
        "not_config_config_object",
        "KeyError", "'DB_POOL_SIZE'",
        '    size = config["DB_POOL_SIZE"]',
        "code_bug",
    ),
    # --- capacity_issue (rule b) ---
    Case(
        "capacity_pool_timeout",
        "TimeoutError", "connection pool timeout after 30s",
        "",
        "capacity_issue",
    ),
    Case(
        "capacity_queuepool_limit",
        "TimeoutError",
        "QueuePool limit of size 5 overflow 10 reached, connection timed out",
        "",
        "capacity_issue",
    ),
    Case(
        "capacity_memory_error",
        "MemoryError", "",
        "",
        "capacity_issue",
    ),
    Case(
        "capacity_no_space_left",
        "OSError", "[Errno 28] No space left on device",
        "",
        "capacity_issue",
    ),
    Case(
        "capacity_too_many_open_files",
        "OSError", "[Errno 24] Too many open files",
        "",
        "capacity_issue",
    ),
    Case(
        "capacity_too_many_clients",
        "OperationalError", "FATAL: sorry, too many clients already",
        "",
        "capacity_issue",
    ),
    # --- dependency_outage (rule c) ---
    Case(
        "dependency_connection_refused_error",
        "ConnectionRefusedError", "[Errno 111] Connection refused",
        "",
        "dependency_outage",
    ),
    Case(
        "dependency_connect_timeout_empty_msg",
        "ConnectTimeout", "",
        "",
        "dependency_outage",
    ),
    Case(
        "dependency_requests_max_retries",
        "ConnectionError",
        "Max retries exceeded with url: /v1 (Caused by NewConnectionError('Name or service not known'))",
        "",
        "dependency_outage",
    ),
    Case(
        "dependency_gaierror",
        "gaierror", "[Errno -2] Name or service not known",
        "",
        "dependency_outage",
    ),
    Case(
        "dependency_redis_connection_refused",
        "ConnectionError",
        "Error 111 connecting to redis:6379. Connection refused.",
        "",
        "dependency_outage",
    ),
    Case(
        "dependency_503_in_message",
        "RuntimeError", "upstream returned 503 Service Unavailable",
        "",
        "dependency_outage",
    ),
    # --- code_bug (rule d) ---
    Case(
        "code_bug_attribute_error_nonetype_id",
        "AttributeError", "'NoneType' object has no attribute 'id'",
        "",
        "code_bug",
    ),
    Case(
        "code_bug_attribute_error_nonetype_title",
        "AttributeError", "'NoneType' object has no attribute 'title'",
        "",
        "code_bug",
    ),
    Case(
        "code_bug_type_error_operand",
        "TypeError", "unsupported operand type(s) for +: 'int' and 'str'",
        "",
        "code_bug",
    ),
    Case(
        "code_bug_index_error",
        "IndexError", "list index out of range",
        "",
        "code_bug",
    ),
    Case(
        "code_bug_zero_division",
        "ZeroDivisionError", "division by zero",
        "",
        "code_bug",
    ),
    # --- unknown (rule e) ---
    Case(
        "unknown_generic_timeout",
        "TimeoutError", "timed out",
        "",
        "unknown",
    ),
    Case(
        "unknown_statement_error",
        "StatementError", "bytes is not a 16-char string",
        "",
        "unknown",
    ),
    Case(
        "unknown_custom_business_error",
        "CustomBusinessError", "something odd",
        "",
        "unknown",
    ),
    Case(
        "unknown_empty_exception_type",
        "", "some message",
        "",
        "unknown",
    ),
]


@pytest.mark.parametrize(
    "case",
    TABLE,
    ids=[c.label for c in TABLE],
)
def test_classify_incident_category(case: Case) -> None:
    result = classify_incident(case.exc, case.msg, case.crash)
    assert result.category == case.expected_category, (
        f"Expected {case.expected_category!r} but got {result.category!r} "
        f"for exc={case.exc!r} msg={case.msg!r} crash={case.crash!r}"
    )


# ---------------------------------------------------------------------------
# Confidence checks for the main categories
# ---------------------------------------------------------------------------

def test_config_issue_is_high_confidence() -> None:
    r = classify_incident("KeyError", "'SECRET_KEY'", "v = os.environ['SECRET_KEY']")
    assert r.confidence == "high"


def test_capacity_is_high_confidence() -> None:
    r = classify_incident("TimeoutError", "connection pool timeout after 30s", "")
    assert r.confidence == "high"


def test_dependency_is_high_confidence() -> None:
    r = classify_incident("ConnectionRefusedError", "[Errno 111] Connection refused", "")
    assert r.confidence == "high"


def test_unknown_generic_timeout_is_low_confidence() -> None:
    r = classify_incident("TimeoutError", "timed out", "")
    assert r.confidence == "low"


# ---------------------------------------------------------------------------
# suggested_action validation: malicious name injection must not appear
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_msg", [
    "environment variable x; rm -rf / is not set",
    "environment variable $(id) is not set",
    "environment variable ../etc/passwd is not set",
    "environment variable 'DROP TABLE users' is not set",
])
def test_config_suggested_action_never_contains_malicious_name(bad_msg: str) -> None:
    r = classify_incident("RuntimeError", bad_msg, "")
    # malicious content must NEVER appear in suggested_action regardless of category
    assert "rm -rf" not in r.suggested_action
    assert "$(id)" not in r.suggested_action
    assert "DROP TABLE" not in r.suggested_action
    assert "../" not in r.suggested_action


def test_config_action_uses_valid_env_var_name() -> None:
    r = classify_incident(
        "KeyError", "'SHOP_PAYMENT_KEY'",
        'return {"payment_key": os.environ["SHOP_PAYMENT_KEY"]}'
    )
    assert "SHOP_PAYMENT_KEY" in r.suggested_action
    assert "No code change needed" in r.suggested_action


def test_config_action_uses_fallback_for_non_uppercase_key() -> None:
    r = classify_incident("KeyError", "'user_id'", "value = os.environ['user_id']")
    # 'user_id' is lowercase -> fallback
    assert "the missing variable" in r.suggested_action


# ---------------------------------------------------------------------------
# never raises on bizarre input
# ---------------------------------------------------------------------------

def test_classify_never_raises_on_none_like_values() -> None:
    r = classify_incident("", "", "")
    assert isinstance(r, Classification)


# ---------------------------------------------------------------------------
# Graph integration: config incident -> escalated, LLM + sandbox NOT called
# ---------------------------------------------------------------------------

ROOT = "mock_services/sample_app"

CONFIG_TRACE = """\
Traceback (most recent call last):
  File "/app/users.py", line 19, in get_user_email
    return os.environ["SHOP_PAYMENT_KEY"]
KeyError: 'SHOP_PAYMENT_KEY'
"""


class _CallTracker:
    """Sandbox/LLM stub that records calls and fails if invoked."""
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list = []

    def complete(self, *args, **kwargs):
        self.calls.append(("complete", args))
        raise AssertionError(f"{self.name}.complete must NOT be called for config incidents")

    def baseline(self):
        self.calls.append(("baseline",))
        raise AssertionError(f"{self.name}.baseline must NOT be called for config incidents")

    def verify(self, *args, **kwargs):
        self.calls.append(("verify", args))
        raise AssertionError(f"{self.name}.verify must NOT be called for config incidents")

    def get_relevant_context(self, *args, **kwargs):
        return []


# users.py with get_user_email at line 17 (matching mock_services/sample_app/users.py)
# and line 19 being `return os.environ["SHOP_PAYMENT_KEY"]`
_CONFIG_USERS_PY = (
    '"""Sample user-service code."""\n'
    "\n"
    "import os\n"
    "\n"
    'USERS = {1: "ana@example.com"}\n'
    "\n"
    "\n"
    "class User:\n"
    "    def __init__(self, email):\n"
    "        self.email = email\n"
    "\n"
    "\n"
    "def find_user(user_id):\n"
    "    if user_id in USERS:\n"
    "        return User(USERS[user_id])\n"
    "    return None\n"
    "\n"
    "\n"
    'def get_user_email(user_id):\n'           # line 19
    '    return os.environ["SHOP_PAYMENT_KEY"]\n'  # line 20
)
# Adjust trace to match: crash at line 20
CONFIG_TRACE_EXACT = """\
Traceback (most recent call last):
  File "/app/users.py", line 20, in get_user_email
    return os.environ["SHOP_PAYMENT_KEY"]
KeyError: 'SHOP_PAYMENT_KEY'
"""


def test_graph_config_incident_escalates_without_llm_or_sandbox(tmp_path) -> None:
    fake_llm = _CallTracker("llm")
    # write a file so analyze_error succeeds (function at line 19-20)
    (tmp_path / "users.py").write_text(_CONFIG_USERS_PY, encoding="utf-8")
    state = run_agent(
        fake_llm,
        None,
        trace=CONFIG_TRACE_EXACT,
        service_name="payment-service",
        repo_root=str(tmp_path),
        path_prefix="/app/",
        classify=True,
    )
    assert state["status"] == "escalated", f"Expected escalated, got {state['status']}"
    reason = state.get("reason", "")
    assert "config_issue" in reason, (
        f"Reason should mention config_issue: {reason!r}"
    )
    assert fake_llm.calls == [], "LLM must not be called for config incidents"


ATTR_TRACE = """\
Traceback (most recent call last):
  File "/app/users.py", line 19, in get_user_email
    return user.email
AttributeError: 'NoneType' object has no attribute 'email'
"""

GOOD_USER_DIFF = """\
--- a/users.py
+++ b/users.py
@@ -17,3 +17,5 @@
 def get_user_email(user_id):
     user = find_user(user_id)
+    if user is None:
+        raise LookupError(f"user {user_id} not found")
     return user.email
"""


def test_graph_code_bug_still_reaches_llm(tmp_path) -> None:
    from aiops.agent.fake_llm import FakeLLM
    llm = FakeLLM({"def get_user_email": [GOOD_USER_DIFF]})
    state = run_agent(
        llm,
        None,
        trace=ATTR_TRACE,
        service_name="user-service",
        repo_root=ROOT,
        path_prefix="/app/",
        classify=True,
    )
    # LLM must have been called for a code_bug
    assert llm.calls, "LLM should have been called for code_bug incident"
    assert state["status"] in ("patch_ready", "verified", "escalated")
    # classification present and correct category
    cls = state.get("classification", {})
    assert cls is not None and cls.get("category") == "code_bug"


# ---------------------------------------------------------------------------
# classify=False leaves everything unchanged
# ---------------------------------------------------------------------------

def test_classify_false_leaves_behaviour_and_summary_unchanged(tmp_path) -> None:
    llm = FakeLLM({"def get_user_email": [GOOD_USER_DIFF]})
    state_off = run_agent(
        llm, None, trace=ATTR_TRACE, service_name="svc",
        repo_root=ROOT, path_prefix="/app/", classify=False,
    )
    summary = summarize(state_off)
    # With classify=False, 'classification' key should be absent or None
    assert summary.get("classification") is None, (
        f"summary['classification'] should be None when classify=False, "
        f"got {summary.get('classification')}"
    )
    # Behaviour: LLM was called, patch produced
    assert llm.calls


# ---------------------------------------------------------------------------
# AIOPS_CLASSIFIER env-var default is off
# ---------------------------------------------------------------------------

def test_aiops_classifier_default_is_off(monkeypatch) -> None:
    monkeypatch.delenv("AIOPS_CLASSIFIER", raising=False)
    value = os.getenv("AIOPS_CLASSIFIER", "off").lower()
    assert value != "on", "AIOPS_CLASSIFIER should default to 'off'"


def test_aiops_classifier_on_enables_classify(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_CLASSIFIER", "on")
    assert os.getenv("AIOPS_CLASSIFIER", "off").lower() == "on"
