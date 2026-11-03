from pathlib import Path

import pytest

from aiops.agent.diff_validator import validate_diff
from aiops.delivery.github_pr import (
    DeliveryError,
    deliver,
    redact,
)
from aiops.delivery.pr_text import make_branch_name, render_pr
from aiops.delivery.slack import build_slack_text, post_slack

SOURCE = Path("mock_services/sample_app/users.py").read_text(
    encoding="utf-8"
)
PATCH = """--- a/users.py
+++ b/users.py
@@ -17,3 +17,5 @@
 def get_user_email(user_id):
     user = find_user(user_id)
+    if user is None:
+        raise LookupError(f"user {user_id} not found")
     return user.email
"""
PATCHED = validate_diff(PATCH, "users.py", SOURCE).new_source


def verified_summary(**updates):
    summary = {
        "status": "verified",
        "patch": PATCH,
        "file_source": SOURCE,
        "final_source": PATCHED,
        "function_range": (17, 19),
        "file": "users.py",
        "function": "get_user_email",
        "signature": "AttributeError: 'NoneType' object has no attribute 'email'",
        "verification": {
            "status": "fix_confirmed",
            "tests_passed": 6,
            "tests_failed": 0,
            "fixed": ["test_users.py::test_missing_user"],
            "newly_failing": [],
            "after_failed_tests": [],
        },
        "baseline_tests_passed": 5,
        "baseline_tests_failed": 1,
        "baseline_failed_tests": ["test_users.py::test_missing_user"],
        "after_failed_tests": [],
        "attempts": 2,
        "tokens_used": 123,
        "model": "fake",
        "runbooks": ["Null result from user lookup"],
    }
    summary.update(updates)
    return summary


INCIDENT = {
    "id": 9,
    "service_name": "user-service",
    "signature": "AttributeError: missing user email",
    "occurrences": 4,
}


class FakeGitHub:
    def __init__(self, content=SOURCE):
        self.content = content
        self.blob_sha = "blob-123"
        self.base = "main"
        self.base_sha = "base-sha"
        self.exists = False
        self.open_pr = None
        self.calls = []
        self.secrets = ["test-github-token"]
        self.pr_url = "https://github.com/example/app/pull/42"

    def default_branch(self):
        self.calls.append(("default_branch",))
        return self.base

    def branch_sha(self, branch):
        self.calls.append(("branch_sha", branch))
        return self.base_sha

    def branch_exists(self, name):
        self.calls.append(("branch_exists", name))
        return self.exists

    def create_branch(self, name, sha):
        self.calls.append(("create_branch", name, sha))
        self.exists = True

    def get_file(self, path, ref):
        self.calls.append(("get_file", path, ref))
        return self.content, self.blob_sha

    def update_file(self, path, message, content, blob_sha, branch):
        self.calls.append(
            ("update_file", path, message, content, blob_sha, branch)
        )

    def find_open_pr(self, head_branch):
        self.calls.append(("find_open_pr", head_branch))
        return self.open_pr

    def create_pr(self, title, body, head, base, draft):
        self.calls.append(("create_pr", title, body, head, base, draft))
        return self.pr_url

    @property
    def writes(self):
        write_names = {"create_branch", "update_file", "create_pr"}
        return [call for call in self.calls if call[0] in write_names]


def test_render_pr_has_evidence_and_safety_text():
    summary = verified_summary()
    summary["verification"]["fixed"] = ["test_users.py::test_fixed @someone"]
    title, body = render_pr(summary, INCIDENT)
    assert title.startswith("AIOps fix: user-service")
    assert PATCH.strip() in body
    assert "- After the change: `6` passed, `0` failed" in body
    assert "- Before the change: `5` passed, `1` failed" in body
    assert "- Fixed by this change:\n  - `test_users.py::test_fixed @\u200bsomeone`" in body
    assert "- Newly failing: none" in body
    assert (
        "- Still failing, also failing before this change "
        "(unrelated to this fix): none"
    ) in body
    assert "Generated automatically by AIOps. Review carefully." in body
    assert "@\u200bsomeone" in body
    assert "@someone" not in body


