"""Demonstrate hostile-code containment in the isolated sandbox."""
import sys

sys.path.insert(0, "src")

import docker

from aiops.sandbox.runner import is_docker_available, run_tests_in_sandbox

IMAGE = "aiops-sandbox:py311"


def main():
    if not is_docker_available():
        for name in (
            "fork bomb",
            "memory hog",
            "network attempt",
            "root filesystem write",
        ):
            print(f"{name}: NOT BLOCKED")
        print("container removed: yes")
        return 1

    client = docker.from_env()
    scenarios = [
        (
            "fork bomb",
            {
                "test_fork.py": (
                    "import errno, os, time\n"
                    "def test_fork_limit():\n"
                    "    children = []\n"
                    "    blocked = False\n"
                    "    for _ in range(128):\n"
                    "        try:\n"
                    "            pid = os.fork()\n"
                    "        except OSError as error:\n"
                    "            assert error.errno == errno.EAGAIN\n"
                    "            blocked = True\n"
                    "            break\n"
                    "        if pid == 0:\n"
                    "            time.sleep(0.5)\n"
                    "            os._exit(0)\n"
                    "        children.append(pid)\n"
                    "    for child in children:\n"
                    "        os.waitpid(child, 0)\n"
                    "    assert blocked\n"
                )
            },
            "passed",
            None,
        ),
        (
            "memory hog",
            {
                "test_memory.py": (
                    "def test_memory_hog():\n"
                    "    memory = bytearray(1024 * 1024 * 1024)\n"
                    "    assert len(memory) == 1024 * 1024 * 1024\n"
                )
            },
            "oom",
            "128m",
        ),
        (
            "network attempt",
            {
                "test_network.py": (
                    "import socket\n"
                    "def test_network_disabled():\n"
                    "    try:\n"
                    "        socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
                    "    except OSError:\n"
                    "        return\n"
                    "    raise AssertionError('network connection succeeded')\n"
                )
            },
            "passed",
            None,
        ),
        (
            "root filesystem write",
            {
                "test_root_write.py": (
                    "def test_root_is_read_only():\n"
                    "    try:\n"
                    "        open('/etc/aiops-week5c-write-test', 'w').close()\n"
                    "    except OSError:\n"
                    "        return\n"
                    "    raise AssertionError('root filesystem write succeeded')\n"
                )
            },
            "passed",
            None,
        ),
    ]

    all_containers_removed = True
    for name, files, expected, memory in scenarios:
        result = run_tests_in_sandbox(
            files,
            timeout_seconds=15,
            memory=memory or "256m",
            image=IMAGE,
            client=client,
        )
        if expected == "oom":
            blocked = (
                result.status == "error"
                and ("memory limit" in result.message.lower()
                     or "oom" in result.message.lower())
            )
        else:
            blocked = result.status == expected
        print(f"{name}: {'blocked' if blocked else 'NOT BLOCKED'}")
        leftovers = client.containers.list(
            all=True, filters={"label": "aiops=sandbox"}
        )
        all_containers_removed = all_containers_removed and not leftovers

    print(f"container removed: {'yes' if all_containers_removed else 'no'}")
    return 0 if all_containers_removed else 1


if __name__ == "__main__":
    raise SystemExit(main())
