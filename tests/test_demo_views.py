from demo_ui.client import ApiResult
from demo_ui.views import (
    api_error_message,
    delivery_button_enabled,
    empty_state,
    format_analysis,
    incident_detail,
    queue_status,
    shape_incidents,
    summarize_redaction,
)


def test_incident_table_shapes_only_api_fields_and_sorts_newest_first():
    incidents = [
        {
            "id": 1,
            "service_name": "user-service",
            "error_class": "ValueError",
            "count": 2,
            "first_seen": "2026-11-01T10:00:00Z",
            "last_seen": "2026-11-01T10:00:00Z",
            "raw_secret": "must not render",
        },
        {
            "id": 2,
            "service_name": "payment-service",
            "error_class": "KeyError",
            "count": 1,
            "first_seen": "2026-11-02T10:00:00Z",
            "last_seen": "2026-11-02T10:00:00Z",
            "analysis_state": "verified",
        },
    ]
    shaped = shape_incidents(incidents)
    assert [item["id"] for item in shaped] == [2, 1]
    assert "raw_secret" not in repr(shaped)
    assert shaped[0]["analysis_state"] == "verified"


def test_empty_incident_state_is_friendly():
    assert empty_state([]) == "no incidents yet, run the target app"
    assert empty_state([{"id": 1}]) == ""


def test_detail_and_redaction_summary_use_only_safe_fields():
    detail = incident_detail({
        "id": 4,
        "sample_trace": "ValueError",
        "service_name": "demo",
        "api_key": "do-not-show",
        "redaction": {"secret": 2, "email": 1},
    })
    assert detail == {
        "id": 4,
        "sample_trace": "ValueError",
        "service_name": "demo",
        "redaction": {"secret": 2, "email": 1},
    }
    assert summarize_redaction(detail["redaction"]) == "email: 1, secret: 2"
    assert "do-not-show" not in repr(detail)


def test_api_and_queue_error_messages_are_friendly():
    assert api_error_message(ApiResult(False, None, "key not configured")) == (
        "key not configured"
    )
    assert api_error_message(ApiResult(False, 401, "unauthorized")) == (
        "unauthorized"
    )
    assert api_error_message(ApiResult(False, None, "backend unavailable")) == (
        "backend unavailable"
    )
    assert queue_status(ApiResult(
        True,
        200,
        "ok",
        {"stream_length": 2, "pending_count": 1, "dead_letter_count": 0},
    )) == {
        "stream_length": 2,
        "pending_count": 1,
        "dead_letter_count": 0,
        "message": "",
    }


def test_delivery_is_enabled_only_for_verified_status():
    assert not delivery_button_enabled({"status": "patch_ready"})
    assert not delivery_button_enabled({"verification": {"status": "failed"}})
    assert delivery_button_enabled({
        "verification": {"status": "verified"}
    })
    assert delivery_button_enabled({
        "verification": {"status": "fix_confirmed"}
    })


def test_analysis_format_extracts_diff_rejections_and_test_evidence():
    formatted = format_analysis({
        "status": "verified",
        "attempts": 2,
        "tokens_used": 123,
        "patch": "--- a/users.py\n+++ b/users.py\n",
        "baseline_tests_passed": 4,
        "baseline_tests_failed": 2,
        "history": [
            {"attempt": 1, "stage": "diff", "ok": False, "error": "bad hunk"}
        ],
        "verification": {
            "status": "verified",
            "tests_passed": 6,
            "tests_failed": 0,
            "fixed": ["test_users.py::test_missing_user"],
            "newly_failing": [],
            "still_failing": [],
        },
    })
    assert formatted["diff"].startswith("--- a/users.py")
    assert formatted["rejected_attempts"] == [{
        "attempt": 1,
        "stage": "diff",
        "error": "bad hunk",
    }]
    assert formatted["evidence"] == {
        "before_passed": 4,
        "before_failed": 2,
        "after_passed": 6,
        "after_failed": 0,
        "fixed": ["test_users.py::test_missing_user"],
        "newly_failing": [],
        "still_failing": [],
    }


def test_formatted_analysis_does_not_render_secret_fields():
    formatted = format_analysis({
        "status": "patch_ready",
        "api_key": "secret-api",
        "github_token": "secret-token",
        "history": [{
            "attempt": 1,
            "stage": "diff",
            "ok": False,
            "error": "invalid diff",
            "reply": "secret raw response",
        }],
    })
    rendered = repr(formatted)
    assert "secret-api" not in rendered
    assert "secret-token" not in rendered
    assert "secret raw response" not in rendered
