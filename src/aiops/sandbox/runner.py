import io
import re
import tarfile
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import docker
from docker.errors import DockerException, ImageNotFound

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
_FAILED_TEST = re.compile(r"^(?:FAILED|ERROR) (\S+)")


@dataclass
class SandboxResult:
    status: str
    exit_code: Optional[int]
    output: str
    duration_seconds: float
    tests_passed: Optional[int]
    tests_failed: Optional[int]
    message: str = ""
    failed_tests: List[str] = field(default_factory=list)


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


def parse_failed_tests(output: str) -> List[str]:
    failed = []
    for line in output.splitlines():
        match = _FAILED_TEST.match(line)
        if match:
            failed.append(match.group(1))
    return failed


def _format_exec_output(output) -> str:
    if isinstance(output, tuple):
        output = b"".join(part for part in output if part)
    if isinstance(output, bytes):
        encoded = output[-(64 * 1024):]
    else:
        encoded = str(output).encode("utf-8", errors="replace")[-(64 * 1024):]
    return encoded.decode("utf-8", errors="replace")


def _stage_archive(container, archive: bytes) -> None:
    api = container.client.api
    exec_info = api.exec_create(
        container.id,
        [
            "python",
            "-c",
            "import sys, tarfile; "
            "tarfile.open(fileobj=sys.stdin.buffer, mode='r|').extractall('/work')",
        ],
        stdin=True,
        stdout=True,
        stderr=True,
        user="1000:1000",
        workdir="/work",
    )
    stream = api.exec_start(exec_info["Id"], socket=True, tty=False)
    try:
        stream.sendall(archive)
    finally:
        stream.close()

    while True:
        state = api.exec_inspect(exec_info["Id"])
        if not state["Running"]:
            if state["ExitCode"] != 0:
                raise RuntimeError(
                    "Docker could not extract the test files into the sandbox."
                )
            return
        time.sleep(0.01)


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
            command=["python", "-c", "import time; time.sleep(3600)"],
            working_dir="/work",
            user="1000:1000",
            network_mode="none",
            mem_limit=memory,
            memswap_limit=memory,
            nano_cpus=int(cpus * 1_000_000_000),
            pids_limit=64,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            read_only=True,
            tmpfs={
                "/work": "rw,nosuid,noexec,size=64m,uid=1000,gid=1000,mode=0755",
                "/tmp": "rw,nosuid,noexec,size=64m,uid=1000,gid=1000,mode=1777",
            },
            ulimits=[
                docker.types.Ulimit(name="nofile", soft=1024, hard=1024),
                docker.types.Ulimit(name="nproc", soft=64, hard=64),
            ],
            privileged=False,
            labels={"aiops": "sandbox"},
            detach=True,
        )
        # Docker rejects put_archive on a read-only root, even when /work is tmpfs.
        # Stream the validated archive to a Python extractor running on /work.
        # Run tests only after staging, and kill the idle container if they time out.
        container.start()
        _stage_archive(container, build_tar(files))

        exec_result = []
        exec_error = []
        exec_finished = threading.Event()

        def run_exec():
            try:
                api = container.client.api
                exec_info = api.exec_create(
                    container.id,
                    command or _DEFAULT_COMMAND,
                    stdout=True,
                    stderr=True,
                    user="1000:1000",
                    workdir="/work",
                )
                captured_output = bytearray()
                for stdout, stderr in api.exec_start(
                    exec_info["Id"], stream=True, demux=True
                ):
                    for chunk in (stdout, stderr):
                        if chunk:
                            captured_output.extend(chunk)
                            if len(captured_output) > 64 * 1024:
                                del captured_output[:len(captured_output) - 64 * 1024]
                exit_code = api.exec_inspect(exec_info["Id"]).get("ExitCode")
                exec_result.append((exit_code, bytes(captured_output)))
            except Exception as error:
                exec_error.append(error)
            finally:
                exec_finished.set()

        exec_thread = threading.Thread(target=run_exec, daemon=True)
        exec_thread.start()
        if not exec_finished.wait(timeout_seconds):
            timed_out = True
            try:
                container.kill()
            except DockerException:
                pass
            exec_finished.wait(1)
        elif exec_error:
            raise exec_error[0]
        else:
            exit_code, captured_output = exec_result[0]
            output = _format_exec_output(captured_output)

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
        failed_tests = parse_failed_tests(output)
        elapsed = time.monotonic() - started_at
        if timed_out:
            return SandboxResult(
                "timeout", exit_code, output, elapsed, passed, failed,
                f"Tests exceeded the {timeout_seconds}-second timeout.",
                failed_tests,
            )
        if oom_killed or exit_code == 137:
            return SandboxResult(
                "error", exit_code, output, elapsed, passed, failed,
                f"Sandbox exceeded its memory limit ({memory}).", failed_tests,
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
            status, exit_code, output, elapsed, passed, failed, message,
            failed_tests,
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
            except DockerException:
                pass
