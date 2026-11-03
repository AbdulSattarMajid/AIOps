# Week 3: Validated AI Patch Agent

## What was implemented

The Week 3 agent analyzes a crash trace, locates the failing function and relevant
runbooks, asks an LLM for a unified diff, validates that diff, and retries rejected
answers up to the configured limit. It returns a validated patch or a clear reason
for escalation. The agent does not write changes to the repository.

### Agent components

- `src/aiops/agent/graph.py` defines the LangGraph workflow: crash analysis,
  runbook retrieval, patch generation, validation, retry, and summary.
- `src/aiops/agent/diff_validator.py` parses and applies diffs in memory. It checks
  paths, hunk context, change limits, function boundaries, and resulting Python
  syntax, then returns a canonical diff.
- `src/aiops/agent/prompts.py` builds prompts and fences untrusted crash logs,
  source code, and runbooks.
- `src/aiops/agent/fake_llm.py` provides canned responses for offline tests and
  demos, including a rejected first user-service diff and an operations-only
  database timeout.
- `src/aiops/agent/llm.py` provides the `GroqLLM` provider and `LLMReply` response
  type. It supports token reporting and one fallback-model retry.

### API and demos

- `POST /api/v1/incidents/{incident_id}/analyze` analyzes an existing incident.
  The API lazily loads runbooks and returns HTTP 503 when the Groq key or primary
  model configuration is missing.
- `demo_week3.py` runs the three sample incidents with canned answers by default.
  `--real` selects Groq and requires the configured credentials and model.
- `mock_services/send_crashes.py` now emits traces at the sample application's
  actual crash locations.
- `src/aiops/dedup/normalize.py` preserves HTTP status codes such as 404 and 500
  while continuing to normalize other changing values.

### Groq configuration

The expected `.env` variables are:

```dotenv
GROQ_API_KEY=
LLM_MODEL=openai/gpt-oss-20b
LLM_FALLBACK_MODEL=openai/gpt-oss-120b
```

`.env.example`, `requirements.txt`, `check_setup.py`, and `setup_project.py` were
updated for Groq. Keep the actual API key in the ignored `.env` file; it is not
included in this document.

## Tests

Added offline tests for normalization, diff validation, agent retries and safety,
Groq response handling and fallback, and the analyze endpoint. The original tests
were left intact.

Final verification:

```text
pytest: 64 passed
Ruff checks: passed
Provider reference search: no matches
Tracked-file API-key-pattern scan: no matches
```

The Week 3 demo produces a validated user-service patch on attempt 2, a
payment-service patch on attempt 1, and an operations `NO_PATCH` result for the
order-service timeout. The default demo uses canned answers and does not make
real API calls.
