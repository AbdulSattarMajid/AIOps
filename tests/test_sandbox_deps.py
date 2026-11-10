"""Unit tests for sandbox dependency image discovery, validation, caching, and building."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import BuildError, ImageNotFound

from aiops.agent.graph import build_graph, run_agent
from aiops.sandbox.app_sandbox import AppSandbox
from aiops.sandbox.deps import (
    RequirementsSafetyError,
    build_deps_image,
    compute_deps_tag,
    find_requirements_files,
    get_or_build_deps_image,
    normalise_requirements,
    read_and_validate_requirements,
    validate_requirement_line,
)
from aiops.sandbox.evaluate import Verification, evaluate_verification
from aiops.sandbox.runner import SandboxResult


def test_hash_stable_and_changes_when_version_changes():
    """Hash is stable for the same text and changes when a version changes."""
    text1 = ["fastapi==0.100.0", "sqlalchemy>=2.0.0", "uvicorn[standard]>=0.20.0"]
    text2 = ["uvicorn[standard]>=0.20.0", "# comment", "fastapi==0.100.0", "sqlalchemy>=2.0.0"]
    
    norm1 = normalise_requirements(text1)
    norm2 = normalise_requirements(text2)
    assert norm1 == norm2
    tag1 = compute_deps_tag(norm1)
    tag2 = compute_deps_tag(norm2)
    assert tag1 == tag2
    assert tag1.startswith("aiops-deps:")
    assert len(tag1.split(":")[1]) == 12

    # Changing a version changes the hash
    text_changed = ["fastapi==0.104.0", "sqlalchemy>=2.0.0", "uvicorn[standard]>=0.20.0"]
    norm_changed = normalise_requirements(text_changed)
    tag_changed = compute_deps_tag(norm_changed)
    assert tag_changed != tag1
    assert tag_changed.startswith("aiops-deps:")


def test_normalise_ensures_pytest_installed():
    """Ensure pytest is included in normalised requirements if omitted."""
    norm = normalise_requirements(["fastapi==0.100.0"])
    lines = [line.strip() for line in norm.splitlines() if line.strip()]
    assert "pytest" in lines
    assert "fastapi==0.100.0" in lines


def test_combines_multiple_requirements_files(tmp_path):
    """Combines requirements.txt with requirements-dev.txt, requirements_test.txt, dev-requirements.txt."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("black==23.1.0\n", encoding="utf-8")
    (tmp_path / "requirements_test.txt").write_text("httpx>=0.24.0\n", encoding="utf-8")
    (tmp_path / "dev-requirements.txt").write_text("flake8>=6.0.0\n", encoding="utf-8")

    files = find_requirements_files(tmp_path)
    assert len(files) == 4

    norm = read_and_validate_requirements(tmp_path)
    lines = [l.strip() for l in norm.splitlines() if l.strip()]
    assert "fastapi==0.100.0" in lines
    assert "black==23.1.0" in lines
    assert "httpx>=0.24.0" in lines
    assert "flake8>=6.0.0" in lines
    assert "pytest" in lines


@pytest.mark.parametrize(
    "unsafe_line",
    [
        "-e .",
        "-e git+https://github.com/org/repo.git",
        "-r other.txt",
        "-c constraints.txt",
        "--index-url https://pypi.org/simple",
        "--extra-index-url https://internal.pypi.org/simple",
        "--find-links ./wheels",
        "--trusted-host pypi.org",
        "pkg @ https://example.com/pkg-1.0.whl",
        "git+https://github.com/org/repo.git",
        "https://example.com/pkg.tar.gz",
        "./local_package",
        "../parent_package",
        "/absolute/path/pkg",
        "package/with/slashes",
        "bad package name $$$",
        "package == 1.0 == 2.0",
    ],
)
def test_each_unsafe_line_type_is_rejected(unsafe_line):
    """Each unsafe line type from item 2 is rejected."""
    with pytest.raises(RequirementsSafetyError):
        validate_requirement_line(unsafe_line)


