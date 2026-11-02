---
title: Out of memory
service_name: "*"
error_type: MemoryError
severity: high
---
# Out of memory

## Symptoms
MemoryError, or the container is killed by the platform (OOMKilled), often after processing a
large request or file.

## Likely cause
Loading a whole large payload into memory, an unbounded cache or list, or a memory limit set
too low for normal traffic.

## Fix
Process data in chunks or streams, cap cache sizes, and set limits on request sizes.
Raising the memory limit is an operations decision and needs human approval.

## Verify
Replay the large input in a test and watch memory use stay flat.
