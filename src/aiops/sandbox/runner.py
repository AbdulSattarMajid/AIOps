import io
import re
import tarfile
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import docker
from docker.errors import DockerException, ImageNotFound
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

_DEFAULT_COMMAND = [
    "python", "-m", "pytest", "-q", "-p", "no:cacheprovider"
]
_SUMMARY_LINE = re.compile(
    r"(?:\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed)"
    r"(?:,\s*\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed))*"
    r"|no tests ran)\s+in\s+[\d.]+s"
)
_PASSED = re.compile(r"(\d+)\s+passed")
_FAILED = re.compile(r"(\d+)\s+failed")


@dataclass
class SandboxResult:
    status: str
    exit_code: Optional[int]
    output: str
    duration_seconds: float
    tests_passed: Optional[int]
    tests_failed: Optional[int]
    message: str = ""


def validate_files(files: Dict[str, str]) -> None:
    if not isinstance(files, dict):
        raise ValueError("files must be a dictionary of relative paths to text.")
    if len(files) > 200:
        raise ValueError("At most 200 files can be sent to the sandbox.")
    total_bytes = 0
    for path, content in files.items():
        if not isinstance(path, str) or not path:
            raise ValueError("Every file path must be a non-empty relative POSIX path.")
        if "\\" in path or path.startswith("/"):
            raise ValueError(f"Invalid relative POSIX file path: {path!r}.")
        components = path.split("/")
        if any(part in ("", ".", "..") for part in components):
            raise ValueError(f"Invalid relative POSIX file path: {path!r}.")
        if not isinstance(content, str):
            raise ValueError(f"File contents for {path!r} must be text.")
        size = len(content.encode("utf-8"))
        if size > 200 * 1024:
            raise ValueError(f"File {path!r} is larger than 200 KB.")
        total_bytes += size
    if total_bytes > 500 * 1024:
        raise ValueError("The total sandbox file size is larger than 500 KB.")


def build_tar(files: Dict[str, str]) -> bytes:
    validate_files(files)
    archive = io.BytesIO()
    directories = set()
    for path in files:
        parent = path.rpartition("/")[0]
        while parent:
            directories.add(parent)
            parent = parent.rpartition("/")[0]
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for directory in sorted(directories, key=lambda item: (item.count("/"), item)):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.uid = 1000
            info.gid = 1000
            info.mode = 0o755
            info.mtime = 0
            tar.addfile(info)
        for path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.uid = 1000
            info.gid = 1000
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return archive.getvalue()


def parse_pytest_summary(output: str) -> Tuple[Optional[int], Optional[int]]:
    for line in reversed(output.splitlines()):
        if not _SUMMARY_LINE.search(line):
            continue
        passed_match = _PASSED.search(line)
        failed_match = _FAILED.search(line)
        return (
            int(passed_match.group(1)) if passed_match else 0,
            int(failed_match.group(1)) if failed_match else 0,
        )
    return None, None


def _client_or_default(client):
    return client if client is not None else docker.from_env()


def is_docker_available(client=None) -> bool:
    try:
        _client_or_default(client).ping()
        return True
    except Exception:
        return False


def run_tests_in_sandbox(
    files: Dict[str, str],
    command=None,
    image: str = "aiops-sandbox:py311",
    timeout_seconds: int = 60,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 128,
    client=None,
) -> SandboxResult:
    validate_files(files)
    started_at = time.monotonic()
    container = None
    output = ""
    exit_code = None
    timed_out = False
    docker_client = None
    try:
        docker_client = _client_or_default(client)
        container = docker_client.containers.create(
            image,
            command=command or _DEFAULT_COMMAND,
            working_dir="/work",
            user="1000:1000",
            network_mode="none",
            mem_limit=memory,
            memswap_limit=memory,
            nano_cpus=int(cpus * 1_000_000_000),
            pids_limit=pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            labels={"aiops": "sandbox"},
            detach=True,
        )
        # /work must remain writable: Docker's copy API cannot extract onto a
        # read-only root or tmpfs there. Week 5 can harden other paths instead.
        copied = container.put_archive("/work", build_tar(files))
        if copied is False:
            raise RuntimeError("Docker could not copy the test files into the sandbox.")
        container.start()
        try:
            wait_result = container.wait(timeout=timeout_seconds)
            exit_code = wait_result.get("StatusCode")
        except (RequestsTimeout, RequestsConnectionError, TimeoutError):
            timed_out = True
            try:
                container.kill()
            except DockerException:
                pass

        try:
            output = container.logs(tail=300)
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            output = str(output)[-8000:]
        except DockerException as error:
            output = f"Could not read sandbox output: {error}"

        oom_killed = False
        try:
            container.reload()
            state = container.attrs.get("State", {})
            oom_killed = bool(state.get("OOMKilled"))
            if exit_code is None:
                exit_code = state.get("ExitCode")
        except DockerException:
            pass

        passed, failed = parse_pytest_summary(output)
        elapsed = time.monotonic() - started_at
        if timed_out:
            return SandboxResult(
                "timeout", exit_code, output, elapsed, passed, failed,
                f"Tests exceeded the {timeout_seconds}-second timeout.",
            )
        if oom_killed or exit_code == 137:
            return SandboxResult(
                "error", exit_code, output, elapsed, passed, failed,
                f"Sandbox exceeded its memory limit ({memory}).",
            )
        if exit_code == 0:
            status, message = "passed", ""
        elif exit_code == 1:
            status, message = "failed", ""
        elif exit_code == 5:
            status, message = "error", "No tests were collected."
        else:
            status, message = "error", f"Sandbox exited with unexpected code {exit_code}."
        return SandboxResult(
            status, exit_code, output, elapsed, passed, failed, message
        )
    except ImageNotFound:
        elapsed = time.monotonic() - started_at
        return SandboxResult(
            "error", None, output, elapsed, None, None,
            "Sandbox image not found. Run: docker build -t "
            "aiops-sandbox:py311 sandbox_images/python",
        )
    except DockerException:
        elapsed = time.monotonic() - started_at
        return SandboxResult(
            "error", None, output, elapsed, None, None,
            "Docker is not running. Start Docker Desktop and try again.",
        )
    except Exception as error:
        elapsed = time.monotonic() - started_at
        return SandboxResult(
            "error", exit_code, output, elapsed, None, None,
            f"Sandbox could not run tests: {type(error).__name__}: {error}",
        )
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
