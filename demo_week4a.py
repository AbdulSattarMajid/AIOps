"""Run baseline and patched sample-app tests in isolated Docker containers."""
import sys
from pathlib import Path

sys.path.insert(0, "src")

from aiops.sandbox.runner import is_docker_available
from aiops.sandbox.verify import run_app_tests

APP_DIR = Path("mock_services/sample_app")
GOOD_USERS = """\"\"\"Sample user-service code with a deliberate bug, used to demo crash analysis.\"\"\"

USERS = {1: "ana@example.com"}


class User:
    def __init__(self, email):
        self.email = email


def find_user(user_id):
    if user_id in USERS:
        return User(USERS[user_id])
    return None


def get_user_email(user_id):
    user = find_user(user_id)
    if user is None:
        raise LookupError(f"user {user_id} not found")
    return user.email
"""
GOOD_CONFIG = '''"""Sample payment-service config loader with a deliberate bug."""
import os


def load_config():
    key = os.environ.get("STRIPE_API_KEY")
    if key is None:
        raise RuntimeError("Missing required environment variable: STRIPE_API_KEY")
    return {"stripe_key": key}
'''
BAD_DB = '''"""Sample order-service database helper with a deliberate bug."""


class OrderRepository:
    def __init__(self, pool):
        self.pool = pool

    def get_connection(self):
        try:
            conn = self.pool.acquire(timeout=30)
        except TimeoutError as error:
            raise RuntimeError("database temporarily unavailable") from error
        return conn
'''


def show_result(name, result):
    print(f"\n=== {name} ===")
    print(f"Status: {result.status}")
    print(f"Tests passed: {result.tests_passed}")
    print(f"Tests failed: {result.tests_failed}")
    print(f"Duration: {result.duration_seconds:.2f}s")
    if result.message:
        print(f"Message: {result.message}")
    print("Last output lines:")
    lines = result.output.splitlines()
    print("\n".join(lines[-15:]) if lines else "(no output)")


def main():
    if not is_docker_available():
        print("Docker is not running. Start Docker Desktop and try again.")
        return 1

    base = run_app_tests(str(APP_DIR))
    show_result("A) Baseline", base)

    good = run_app_tests(
        str(APP_DIR),
        overrides={"users.py": GOOD_USERS, "config.py": GOOD_CONFIG},
    )
    show_result("B) Good users.py and config.py patches", good)

    symptom_only = run_app_tests(
        str(APP_DIR),
        overrides={
            "users.py": GOOD_USERS,
            "config.py": GOOD_CONFIG,
            "db.py": BAD_DB,
        },
    )
    show_result("C) Good patches plus symptom-only db.py patch", symptom_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
