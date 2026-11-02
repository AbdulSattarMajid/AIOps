from aiops.context.extract import extract_function, resolve_repo_path
from aiops.context.stacktrace import parse_python_traceback

ROOT = "mock_services/sample_app"

TRACE = """Traceback (most recent call last):
  File "/app/main.py", line 10, in handler
    get_user_email(5)
  File "/app/users.py", line 19, in get_user_email
    return user.email
AttributeError: 'NoneType' object has no attribute 'email'
request_id=1234"""


def test_parse_trace_finds_frames_and_error():
    parsed = parse_python_traceback(TRACE)
    assert parsed.exception_type == "AttributeError"
    assert "NoneType" in parsed.message
    assert len(parsed.frames) == 2
    assert parsed.crash_frame.function == "get_user_email"
    assert parsed.crash_frame.line == 19


def test_extract_returns_only_the_broken_function():
    ctx = extract_function("/app/users.py", 19, ROOT)
    assert ctx.function == "get_user_email"
    assert "return user.email" in ctx.source
    assert "def find_user" not in ctx.source  # other functions are left out


def test_extract_method_inside_class_has_qualified_name():
    ctx = extract_function("/app/db.py", 9, ROOT)
    assert ctx.function == "OrderRepository.get_connection"


def test_line_outside_any_function_returns_none():
    assert extract_function("/app/users.py", 1, ROOT) is None


def test_path_traversal_is_refused():
    assert resolve_repo_path("/app/../../../etc/passwd", ROOT) is None
    assert extract_function("/app/../users.py", 19, ROOT) is None


def test_secret_files_are_refused(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "server.pem").write_text("x")
    assert resolve_repo_path("/app/.env", str(tmp_path)) is None
    assert resolve_repo_path("/app/server.pem", str(tmp_path)) is None


def test_missing_file_returns_none():
    assert extract_function("/app/nope.py", 3, ROOT) is None
