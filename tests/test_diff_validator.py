from pathlib import Path

import pytest

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


def test_dot_b_prefix_is_rejected():
    source = "def charge():\n    return False\n"
    diff = (
        "--- a/demo_shop/payments.py\n"
        "+++ b.demo_shop/payments.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def charge():\n"
        "-    return False\n"
        "+    return True\n"
    )

    result = validate_diff(diff, "demo_shop/payments.py", source)

    assert not result.ok
    assert "file paths" in result.error


@pytest.mark.parametrize(
    ("old_header", "new_header"),
    [
        ("demo_shop/payments.py", "demo_shop/payments.py"),
        ("./demo_shop/payments.py", "./demo_shop/payments.py"),
    ],
)
def test_nested_headers_without_a_b_prefix_are_accepted_and_canonical(
    old_header, new_header
):
    source = "def charge():\n    return False\n"
    diff = (
        f"--- {old_header}\n"
        f"+++ {new_header}\n"
        "@@ -1,2 +1,2 @@\n"
        " def charge():\n"
        "-    return False\n"
        "+    return True\n"
    )

    result = validate_diff(diff, "demo_shop/payments.py", source)

    assert result.ok
    assert result.clean_diff.startswith(
        "--- a/demo_shop/payments.py\n+++ b/demo_shop/payments.py\n"
    )


def test_nested_a_b_headers_are_accepted_and_canonical():
    source = "def charge():\n    return False\n"
    diff = (
        "--- a/demo_shop/payments.py\n"
        "+++ b/demo_shop/payments.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def charge():\n"
        "-    return False\n"
        "+    return True\n"
    )

    result = validate_diff(diff, "demo_shop/payments.py", source)

    assert result.ok
    assert result.clean_diff.startswith(
        "--- a/demo_shop/payments.py\n+++ b/demo_shop/payments.py\n"
    )


@pytest.mark.parametrize(
    "header_path",
    ["/demo_shop/payments.py", "C:/demo_shop/payments.py", "../x.py"],
)
def test_absolute_and_traversal_header_paths_are_rejected(header_path):
    source = "def charge():\n    return False\n"
    diff = (
        "--- a/demo_shop/payments.py\n"
        f"+++ {header_path}\n"
        "@@ -1,2 +1,2 @@\n"
        " def charge():\n"
        "-    return False\n"
        "+    return True\n"
    )

    result = validate_diff(diff, "demo_shop/payments.py", source)

    assert not result.ok
    assert "file paths" in result.error


def test_two_different_header_paths_are_rejected():
    source = "def charge():\n    return False\n"
    diff = (
        "--- a/demo_shop/payments.py\n"
        "+++ b/other/file.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def charge():\n"
        "-    return False\n"
        "+    return True\n"
    )

    result = validate_diff(diff, "demo_shop/payments.py", source)

    assert not result.ok
    assert "file paths" in result.error


def test_dot_b_prefix_for_another_file_is_rejected():
    source = "def charge():\n    return False\n"
    diff = (
        "--- a/demo_shop/payments.py\n"
        "+++ b.other/file.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def charge():\n"
        "-    return False\n"
        "+    return True\n"
    )

    result = validate_diff(diff, "demo_shop/payments.py", source)

    assert not result.ok
    assert "file paths" in result.error


def test_parent_traversal_in_slash_header_is_rejected():
    source = "def charge():\n    return False\n"
    diff = (
        "--- a/demo_shop/payments.py\n"
        "+++ b/../etc/passwd\n"
        "@@ -1,2 +1,2 @@\n"
        " def charge():\n"
        "-    return False\n"
        "+    return True\n"
    )

    result = validate_diff(diff, "demo_shop/payments.py", source)

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
    # Use a non-email replacement so the Week-5A secret scanner does
    # not flag the added line; this test is about hunk ordering only.
    diff = """--- a/users.py
+++ b/users.py
@@ -3 +3 @@
-USERS = {1: "ana@example.com"}
+USERS = {1: "user-ana"}
@@ -19 +19 @@
-    return user.email
+    return user.email.lower()
"""
    result = validate_diff(diff, "users.py", SOURCE)
    assert result.ok
    assert 'USERS = {1: "user-ana"}' in result.new_source
    assert "return user.email.lower()" in result.new_source


