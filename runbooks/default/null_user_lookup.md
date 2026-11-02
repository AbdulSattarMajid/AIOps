---
title: Null result from user lookup
service_name: user-service
error_type: AttributeError
severity: medium
---
# Null result from user lookup

## Symptoms
AttributeError: 'NoneType' object has no attribute such as email or name, raised right after
looking up a user, order or record that does not exist.

## Likely cause
A lookup returned None (record deleted, wrong id, or not yet created) and the code used the
result without checking.

## Fix
Check for None right after the lookup and return a clear not-found result or raise a specific
exception. Do not hide the problem with a broad try/except.

## Verify
Add a regression test that looks up a missing id and expects the not-found behavior.
