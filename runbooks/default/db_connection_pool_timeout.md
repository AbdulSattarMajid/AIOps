---
title: Database connection pool timeout
service_name: order-service
error_type: TimeoutError
severity: high
---
# Database connection pool timeout

## Symptoms
Requests hang, then fail with TimeoutError while waiting for a free connection from the
database pool. Often happens under load or after a slow query.

## Likely cause
Connections are not released (missing close or context manager), the pool is too small for
the traffic, or a slow query is holding connections open.

## Fix
Make sure every acquired connection is released, using a with-block or try/finally. Check for
slow queries and long transactions. Raise the pool size only after leaks are ruled out.
If the incident is capacity related, restarting the service or scaling out is an operations
action that needs human approval, not a code change.

## Verify
Watch pool usage under load and confirm connections return to the pool.
