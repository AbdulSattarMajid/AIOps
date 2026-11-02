---
title: Disk full
service_name: "*"
error_type: OSError
severity: high
---
# Disk full

## Symptoms
OSError: No space left on device when writing files, logs or temporary data.

## Likely cause
Logs or temporary files grew without cleanup, or an upload directory filled the volume.

## Fix
Free space by rotating logs and cleaning temporary files, then add log rotation and size
limits. Resizing the volume is an operations action that needs human approval.

## Verify
Confirm free disk space and that writes succeed.