def test_render_pr_neutralizes_backticks_in_log_text():
    _, body = render_pr(
        verified_summary(),
        {**INCIDENT, "signature": "Failure ```\n@someone"},
    )
    assert "``\u200b`" in body
    assert body.count("```") >= 2
    assert "Failure ```\n@someone" not in body
    assert "@\u200bsomeone" in body


def test_render_pr_uses_inline_values_and_bullet_lists():
    summary = verified_summary(
        file="src/user\nmodule.py",
        function="lookup`user\nemail",
        model="model`name\n@name",
        runbooks=["Null `result\n@name", "Second runbook"],
        baseline_tests_passed=4,
        baseline_tests_failed=2,
        baseline_failed_tests=[
            "test_users.py::test_missing_user",
            "test_config.py::test_old_config_failure",
        ],
        after_failed_tests=["test_config.py::test_old_config_failure"],
    )
    summary["verification"].update({
        "fixed": ["test_users.py::test_missing_user"],
        "newly_failing": ["test_db.py::test_new_failure"],
        "tests_passed": 5,
        "tests_failed": 1,
        "after_failed_tests": ["test_config.py::test_old_config_failure"],
    })
    incident = {
        **INCIDENT,
        "service_name": "user\nservice @name`",
        "signature": "Error`type\n@name",
        "occurrences": "4\n@name`",
    }

    title, body = render_pr(summary, incident)

    assert "user service @\u200bname`" in title
    assert "- Service: `` user service @\u200bname` ``" in body
    assert "- Error signature: ``Error`type @\u200bname``" in body
    assert "- Occurrences: `` 4 @\u200bname` ``" in body
    assert "- File: `src/user module.py`" in body
    assert "- Function: ``lookup`user email``" in body
    assert "- Model: ``model`name @\u200bname``" in body
    assert "- Fixed by this change:\n  - `test_users.py::test_missing_user`" in body
    assert "- Newly failing:\n  - `test_db.py::test_new_failure`" in body
    assert (
        "- Still failing, also failing before this change "
        "(unrelated to this fix):\n"
        "  - `test_config.py::test_old_config_failure`"
    ) in body
    assert (
        "- Runbooks consulted:\n"
        "  - ``Null `result @\u200bname``\n"
        "  - `Second runbook`"
    ) in body
    assert "\n" not in body.split("- Service: ", 1)[1].split("\n", 1)[0]
    assert "@name" not in body


def test_render_pr_empty_lists_render_none_and_diff_fence_is_preserved():
    summary = verified_summary(
        patch="ordinary diff text\n```\nmalicious content\n```",
        runbooks=[],
        baseline_failed_tests=[],
        after_failed_tests=[],
    )
    summary["verification"].update({
        "fixed": [],
        "newly_failing": [],
        "after_failed_tests": [],
    })
    _, body = render_pr(summary, INCIDENT)

    assert "- Fixed by this change: none" in body
    assert "- Newly failing: none" in body
    assert (
        "- Still failing, also failing before this change "
        "(unrelated to this fix): none"
    ) in body
    assert "- Runbooks consulted: none" in body
    assert body.count("```") == 2
    assert "``\u200b`" in body


def test_render_pr_caps_title_and_marks_unverified():
    summary = verified_summary()
    title, _ = render_pr(
        summary,
        {**INCIDENT, "service_name": "s" * 300,
         "signature": "e" * 300},
        unverified=True,
    )
    assert len(title) == 100
    assert title.startswith("[UNVERIFIED] ")


def test_branch_name_is_deterministic_and_safe():
    first = make_branch_name(9, PATCH)
    assert first == make_branch_name(9, PATCH)
    assert first.startswith("aiops/")
    assert set(first) <= set("abcdefghijklmnopqrstuvwxyz0123456789/-")


def test_delivery_creates_branch_updates_file_and_opens_draft_pr():
    client = FakeGitHub()
    result = deliver(verified_summary(), INCIDENT, client)
    branch = make_branch_name(INCIDENT["id"], PATCH)
    assert result.status == "opened"
    assert result.pr_url == client.pr_url
    assert ("create_branch", branch, client.base_sha) in client.calls
    update = next(call for call in client.calls if call[0] == "update_file")
    assert update[1] == "users.py"
    assert update[3] == PATCHED
    assert update[5] == branch
    pr_call = next(call for call in client.calls if call[0] == "create_pr")
    assert pr_call[3:] == (branch, "main", True)
    assert all(
        call[-1] != "main"
        for call in client.writes
        if call[0] in ("update_file", "create_pr")
    )


