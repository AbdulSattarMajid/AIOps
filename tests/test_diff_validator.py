from pathlib import Path

from aiops.agent.diff_validator import validate_diff
from aiops.agent.prompts import SYSTEM_PROMPT

USERS_PATH = Path("mock_services/sample_app/users.py")
SOURCE = USERS_PATH.read_text(encoding="utf-8")
FUNCTION_RANGE = (17, 19)

GOOD_DIFF = """--- a/users.py
+++ b/users.py
@@ -17,3 +17,5 @@
 def get_user_email(user_id):
     user = find_user(user_id)
+    if user is None:
+        raise LookupError(f"user {user_id} not found")
     return user.email
"""


def test_valid_diff_applies_and_is_canonical():
    result = validate_diff(GOOD_DIFF, "users.py", SOURCE, FUNCTION_RANGE)
    assert result.ok
    assert "raise LookupError" in result.new_source
    assert result.clean_diff.startswith("--- a/users.py")


def test_wrong_hunk_line_numbers_do_not_prevent_exact_application():
    diff = GOOD_DIFF.replace("@@ -17,3 +17,5 @@", "@@ -100,3 +100,5 @@")
    result = validate_diff(diff, "users.py", SOURCE, FUNCTION_RANGE)
    assert result.ok
    assert "raise LookupError" in result.new_source


def test_bare_at_sign_header_is_accepted():
    diff = GOOD_DIFF.replace("@@ -17,3 +17,5 @@", "@@")
    result = validate_diff(diff, "users.py", SOURCE, FUNCTION_RANGE)
    assert result.ok


def test_at_sign_header_with_function_text_is_accepted():
    diff = GOOD_DIFF.replace(
        "@@ -17,3 +17,5 @@",
        "@@ def get_user_email(user_id):",
    )
    result = validate_diff(diff, "users.py", SOURCE, FUNCTION_RANGE)
    assert result.ok


def test_hunk_without_at_sign_header_is_accepted():
    diff = GOOD_DIFF.replace("@@ -17,3 +17,5 @@\n", "")
    result = validate_diff(diff, "users.py", SOURCE, FUNCTION_RANGE)
    assert result.ok


def test_garbage_line_has_actionable_line_numbered_error():
    diff = GOOD_DIFF.replace(
        "     user = find_user(user_id)\n",
        "     user = find_user(user_id)\n"
        "this is not a valid diff line\n",
    )
    result = validate_diff(diff, "users.py", SOURCE, FUNCTION_RANGE)
    assert not result.ok
    assert result.error.startswith(
        "Line 6 of your diff is not valid: 'this is not a valid diff line'."
    )
    assert "each section must start with a line like '@@ -17,3 +17,5 @@'" in result.error
    assert "every following line must start with a space" in result.error


def test_system_prompt_contains_required_diff_example():
    assert "--- a/example.py" in SYSTEM_PROMPT
    assert "+++ b/example.py" in SYSTEM_PROMPT
    assert "@@ -3,4 +3,5 @@" in SYSTEM_PROMPT
    assert "Every section must start with an @@ line." in SYSTEM_PROMPT
    assert "Do not use any other patch format (no '*** Begin Patch')." in SYSTEM_PROMPT
    assert "Do not include reasoning or commentary" in SYSTEM_PROMPT


def test_fenced_diff_is_accepted():
    result = validate_diff("```diff\n" + GOOD_DIFF + "```", "users.py", SOURCE)
    assert result.ok


def test_context_mismatch_is_rejected():
    bad = GOOD_DIFF.replace("user = find_user(user_id)", "user = find_user(other_id)")
    result = validate_diff(bad, "users.py", SOURCE)
    assert not result.ok
    assert "No matching source context" in result.error


def test_diff_for_another_file_is_rejected():
    result = validate_diff(GOOD_DIFF, "config.py", SOURCE)
    assert not result.ok
    assert "file paths" in result.error


def test_new_file_diff_is_rejected():
    diff = """--- /dev/null
+++ b/users.py
@@ -0,0 +1 @@
+x = 1
"""
    result = validate_diff(diff, "users.py", SOURCE)
    assert not result.ok
    assert "Creating or deleting" in result.error


def test_changes_outside_function_are_rejected():
    diff = """--- a/users.py
+++ b/users.py
@@ -11,4 +11,4 @@
 def find_user(user_id):
     if user_id in USERS:
-        return User(USERS[user_id])
+        return None
     return None
"""
    result = validate_diff(diff, "users.py", SOURCE, FUNCTION_RANGE)
    assert not result.ok
    assert "outside the function" in result.error


def test_invalid_python_result_is_rejected():
    diff = """--- a/users.py
+++ b/users.py
@@ -19 +19 @@
-    return user.email
+    return (
"""
    result = validate_diff(diff, "users.py", SOURCE)
    assert not result.ok
    assert "syntax error" in result.error


def test_missing_or_unchanged_diff_is_rejected():
    assert not validate_diff("nothing here", "users.py", SOURCE).ok
    unchanged = """--- a/users.py
+++ b/users.py
@@ -19 +19 @@
     return user.email
"""
    result = validate_diff(unchanged, "users.py", SOURCE)
    assert not result.ok


def test_more_than_sixty_changed_lines_is_rejected():
    diff = (
        "--- a/users.py\n+++ b/users.py\n@@ -17,1 +17,62 @@\n"
        " def get_user_email(user_id):\n"
        + "".join(f"+    value_{number} = {number}\n" for number in range(61))
    )
    result = validate_diff(diff, "users.py", SOURCE)
    assert not result.ok
    assert "more than 60" in result.error


def test_two_file_diff_is_rejected():
    second = GOOD_DIFF + """--- a/other.py
+++ b/other.py
@@ -1 +1 @@
-old
+new
"""
    result = validate_diff(second, "users.py", SOURCE)
    assert not result.ok
    assert "exactly one file" in result.error


def test_multiple_hunks_apply_in_order():
    diff = """--- a/users.py
+++ b/users.py
@@ -3 +3 @@
-USERS = {1: "ana@example.com"}
+USERS = {1: "updated@example.com"}
@@ -19 +19 @@
-    return user.email
+    return user.email.lower()
"""
    result = validate_diff(diff, "users.py", SOURCE)
    assert result.ok
    assert 'USERS = {1: "updated@example.com"}' in result.new_source
    assert "return user.email.lower()" in result.new_source
