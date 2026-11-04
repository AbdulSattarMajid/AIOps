# Week 4A: Docker Sandbox Verification

## What was implemented

Added a Docker-based test sandbox that runs pytest against a copied sample
application. Files are transferred into containers with Docker's archive-copy API;
the host project is never mounted, and the sandbox does not mount the Docker
socket.

The sandbox container:

- Uses the `aiops-sandbox:py311` image, based on `python:3.11-slim`.
- Runs pytest as the non-root `sandbox` user (UID/GID 1000).
- Has networking disabled with `network_mode="none"`.
- Is not privileged, drops all capabilities, and enables `no-new-privileges`.
- Has memory, swap, CPU, PID, and execution-time limits.
- Is removed with `force=True` in a `finally` block after each run.

`/work` is intentionally writable because Docker's archive-copy API cannot
extract files to a read-only root or a tmpfs work directory. Further hardening can
restrict other container paths in a later phase.

## New files

- `sandbox_images/python/Dockerfile` builds the minimal Python 3.11 test image.
- `src/aiops/sandbox/runner.py` validates input files, creates the in-memory tar
  archive, runs pytest in a constrained container, and returns a `SandboxResult`.
- `src/aiops/sandbox/verify.py` safely collects Python files and runs tests with
  optional in-memory source overrides.
- `mock_services/sample_app/test_users.py` checks existing and missing-user
  behavior.
- `mock_services/sample_app/test_config.py` checks configured and missing
  environment-variable behavior.
- `mock_services/sample_app/test_db.py` checks successful acquisition and
  propagation of pool timeouts.
- `tests/test_sandbox.py` covers file validation, tar ownership, pytest-summary
  parsing, file collection, Docker errors, and Docker integration/security.
- `demo_week4a.py` runs the baseline, good-patch, and symptom-only-patch scenarios.

Updated `.gitignore` with `.env.*` (while allowing `.env.example`),
`.mypy_cache/`, `.coverage`, and `htmlcov/`.

The sample-app tests live beside the sample application and are not collected by
the project's root pytest configuration.

## Verification

Image build:

```text
docker build -t aiops-sandbox:py311 sandbox_images/python
... DONE
```

Project tests, with Docker integration tests enabled:

```text
86 passed
```

All 9 Docker integration tests ran; none were skipped.

Demo results:

| Scenario | Status | Passed | Failed |
|---|---:|---:|---:|
| Baseline sample app | failed | 4 | 2 |
| Good `users.py` and `config.py` overrides | passed | 6 | 0 |
| Good overrides plus symptom-only `db.py` override | failed | 5 | 1 |

The sandbox-labeled container check found no leftover containers after the test
runs. Other unrelated containers already present on the Docker host were left
untouched.

Ruff checks passed and `git diff --check` was clean. Changes were not committed.