def test_requirements_file_size_and_count_limits(tmp_path):
    """Files over 50 KB or with more than 200 requirement lines are rejected."""
    # More than 200 lines
    req_file = tmp_path / "requirements.txt"
    lines = [f"pkg{i}==1.0" for i in range(205)]
    req_file.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(RequirementsSafetyError, match="More than 200 requirement lines"):
        read_and_validate_requirements(tmp_path)

    # File > 50 KB
    large_req = tmp_path / "requirements.txt"
    large_content = "a" * (51 * 1024)
    large_req.write_text(large_content, encoding="utf-8")
    with pytest.raises(RequirementsSafetyError, match="exceeds 50 KB limit"):
        read_and_validate_requirements(tmp_path)


def test_cache_hit_skips_build(tmp_path):
    """Cache hit skips the build; never rebuilds the same hash."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    mock_client = MagicMock()
    # Cache hit: image exists
    mock_image = MagicMock()
    mock_client.images.get.return_value = mock_image

    tag = get_or_build_deps_image(repo_root=tmp_path, client=mock_client)
    assert isinstance(tag, str)
    assert tag.startswith("aiops-deps:")
    mock_client.images.get.assert_called_once_with(tag)
    mock_client.images.build.assert_not_called()


def test_cache_miss_builds_once(tmp_path):
    """Cache miss builds once with proper arguments."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.images.get.side_effect = ImageNotFound("not found")
    mock_client.images.build.return_value = (MagicMock(), [])

    tag = get_or_build_deps_image(repo_root=tmp_path, client=mock_client)
    assert isinstance(tag, str)
    assert tag.startswith("aiops-deps:")
    mock_client.images.get.assert_called_once_with(tag)
    mock_client.images.build.assert_called_once()
    kwargs = mock_client.images.build.call_args.kwargs
    assert kwargs["tag"] == tag
    assert kwargs["rm"] is True
    assert kwargs["forcerm"] is True
    assert "container_limits" in kwargs


def test_build_failure_returns_dependencies_unavailable(tmp_path):
    """Build failure returns sandbox status dependencies_unavailable with redacted reason."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.images.get.side_effect = ImageNotFound("not found")
    mock_client.images.build.side_effect = BuildError(
        reason="Pip installation error: Bearer secret_token_12345 failed",
        build_log=[{"stream": "ERROR: No matching distribution found for Bearer secret_token_12345\n"}],
    )

    result = get_or_build_deps_image(repo_root=tmp_path, client=mock_client)
    assert isinstance(result, SandboxResult)
    assert result.status == "dependencies_unavailable"
    assert "secret_token_12345" not in result.message
    assert "secret_token_12345" not in result.output
    assert "<REDACTED_" in result.message or "<REDACTED_" in result.output or "Dependency image build failed" in result.message


def test_build_timeout_returns_dependencies_unavailable(tmp_path):
    """Build timeout returns dependencies_unavailable without crashing."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.images.get.side_effect = ImageNotFound("not found")
    mock_client.images.build.side_effect = TimeoutError("Docker build timed out after 300 seconds")

    result = get_or_build_deps_image(repo_root=tmp_path, client=mock_client)
    assert isinstance(result, SandboxResult)
    assert result.status == "dependencies_unavailable"
    assert "timed out" in result.message.lower() or "timeout" in result.message.lower()


def test_repo_without_requirements_uses_base_image(tmp_path, monkeypatch):
    """Repo without requirements keeps old behaviour, deps image is never used."""
    monkeypatch.setenv("AIOPS_SANDBOX_BUILD_DEPS", "on")
    mock_client = MagicMock()
    sandbox = AppSandbox(tmp_path, client=mock_client)

    err = sandbox._ensure_image()
    assert err is None
    assert sandbox._resolved_image == "aiops-sandbox:py311"
    mock_client.images.get.assert_not_called()
    mock_client.images.build.assert_not_called()


def test_env_var_off_nothing_new_happens(tmp_path, monkeypatch):
    """When AIOPS_SANDBOX_BUILD_DEPS is off, nothing new happens."""
    monkeypatch.setenv("AIOPS_SANDBOX_BUILD_DEPS", "off")
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    mock_client = MagicMock()
    sandbox = AppSandbox(tmp_path, client=mock_client)

    err = sandbox._ensure_image()
    assert err is None
    assert sandbox._resolved_image == "aiops-sandbox:py311"
    mock_client.images.get.assert_not_called()
    mock_client.images.build.assert_not_called()


