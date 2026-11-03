"""Sends fake crash logs to the API. Start the API first, then run:
    python mock_services/send_crashes.py
"""
import random
import uuid
from datetime import UTC, datetime

import httpx

URL = "http://127.0.0.1:8000/api/v1/telemetry/logs"


def now():
    return datetime.now(UTC).isoformat()


def null_reference():
    return {
        "timestamp": now(),
        "service_name": "user-service",
        "error_class": "AttributeError",
        "message": "'NoneType' object has no attribute 'email'",
        "stack_trace": (
            "Traceback (most recent call last):\n"
            '  File "/app/users.py", line 19, in get_user_email\n'
            "    return user.email\n"
            "AttributeError: 'NoneType' object has no attribute 'email'\n"
            f"request_id={uuid.uuid4()}"
        ),
    }


def missing_env_var():
    return {
        "timestamp": now(),
        "service_name": "payment-service",
        "error_class": "KeyError",
        "message": "'STRIPE_API_KEY'",
        "stack_trace": (
            "Traceback (most recent call last):\n"
        '  File "/app/config.py", line 6, in load_config\n'
        '    key = os.environ["STRIPE_API_KEY"]\n'
            "KeyError: 'STRIPE_API_KEY'"
        ),
    }


def db_timeout():
    return {
        "timestamp": now(),
        "service_name": "order-service",
        "error_class": "TimeoutError",
        "message": "connection pool timeout",
        "stack_trace": (
            "Traceback (most recent call last):\n"
        '  File "/app/db.py", line 9, in get_connection\n'
        "    conn = self.pool.acquire(timeout=30)\n"
            "TimeoutError: connection pool timeout after waiting for a free "
            "connection\n"
            f"pid={random.randint(1000, 9999)}"
        ),
    }


GENERATORS = [null_reference, missing_env_var, db_timeout]

if __name__ == "__main__":
    for generator in GENERATORS:
        print(f"--- {generator.__name__}: sending 100 times ---")
        results = [httpx.post(URL, json=generator()).json() for _ in range(100)]
        new = sum(1 for r in results if r["is_new"])
        print(f"new incidents created: {new}")
        print(f"final occurrence count: {results[-1]['occurrence_count']}")