# ---------------------------------------------------------------------------
# Secret-in-patch tests (Week 5A)
# ---------------------------------------------------------------------------

def _fake_secret(prefix, fill="A", length=24):
    """Build a secret-looking value at runtime so the repo scanner stays clean."""
    return prefix + fill * length


def test_patch_adding_hardcoded_groq_key_is_rejected():
    """A diff that adds a Groq API key on a '+' line must be rejected outright."""
    groq = _fake_secret("gsk_")
    diff = (
        "--- a/users.py\n+++ b/users.py\n@@ -17,3 +17,4 @@\n"
        " def get_user_email(user_id):\n"
        "     user = find_user(user_id)\n"
        f"+    key = \"{groq}\"\n"
        "     return user.email\n"
    )
    result = validate_diff(diff, "users.py", SOURCE, FUNCTION_RANGE)
    assert not result.ok
    assert "secret" in result.error.lower()
    assert groq not in result.error          # value must not leak into the error message
    assert result.new_source is None         # new_source is never produced


def test_patch_adding_github_token_is_rejected():
    ghp = _fake_secret("ghp_")
    diff = (
        "--- a/users.py\n+++ b/users.py\n@@ -17,3 +17,4 @@\n"
        " def get_user_email(user_id):\n"
        "     user = find_user(user_id)\n"
        f"+    token = \"{ghp}\"\n"
        "     return user.email\n"
    )
    result = validate_diff(diff, "users.py", SOURCE)
    assert not result.ok
    assert "secret" in result.error.lower()
    assert ghp not in result.error


def test_patch_adding_aws_key_is_rejected():
    aws = "AKIA" + "B" * 16
    diff = (
        "--- a/users.py\n+++ b/users.py\n@@ -17,3 +17,4 @@\n"
        " def get_user_email(user_id):\n"
        "     user = find_user(user_id)\n"
        f"+    creds = \"{aws}\"\n"
        "     return user.email\n"
    )
    result = validate_diff(diff, "users.py", SOURCE)
    assert not result.ok
    assert "secret" in result.error.lower()
    assert aws not in result.error


def test_patch_adding_quoted_password_is_rejected():
    """key='value' on an added line is caught by the source_secret_findings scanner."""
    diff = (
        "--- a/users.py\n+++ b/users.py\n@@ -17,3 +17,4 @@\n"
        " def get_user_email(user_id):\n"
        "     user = find_user(user_id)\n"
        "+    password = \"super" + "secret99\"\n"
        "     return user.email\n"
    )
    result = validate_diff(diff, "users.py", SOURCE)
    assert not result.ok
    assert "secret" in result.error.lower()


def test_normal_patch_without_secrets_is_still_accepted():
    """Sanity check: the scanner must not introduce false positives on clean diffs."""
    result = validate_diff(GOOD_DIFF, "users.py", SOURCE, FUNCTION_RANGE)
    assert result.ok
    assert result.new_source is not None


def test_secret_on_removed_line_does_not_block_patch():
    """Removing a secret-looking line is fine — we only block *adding* one."""
    groq = _fake_secret("gsk_", "C")
    # Inject the secret into SOURCE as an existing line so the diff can remove it
    patched_source = SOURCE.replace(
        "    return user.email",
        f'    key = "{groq}"  # existing secret\n    return user.email',
        1,
    )
    diff = (
        "--- a/users.py\n+++ b/users.py\n@@ -17,4 +17,3 @@\n"
        " def get_user_email(user_id):\n"
        "     user = find_user(user_id)\n"
        f"-    key = \"{groq}\"  # existing secret\n"
        "     return user.email\n"
    )
    result = validate_diff(diff, "users.py", patched_source)
    assert result.ok, result.error
    assert groq not in result.new_source
