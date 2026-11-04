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
    assert _CLIENT.containers.list(
        all=True, filters={"label": "aiops=sandbox"}
    ) == []
