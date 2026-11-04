"""Run the Redis Streams worker with ``python -m aiops.worker``."""

from aiops.worker.worker import run_worker


def main() -> None:
    run_worker()


if __name__ == "__main__":
    main()
