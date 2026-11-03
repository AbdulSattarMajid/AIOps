import io
import tarfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest

from aiops.sandbox.runner import (
    build_tar,
    is_docker_available,
    parse_pytest_summary,
    run_tests_in_sandbox,
)
from aiops.sandbox.verify import collect_app_files, run_app_tests, verify_patch

IMAGE = "aiops-sandbox:py311"
APP_DIR = Path("mock_services/sample_app")


def test_sandbox_rejects_invalid_paths():
    for path in ("/absolute.py", "../secret.py", "folder\\file.py", ""):
        with pytest.raises(ValueError):
            run_tests_in_sandbox({path: "pass"}, client=object())


def test_sandbox_rejects_too_many_and_oversized_files():
    with pytest.raises(ValueError, match="200 files"):
        run_tests_in_sandbox(
            {f"file_{index}.py": "" for index in range(201)},
            client=object(),
        )
    with pytest.raises(ValueError, match="200 KB"):
        run_tests_in_sandbox(
            {"large.py": "x" * (200 * 1024 + 1)},
            client=object(),
        )
    with pytest.raises(ValueError, match="500 KB"):
        run_tests_in_sandbox(
            {f"file_{index}.py": "x" * (100 * 1024) for index in range(6)},
            client=object(),
        )


def test_parse_pytest_summary_counts_passed_and_failed():
    assert parse_pytest_summary("4 passed, 2 failed in 0.13s") == (4, 2)
    assert parse_pytest_summary("6 passed in 0.13s") == (6, 0)
    assert parse_pytest_summary("2 failed in 0.13s") == (0, 2)
    assert parse_pytest_summary("no tests ran in 0.01s") == (0, 0)
    assert parse_pytest_summary("some non-summary output") == (None, None)


def test_build_tar_sets_sandbox_uid_gid_and_permissions():
    with tarfile.open(fileobj=io.BytesIO(build_tar({"pkg/test.py": "pass"}))) as tar:
        member = tar.getmember("pkg/test.py")
        assert member.uid == 1000
        assert member.gid == 1000
        assert member.mode == 0o644
        assert tar.getmember("pkg").uid == 1000


def test_docker_creation_error_returns_friendly_result():
    class Containers:
        def create(self, *args, **kwargs):
            raise docker.errors.DockerException("daemon unavailable")

    client = SimpleNamespace(containers=Containers())
    result = run_tests_in_sandbox({"test_ok.py": "def test_ok(): pass"},
                                  client=client)
    assert result.status == "error"
    assert "Docker is not running" in result.message


def test_collect_app_files_skips_cache_symlinks_and_secrets(tmp_path):
    app = tmp_path / "app"
    cache = app / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "cached.py").write_text("raise RuntimeError()")
    (app / "app.py").write_text("VALUE = 1")
    (app / ".env").write_text("SECRET=never copy")
    (app / "private.pem").write_text("never copy")
    (app / "private.key").write_text("never copy")
    (app / "too_big.py").write_text("x" * (200 * 1024 + 1))
    link = app / "linked.py"
    try:
        link.symlink_to(app / "app.py")
    except OSError:
        pass
    files = collect_app_files(str(app))
    assert files == {"app.py": "VALUE = 1"}


def test_collect_app_files_caps_file_count(tmp_path):
    for index in range(201):
        (tmp_path / f"file_{index}.py").write_text("pass")
    with pytest.raises(ValueError, match="200 Python files"):
        collect_app_files(str(tmp_path))


def _docker_client():
    if not is_docker_available():
        return None
    client = docker.from_env()
    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        return None
    return client


_CLIENT = _docker_client()
_DOCKER_SKIP = pytest.mark.skipif(
    _CLIENT is None,
    reason="Docker daemon or aiops-sandbox:py311 image is unavailable",
)


@_DOCKER_SKIP
def test_docker_sandbox_passes():
    result = run_tests_in_sandbox(
        {"test_sample.py": "def test_ok():\n    assert 1 + 1 == 2\n"},
        client=_CLIENT,
    )
    assert result.status == "passed"
    assert (result.tests_passed, result.tests_failed) == (1, 0)


@_DOCKER_SKIP
def test_docker_sandbox_reports_failed_test_counts():
    result = run_tests_in_sandbox(
        {"test_sample.py": "def test_bad():\n    assert False\n"},
        client=_CLIENT,
    )
    assert result.status == "failed"
    assert (result.tests_passed, result.tests_failed) == (0, 1)


@_DOCKER_SKIP
def test_docker_sandbox_times_out():
    result = run_tests_in_sandbox(
        {
            "test_sample.py": (
                "import time\n"
                "def test_slow():\n"
                "    time.sleep(30)\n"
            )
        },
        timeout_seconds=3,
        client=_CLIENT,
    )
    assert result.status == "timeout"


@_DOCKER_SKIP
def test_docker_sandbox_has_no_network():
    code = textwrap.dedent("""\
        import socket

        def test_network_is_disabled():
            try:
                socket.create_connection(("1.1.1.1", 53), timeout=2)
            except OSError:
                return
            assert False, "sandbox unexpectedly connected to the network"
    """)
    result = run_tests_in_sandbox({"test_network.py": code}, client=_CLIENT)
    assert result.status == "passed"
    assert result.tests_passed == 1


@_DOCKER_SKIP
def test_docker_sandbox_runs_as_non_root():
    code = "import os\ndef test_not_root():\n    assert os.getuid() != 0\n"
    result = run_tests_in_sandbox({"test_user.py": code}, client=_CLIENT)
    assert result.status == "passed"
    assert result.tests_passed == 1


@_DOCKER_SKIP
def test_docker_sandbox_enforces_memory_limit():
    code = (
        "def test_memory_limit():\n"
        "    memory = bytearray(400 * 1024 * 1024)\n"
        "    assert len(memory) == 400 * 1024 * 1024\n"
    )
    result = run_tests_in_sandbox(
        {"test_memory.py": code},
        memory="128m",
        client=_CLIENT,
    )
    assert result.status == "error"
    assert "memory limit" in result.message


@_DOCKER_SKIP
def test_docker_sandbox_removes_all_labeled_containers():
    result = run_tests_in_sandbox(
        {"test_cleanup.py": "def test_ok():\n    assert True\n"},
        client=_CLIENT,
    )
    assert result.status == "passed"
    assert _CLIENT.containers.list(
        all=True, filters={"label": "aiops=sandbox"}
    ) == []


@_DOCKER_SKIP
def test_run_app_tests_returns_expected_original_failures():
    result = run_app_tests(str(APP_DIR), client=_CLIENT)
    assert result.status == "failed"
    assert (result.tests_passed, result.tests_failed) == (4, 2)


@_DOCKER_SKIP
def test_verify_patch_reduces_sample_app_failures():
    source = (APP_DIR / "users.py").read_text(encoding="utf-8")
    patched = source.replace(
        "    return user.email\n",
        '    if user is None:\n'
        '        raise LookupError(f"user {user_id} not found")\n'
        "    return user.email\n",
    )
    result = verify_patch(str(APP_DIR), "users.py", patched, client=_CLIENT)
    assert result.status == "failed"
    assert (result.tests_passed, result.tests_failed) == (5, 1)


def test_verify_patch_rejects_unknown_file():
    with pytest.raises(ValueError, match="not an existing app file"):
        verify_patch(str(APP_DIR), "unknown.py", "pass", client=object())
