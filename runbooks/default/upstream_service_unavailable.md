---
title: Upstream service unavailable
service_name: "*"
error_type: ConnectionError
severity: medium
---
# Upstream service unavailable

## Symptoms
ConnectionError, connection refused, or HTTP 502/503 responses when calling another service.

## Likely cause
The other service is down, restarting, overloaded, or its address or port changed.

## Fix
This is usually not a bug in this service's code. Check the health of the upstream service
first. In code, add timeouts, limited retries with backoff, and a clear fallback or error
message. Restarting or rolling back the upstream service is an operations action that needs
human approval.

## Verify
Confirm the upstream health endpoint responds and errors stop.