@pytest.mark.parametrize("status", ["no_patch", "escalated"])
def test_delivery_refuses_non_patch_status(status):
    client = FakeGitHub()
    result = deliver(verified_summary(status=status), INCIDENT, client)
    assert result.status == "refused"
    assert not client.writes


def test_patch_ready_requires_permission_and_marks_pr_unverified():
    summary = verified_summary(status="patch_ready")
    client = FakeGitHub()
    refused = deliver(summary, INCIDENT, client)
    assert refused.status == "refused"
    assert not client.writes

    result = deliver(summary, INCIDENT, client, allow_unverified=True)
    assert result.status == "opened"
    pr_call = next(call for call in client.calls if call[0] == "create_pr")
    assert pr_call[1].startswith("[UNVERIFIED] ")


def test_delivery_refuses_stale_remote_file_before_writes():
    client = FakeGitHub(content=SOURCE + "\n# newer change\n")
    result = deliver(verified_summary(), INCIDENT, client)
    assert result.status == "refused"
    assert "file changed since the analysis" in result.message
    assert not client.writes


def test_existing_branch_returns_open_pr_without_creating_another():
    client = FakeGitHub()
    client.exists = True
    client.open_pr = "https://github.com/example/app/pull/12"
    result = deliver(verified_summary(), INCIDENT, client)
    assert result.status == "already_open"
    assert result.pr_url == client.open_pr
    assert not client.writes


def test_dry_run_has_no_writes():
    client = FakeGitHub()
    result = deliver(verified_summary(), INCIDENT, client, dry_run=True)
    assert result.status == "dry_run"
    assert "users.py" in result.message
    assert not client.writes


@pytest.mark.parametrize(
    "path",
    ["../users.py", "/users.py", r"folder\users.py"],
)
def test_delivery_refuses_unsafe_paths(path):
    patch = PATCH.replace("users.py", path)
    source = SOURCE
    final = validate_diff(patch, path, source).new_source
    summary = verified_summary(
        file=path, patch=patch, final_source=final
    )
    client = FakeGitHub()
    result = deliver(summary, INCIDENT, client)
    assert result.status == "refused"
    assert not client.writes


def test_slack_text_does_not_include_patch_or_trace():
    text = build_slack_text(
        verified_summary(),
        {
            **INCIDENT,
            "signature": "AttributeError: crash trace and private details",
            "trace": "SECRET_TRACE",
        },
        "https://github.com/example/app/pull/42",
    )
    assert "user-service" in text
    assert "AttributeError" in text
    assert "fix_confirmed" in text
    assert "https://github.com/example/app/pull/42" in text
    assert PATCH not in text
    assert "SECRET_TRACE" not in text


def test_slack_failure_does_not_fail_delivery():
    def broken_slack(url):
        raise RuntimeError("Slack is unavailable")

    result = deliver(
        verified_summary(), INCIDENT, FakeGitHub(), slack=broken_slack
    )
    assert result.status == "opened"


def test_empty_slack_url_does_nothing():
    calls = []
    post_slack("", "message", post=lambda *args, **kwargs: calls.append(args))
    assert not calls


def test_slack_post_error_is_swallowed():
    def broken_post(*args, **kwargs):
        raise RuntimeError("offline")

    assert post_slack("https://example.invalid", "text", post=broken_post) is None


def test_redact_removes_secret_from_delivery_error_message():
    token = "test-github-token"
    safe = redact(f"GitHub failed with {token}", [token])
    assert token not in safe
    assert "[REDACTED]" in safe
    error = DeliveryError(safe)
    assert token not in str(error)


def test_delivery_revalidates_patch_before_remote_reads():
    client = FakeGitHub()
    summary = verified_summary(patch="not a diff")
    result = deliver(summary, INCIDENT, client)
    assert result.status == "refused"
    assert "failed validation" in result.message
    assert not client.calls
