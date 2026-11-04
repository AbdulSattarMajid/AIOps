import pytest
from db import OrderRepository


class FakePool:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def acquire(self, timeout):
        self.calls.append(timeout)
        if self.error:
            raise self.error
        return "connection"


def test_get_connection_returns_pool_connection():
    pool = FakePool()
    assert OrderRepository(pool).get_connection() == "connection"
    assert pool.calls == [30]


def test_timeout_still_propagates_as_timeouterror():
    pool = FakePool(TimeoutError("pool timeout"))
    with pytest.raises(TimeoutError):
        OrderRepository(pool).get_connection()
