"""Dependency image discovery, safety validation, caching, and building."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple, Union

import docker
from docker.errors import BuildError, DockerException, ImageNotFound

from aiops.sandbox.runner import SandboxResult, _client_or_default
from aiops.security.redaction import redact_text

_REQUIREMENTS_FILENAMES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements_test.txt",
    "dev-requirements.txt",
)

_MAX_FILE_BYTES = 50 * 1024  # 50 KB
_MAX_LINES = 200

_FORBIDDEN_PREFIXES = (
    "-e",
    "-r",
    "-c",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "--trusted-host",
)

_FORBIDDEN_SUBSTRINGS = (
    "://",
    "git+",
    "@",
    "./",
    "../",
    "/",
)

_NAME_RE = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_EXTRAS_RE = r"\[\s*[A-Za-z0-9._-]+(?:\s*,\s*[A-Za-z0-9._-]+)*\s*\]"
_CMP_RE = r"(?:===|==|!=|<=|>=|<|>|~=)"
_VERSION_RE = r"[A-Za-z0-9.*+!_-]+"
_SPEC_RE = rf"{_CMP_RE}\s*{_VERSION_RE}"
_SPECS_RE = rf"{_SPEC_RE}(?:\s*,\s*{_SPEC_RE})*"
_MARKER_RE = r";\s*[^;\n]+"
_PLAIN_REQ_RE = re.compile(
    rf"^{_NAME_RE}(?:\s*{_EXTRAS_RE})?(?:\s*{_SPECS_RE})?(?:\s*{_MARKER_RE})?$",
    re.IGNORECASE,
)


class RequirementsSafetyError(ValueError):
    """Raised when requirements fail security or size validation."""
    pass


def validate_requirement_line(line: str) -> None:
    """Validate a single requirements line against safety rules.

    Rejects:
    - lines starting with -e, -r, -c, --index-url, --extra-index-url, --find-links, --trusted-host
    - lines containing ://, git+, @, ./, ../, /
    - lines that do not match standard name + optional extras/specifiers/markers
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return

    for prefix in _FORBIDDEN_PREFIXES:
        if stripped == prefix or stripped.startswith((prefix + " ", prefix + "=")):
            raise RequirementsSafetyError(
                f"Forbidden option {prefix!r} in requirement: {stripped!r}"
            )
    if stripped.startswith(("-e", "-r", "-c", "--index-url", "--extra-index-url", "--find-links", "--trusted-host")):
        raise RequirementsSafetyError(
            f"Forbidden option prefix in requirement: {stripped!r}"
        )

    for sub in _FORBIDDEN_SUBSTRINGS:
        if sub in stripped:
            raise RequirementsSafetyError(
                f"Forbidden substring {sub!r} in requirement: {stripped!r}"
            )

    if not _PLAIN_REQ_RE.match(stripped):
        raise RequirementsSafetyError(
            f"Requirement does not match plain format: {stripped!r}"
        )


def find_requirements_files(repo_root: Union[str, Path]) -> List[Path]:
    """Find supported requirements files in the root of the repository snapshot."""
    root = Path(repo_root).resolve()
    found: List[Path] = []
    for name in _REQUIREMENTS_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            found.append(candidate)
    return found


def normalise_requirements(lines: List[str]) -> str:
    """Combine and normalise requirement lines deterministically, ensuring pytest is present."""
    cleaned: List[str] = []
    has_pytest = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)", stripped)
        if match:
            pkg_name = match.group(1).lower().replace("-", "_")
            if pkg_name == "pytest":
                has_pytest = True
        cleaned.append(stripped)

    if not has_pytest:
        cleaned.append("pytest")

    unique_sorted = sorted(set(cleaned))
    return "\n".join(unique_sorted) + "\n"


def compute_deps_tag(normalised_text: str) -> str:
    """Compute the cached Docker tag from SHA-256 of the normalised requirements text."""
    digest = hashlib.sha256(normalised_text.encode("utf-8")).hexdigest()[:12]
    return f"aiops-deps:{digest}"


