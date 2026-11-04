from aiops.agent.graph import run_agent
from aiops.context.extract import (
    extract_function,
    resolve_repo_path,
    select_application_frame,
)
from aiops.context.stacktrace import Frame, parse_python_traceback

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


def test_select_application_frame_skips_windows_stdlib_frame():
    frames = [
        Frame("/app/config.py", 6, "load_config"),
        Frame(r"C:\Python311\Lib\os.py", 679, "__getitem__"),
    ]

    selected = select_application_frame(frames, ROOT)

    assert selected == frames[0]


def test_select_application_frame_skips_linux_stdlib_frame():
    frames = [
        Frame("/app/config.py", 6, "load_config"),
        Frame("/usr/lib/python3.11/os.py", 679, "__getitem__"),
    ]

    selected = select_application_frame(frames, ROOT)

    assert selected == frames[0]


def test_select_application_frame_skips_site_packages():
    frames = [
        Frame("/app/config.py", 6, "load_config"),
        Frame(
            "/venv/lib/python3.11/site-packages/pkg/config.py",
            20,
            "read",
        ),
    ]

    selected = select_application_frame(frames, ROOT)

    assert selected == frames[0]


def test_only_stdlib_frames_escalate_with_application_frame_message():
    class NoCallLLM:
        def complete(self, *_args):
            raise AssertionError("LLM must not be called without an app frame")

    trace = """Traceback (most recent call last):
  File "C:\\Python311\\Lib\\os.py", line 679, in __getitem__
    raise KeyError(key)
KeyError: 'X'"""
    state = run_agent(
        NoCallLLM(), None, trace, "payment-service", ROOT
    )

    assert state["status"] == "escalated"
    assert state["reason"] == "no application frame found in the traceback"


def test_secret_and_traversal_frames_are_not_selected(tmp_path):
    (tmp_path / ".env").write_text("KEY=value", encoding="utf-8")
    frames = [
        Frame("/app/.env", 1, "<module>"),
        Frame("/app/../users.py", 19, "get_user_email"),
    ]

    assert select_application_frame(frames, str(tmp_path)) is None
    assert resolve_repo_path("/app/.env", str(tmp_path)) is None
    assert resolve_repo_path("/app/../users.py", str(tmp_path)) is None

    class NoCallLLM:
        def complete(self, *_args):
            raise AssertionError("LLM must not read a denied path")

    for frame in frames:
        trace = (
            "Traceback (most recent call last):\n"
            f'  File "{frame.file}", line {frame.line}, in {frame.function}\n'
            "ValueError: refused"
        )
        state = run_agent(
            NoCallLLM(), None, trace, "user-service", str(tmp_path)
        )
        assert state["status"] == "escalated"
        assert state["reason"] == "no application frame found in the traceback"


def test_existing_user_and_order_service_frames_are_selected():
    user_frame = Frame("/app/users.py", 19, "get_user_email")
    order_frame = Frame("/app/db.py", 9, "get_connection")

    assert select_application_frame([user_frame], ROOT) == user_frame
    assert select_application_frame([order_frame], ROOT) == order_frame
