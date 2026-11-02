---
title: Missing environment variable
service_name: payment-service
error_type: KeyError
severity: high
---
# Missing environment variable

## Symptoms
The service crashes at startup or on first use with a KeyError naming a setting such as
STRIPE_API_KEY, raised from os.environ[...] while loading configuration.

## Likely cause
The environment variable was never set in this deployment, was renamed, or a new release
started reading a setting that the deployment config does not provide yet.

## Fix
Read optional settings with os.environ.get() and a clear default, or fail early with a
readable error message that names the missing variable. If the setting is required, add it
to the deployment configuration (secret manager, not source code) and redeploy.

## Verify
Start the service in a clean environment and confirm the config loads. Add a test that
runs load_config() with the variable removed.
