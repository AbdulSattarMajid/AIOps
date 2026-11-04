import textwrap

import docker
import pytest

from aiops.sandbox.runner import is_docker_available, run_tests_in_sandbox

IMAGE = "aiops-sandbox:py311"


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


def _assert_no_sandbox_containers():
    assert _CLIENT.containers.list(
        all=True, filters={"label": "aiops=sandbox"}
    ) == []


@_DOCKER_SKIP
def test_sandbox_container_configuration_is_hardened():
    inspected = []

    class Containers:
        def create(self, *args, **kwargs):
            container = _CLIENT.containers.create(*args, **kwargs)
            container.reload()
            inspected.append(container.attrs)
            return container

    result = run_tests_in_sandbox(
        {"test_config.py": "def test_config():\n    assert True\n"},
        client=type("Client", (), {"containers": Containers()})(),
    )

    assert result.status == "passed"
    assert len(inspected) == 1
    config = inspected[0]
    host_config = config["HostConfig"]
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["PidsLimit"] == 64
    assert host_config["Memory"] == 256 * 1024 * 1024
    assert host_config["CapDrop"] == ["ALL"]
    assert host_config["NetworkMode"] == "none"
    assert "no-new-privileges" in host_config["SecurityOpt"]
    assert config["Config"]["User"] == "1000:1000"
    assert host_config["Privileged"] is False
    assert not host_config.get("Binds")
    ulimits = {limit["Name"]: limit for limit in host_config["Ulimits"]}
    assert ulimits["nofile"]["Soft"] == ulimits["nofile"]["Hard"] == 1024
    assert ulimits["nproc"]["Soft"] == ulimits["nproc"]["Hard"] == 64
    for path in ("/work", "/tmp"):
        options = host_config["Tmpfs"][path]
        assert "nosuid" in options
        assert "size=64m" in options


@_DOCKER_SKIP
def test_sandbox_read_only_root_and_writable_scratch():
    code = textwrap.dedent("""\
        from pathlib import Path

        def test_filesystem_permissions():
            try:
                Path("/etc/aiops-sandbox-write-test").write_text("blocked")
            except OSError:
                pass
            else:
                raise AssertionError("root filesystem unexpectedly writable")

            Path("/work/scratch.txt").write_text("work")
            Path("/tmp/scratch.txt").write_text("tmp")
            assert Path("/work/scratch.txt").read_text() == "work"
            assert Path("/tmp/scratch.txt").read_text() == "tmp"
    """)
    result = run_tests_in_sandbox({"test_filesystem.py": code}, client=_CLIENT)
    assert result.status == "passed"
    assert result.tests_passed == 1


@_DOCKER_SKIP
def test_sandbox_runs_as_non_root():
    code = "import os\ndef test_not_root():\n    assert os.getuid() != 0\n"
    result = run_tests_in_sandbox({"test_user.py": code}, client=_CLIENT)
    assert result.status == "passed"
    assert result.tests_passed == 1


@_DOCKER_SKIP
def test_sandbox_cannot_connect_to_network():
    code = textwrap.dedent("""\
        import socket

        def test_network_is_disabled():
            try:
                socket.create_connection(("1.1.1.1", 53), timeout=2)
            except OSError:
                return
            raise AssertionError("sandbox unexpectedly connected to the network")
    """)
    result = run_tests_in_sandbox({"test_network.py": code}, client=_CLIENT)
    assert result.status == "passed"
    assert result.tests_passed == 1


@_DOCKER_SKIP
def test_sandbox_kills_container_after_timeout():
    result = run_tests_in_sandbox(
        {"test_loop.py": "def test_loop():\n    while True:\n        pass\n"},
        timeout_seconds=2,
        client=_CLIENT,
    )
    assert result.status == "timeout"


@_DOCKER_SKIP
def test_sandbox_leaves_no_containers():
    result = run_tests_in_sandbox(
        {"test_cleanup.py": "def test_ok():\n    assert True\n"},
        client=_CLIENT,
    )
    assert result.status == "passed"
    _assert_no_sandbox_containers()


@_DOCKER_SKIP
def test_sandbox_fork_bomb_hits_pids_limit_and_finishes():
    code = textwrap.dedent("""\
        import errno
        import os
        import time

        def test_forks_are_limited():
            children = []
            blocked = False
            for _ in range(128):
                try:
                    pid = os.fork()
                except OSError as error:
                    assert error.errno == errno.EAGAIN
                    blocked = True
                    break
                if pid == 0:
                    time.sleep(0.5)
                    os._exit(0)
                children.append(pid)

            for child in children:
                os.waitpid(child, 0)
            assert blocked, "fork attempts were not stopped by the pids limit"
    """)
    result = run_tests_in_sandbox(
        {"test_fork_bomb.py": code},
        timeout_seconds=15,
        client=_CLIENT,
    )
    assert result.status == "passed", result.message + "\n" + result.output
    assert result.duration_seconds < 60
    _assert_no_sandbox_containers()


@_DOCKER_SKIP
def test_sandbox_memory_hog_is_killed_by_memory_limit():
    code = (
        "def test_memory_hog():\n"
        "    memory = bytearray(1024 * 1024 * 1024)\n"
        "    assert len(memory) == 1024 * 1024 * 1024\n"
    )
    result = run_tests_in_sandbox(
        {"test_memory_hog.py": code},
        timeout_seconds=15,
        memory="128m",
        client=_CLIENT,
    )
    assert result.status == "error"
    assert "memory limit" in result.message.lower() or "oom" in result.message.lower()
    assert result.duration_seconds < 60
    _assert_no_sandbox_containers()


@_DOCKER_SKIP
def test_sandbox_truncates_huge_output():
    code = (
        "def test_huge_output():\n"
        "    print('output' * (3 * 1024 * 1024))\n"
    )
    result = run_tests_in_sandbox(
        {"test_huge_output.py": code},
        timeout_seconds=15,
        client=_CLIENT,
    )
    assert result.status == "passed", result.message
    assert len(result.output.encode("utf-8")) <= 64 * 1024
    assert result.duration_seconds < 60
    _assert_no_sandbox_containers()


@_DOCKER_SKIP
def test_sandbox_cannot_exceed_work_tmpfs_limit():
    code = textwrap.dedent("""\
        def test_work_tmpfs_limit():
            try:
                with open("/work/oversized.bin", "wb") as target:
                    block = b"x" * (1024 * 1024)
                    for _ in range(80):
                        target.write(block)
            except OSError:
                return
            raise AssertionError("write exceeded the /work tmpfs size limit")
    """)
    result = run_tests_in_sandbox(
        {"test_tmpfs_limit.py": code},
        timeout_seconds=15,
        client=_CLIENT,
    )
    assert result.status == "passed", result.message + "\n" + result.output
    assert result.duration_seconds < 60
    _assert_no_sandbox_containers()