def read_and_validate_requirements(repo_root: Union[str, Path]) -> str:
    """Read all requirement files in the repo, validate safety and limits, and return normalised text."""
    files = find_requirements_files(repo_root)
    if not files:
        raise FileNotFoundError("No requirements files found in repository root.")

    total_bytes = 0
    all_lines: List[str] = []
    for file_path in files:
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file_path.read_text(encoding="latin1")

        file_bytes = len(content.encode("utf-8"))
        if file_bytes > _MAX_FILE_BYTES:
            raise RequirementsSafetyError(
                f"Requirements file {file_path.name!r} ({file_bytes} bytes) exceeds 50 KB limit."
            )
        total_bytes += file_bytes
        if total_bytes > _MAX_FILE_BYTES:
            raise RequirementsSafetyError(
                f"Combined requirements size ({total_bytes} bytes) exceeds 50 KB limit."
            )

        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                all_lines.append(stripped)

    if len(all_lines) > _MAX_LINES:
        raise RequirementsSafetyError(
            f"More than {_MAX_LINES} requirement lines found ({len(all_lines)} lines)."
        )

    for line in all_lines:
        validate_requirement_line(line)

    return normalise_requirements(all_lines)


def is_image_in_cache(docker_client, tag: str) -> bool:
    """Check if the dependency image tag already exists in local Docker storage."""
    try:
        docker_client.images.get(tag)
        return True
    except (ImageNotFound, docker.errors.NotFound):
        return False
    except Exception:
        return False


def build_deps_image(
    docker_client,
    requirements_text: str,
    tag: str,
    base_image: str = "aiops-sandbox:py311",
    timeout: int = 300,
) -> Tuple[bool, str]:
    """Build a hardened dependency image using only-binary pip installation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        req_file = Path(tmpdir) / "requirements.txt"
        req_file.write_text(requirements_text, encoding="utf-8")

        dockerfile = Path(tmpdir) / "Dockerfile"
        dockerfile.write_text(
            f"FROM {base_image}\n"
            "USER root\n"
            "COPY requirements.txt /tmp/requirements.txt\n"
            "RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt"
            " && rm /tmp/requirements.txt\n"
            "USER sandbox\n",
            encoding="utf-8",
        )

        container_limits = {
            "memory": 1024 * 1024 * 1024,
            "memswap": 1024 * 1024 * 1024,
        }
        try:
            docker_client.images.build(
                path=tmpdir,
                tag=tag,
                rm=True,
                forcerm=True,
                timeout=timeout,
                container_limits=container_limits,
            )
            return True, ""
        except BuildError as err:
            lines = []
            if getattr(err, "build_log", None):
                for entry in err.build_log:
                    if isinstance(entry, dict):
                        lines.append(entry.get("stream") or entry.get("error") or str(entry))
                    else:
                        lines.append(str(entry))
            output = "".join(lines) if lines else (getattr(err, "msg", None) or str(err))
            return False, output
        except (DockerException, TimeoutError, Exception) as err:
            return False, str(err)


def make_dependencies_unavailable_result(reason: str, output: str = "") -> SandboxResult:
    """Construct a redacted SandboxResult indicating dependencies could not be provided."""
    clean_reason, _ = redact_text(reason)
    clean_output, _ = redact_text(output if output else reason)
    short_output = clean_output[-500:] if len(clean_output) > 500 else clean_output
    short_reason = clean_reason[:500] if len(clean_reason) > 500 else clean_reason
    return SandboxResult(
        status="dependencies_unavailable",
        exit_code=None,
        output=short_output,
        duration_seconds=0.0,
        tests_passed=None,
        tests_failed=None,
        message=short_reason,
    )


def get_or_build_deps_image(
    repo_root: Union[str, Path],
    client=None,
    base_image: str = "aiops-sandbox:py311",
    timeout: int = 300,
) -> Union[str, SandboxResult]:
    """Retrieve existing cached dependency image or safely build a new one."""
    try:
        normalised_text = read_and_validate_requirements(repo_root)
    except RequirementsSafetyError as err:
        return make_dependencies_unavailable_result(
            reason=f"Requirements validation rejected: {err}"
        )
    except Exception as err:
        return make_dependencies_unavailable_result(
            reason=f"Failed to read requirements: {err}"
        )

    tag = compute_deps_tag(normalised_text)
    docker_client = _client_or_default(client)

    if is_image_in_cache(docker_client, tag):
        return tag

    success, build_output = build_deps_image(
        docker_client=docker_client,
        requirements_text=normalised_text,
        tag=tag,
        base_image=base_image,
        timeout=timeout,
    )
    if not success:
        return make_dependencies_unavailable_result(
            reason=f"Dependency image build failed: {build_output[-500:]}",
            output=build_output,
        )

    return tag