def test_run_command_has_hardening_with_deps_image(tmp_path, monkeypatch):
    """The run command still has network disabled, non-root, read-only, tmpfs when using deps image."""
    monkeypatch.setenv("AIOPS_SANDBOX_BUILD_DEPS", "on")
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    mock_client = MagicMock()
    mock_client.images.get.return_value = MagicMock()  # cache hit
    mock_container = MagicMock()
    mock_client.containers.create.return_value = mock_container

    # Mock staging and execution to avoid needing real Docker
    with patch("aiops.sandbox.runner._stage_archive"), \
         patch("aiops.sandbox.runner._format_exec_output", return_value="1 passed in 0.1s"):
        mock_container.client.api.exec_create.return_value = {"Id": "exec_123"}
        mock_container.client.api.exec_start.return_value = [(b"1 passed in 0.1s", None)]
        mock_container.client.api.exec_inspect.return_value = {"ExitCode": 0}
        mock_container.attrs = {"State": {"ExitCode": 0, "OOMKilled": False}}

        sandbox = AppSandbox(tmp_path, client=mock_client)
        result = sandbox.baseline()
        assert result.status == "passed"

    mock_client.containers.create.assert_called_once()
    create_kwargs = mock_client.containers.create.call_args.kwargs
    assert create_kwargs["network_mode"] == "none"
    assert create_kwargs["user"] == "1000:1000"
    assert create_kwargs["read_only"] is True
    assert "/work" in create_kwargs["tmpfs"]
    assert "/tmp" in create_kwargs["tmpfs"]
    assert "nosuid" in create_kwargs["tmpfs"]["/work"]
    assert "noexec" in create_kwargs["tmpfs"]["/work"]
    create_args = mock_client.containers.create.call_args.args
    image_used = create_kwargs.get("image") or (create_args[0] if create_args else None)
    assert image_used is not None
    assert image_used.startswith("aiops-deps:")


def test_evaluate_verification_dependencies_unavailable():
    """evaluate_verification maps dependencies_unavailable status correctly."""
    baseline = SandboxResult(
        status="dependencies_unavailable",
        exit_code=None,
        output="",
        duration_seconds=0.0,
        tests_passed=None,
        tests_failed=None,
        message="Requirements rejected",
    )
    after = SandboxResult(
        status="dependencies_unavailable",
        exit_code=None,
        output="",
        duration_seconds=0.0,
        tests_passed=None,
        tests_failed=None,
        message="Requirements rejected",
    )
    ver = evaluate_verification(baseline, after, "app")
    assert ver.status == "dependencies_unavailable"
    assert "Requirements rejected" in ver.message


def test_agent_graph_handles_dependencies_unavailable():
    """Agent graph gracefully handles dependencies_unavailable without crashing and ends not-verified."""
    class MockDepsUnavailableSandbox:
        def baseline(self):
            return SandboxResult(
                status="dependencies_unavailable",
                exit_code=None,
                output="Build failed: incompatible dependencies",
                duration_seconds=0.1,
                tests_passed=None,
                tests_failed=None,
                message="Build failed: incompatible dependencies",
            )

        def verify(self, rel_path, new_source):
            return self.baseline()

    class FakeStore:
        def get_relevant_context(self, *args, **kwargs):
            return []

    class FakeLLM:
        def invoke(self, *args, **kwargs):
            return (
                '```diff\n'
                '--- a/app.py\n'
                '+++ b/app.py\n'
                '@@ -1,2 +1,2 @@\n'
                '-old\n'
                '+new\n'
                '```'
            )

    sandbox = MockDepsUnavailableSandbox()
    store = FakeStore()
    llm = FakeLLM()

    state = run_agent(
        llm,
        store,
        trace='  File "/app/users.py", line 19, in get_user_email\n    return user.email',
        service_name="user-service",
        repo_root="mock_services/sample_app",
        sandbox=sandbox,
    )

    assert state.get("sandbox_ok") is False
    assert state.get("verification").status == "dependencies_unavailable"
    assert state.get("status") in ("patch_ready", "escalated")
    assert state.get("status") != "verified"
    assert state.get("verification").status != "fix_confirmed"

