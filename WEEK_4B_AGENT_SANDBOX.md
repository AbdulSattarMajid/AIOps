# Week 4B: Agent and Sandbox Verification

## What was implemented

Connected the Week 3 LangGraph agent to the Week 4A Docker sandbox as an
optional verification step. When a sandbox is provided, the agent runs the app
tests before generating a patch, then runs them again on the patched copy. It
compares failing test IDs before and after, so a patch can fix one bug without
being expected to fix unrelated failures.

When no sandbox is provided, the agent continues to validate and return patches
as it did in Week 3.

### Verification behavior

- `fix_confirmed`: tests that failed in the target module now pass and no new
  failures appeared.
- `no_regressions`: no tests broke, but there was no baseline failure in the
  target module to confirm as fixed.
- `failed`: a target test still fails, a new failure appeared, or tests timed
  out. The agent records the failure and may ask the LLM for a correction.
- `unavailable`: the sandbox could not run the baseline or patched tests. The
  patch is retained without treating the infrastructure problem as an LLM
  failure.

Rejected attempts record whether the diff itself failed validation or whether
the sandbox tests rejected the patch. On test failures, the next prompt includes
the test output inside an untrusted-data fence. A `NO_PATCH` answer ends the run
without another retry.

## New files

- `src/aiops/sandbox/evaluate.py` defines `Verification` and compares baseline
  and after-patch test failures.
- `src/aiops/sandbox/app_sandbox.py` provides the `AppSandbox` baseline and
  patched-file interface.
- `tests/test_sandbox_agent.py` covers verification outcomes, retries,
  prompt-fencing, no-sandbox behavior, and Docker-backed sample scenarios.
- `demo_week4b.py` runs the three sample incidents with canned LLM replies and
  real restricted Docker test runs.

## Updated files

- `src/aiops/sandbox/runner.py` now extracts pytest failure node IDs into
  `SandboxResult.failed_tests`.
- `src/aiops/agent/graph.py` adds optional baseline and patch-verification
  stages, retry handling, and verification in summaries.
- `src/aiops/agent/prompts.py` warns that test output is untrusted and cannot be
  used to justify changing tests or other files.
- `src/aiops/agent/fake_llm.py` adds `make_demo_llm_4b()` without changing the
  Week 3 demo LLM.
- `src/aiops/api/main.py` adds an optional sandbox dependency controlled by
  `AIOPS_SANDBOX=off` and Docker availability.
- `tests/test_sandbox.py` checks failed-test extraction.
- `tests/test_api_analyze.py` checks the verification field in API responses.

## Demo outcomes

`demo_week4b.py` uses canned answers, not a real LLM, and runs tests in the
network-disabled Docker sandbox.

| Incident | Outcome | Attempts |
|---|---|---:|
| User lookup | `verified`; first patch leaves the target test failing | 2 |
| Missing Stripe key | `verified` | 1 |
| Database timeout | `no_patch`; symptom-only exception wrapping causes a newly failing test | 2 |

## Verification

```text
pytest -q: 107 passed
Docker integration tests: 12 ran, 0 skipped
Ruff checks: passed
git diff --check: clean
docker ps -a | findstr aiops: no leftover aiops containers
```

The Week 3 pretend demo was also run and retained its previous outcomes. No real
LLM/API calls were made, and no changes were committed.
