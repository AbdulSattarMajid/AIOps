"""Sample order-service database helper with a deliberate bug."""


class OrderRepository:
    def __init__(self, pool):
        self.pool = pool

    def get_connection(self):
        conn = self.pool.acquire(timeout=30)
        return conn
