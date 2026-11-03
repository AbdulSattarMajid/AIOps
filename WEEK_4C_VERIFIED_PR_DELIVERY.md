# Week 4C: Verified Pull Request Delivery

## What was implemented

Added an opt-in delivery path that can publish a verified agent patch as a
GitHub draft pull request. The normal analysis endpoint and the default demo
remain offline with respect to GitHub and Slack. No merge is performed.

Before creating a branch or PR, delivery:

1. Requires a verified patch, unless unverified delivery was explicitly allowed.
2. Revalidates the patch against the source captured during analysis.
3. Rejects unsafe file paths.
4. Reads the file from the default branch and refuses delivery if its contents
   changed since analysis.
5. Creates a deterministic `aiops/fix-...` branch and writes only to that
   branch, then opens a draft PR.

Log-derived values are treated as untrusted in PR text: dynamic text is
code-fenced, embedded fences are neutralized, and `@` mentions are disabled.
GitHub errors redact the configured token. Slack notifications contain only a
short incident summary and PR link; notification errors do not fail delivery.

## New files

- `src/aiops/delivery/pr_text.py` renders the PR title/body and deterministic
  branch names.
- `src/aiops/delivery/github_pr.py` defines the GitHub client interface,
  PyGithub implementation, validation and stale-file protections, and draft PR
  delivery.
- `src/aiops/delivery/slack.py` builds and sends optional short Slack
  notifications.
- `demo_week4c.py` previews a local dry run by default. Its `--live` mode
  requires GitHub configuration and exact repository-name confirmation.
- `tests/test_delivery.py` exercises PR rendering, path and patch validation,
  stale-file refusal, branch and draft PR behavior, redaction, dry runs, and
  Slack handling without network calls.

## Updated files

- `src/aiops/agent/graph.py` includes the original and final file source in the
  agent summary so delivery can revalidate the patch.
- `src/aiops/api/main.py` adds `deliver=true` as an opt-in API query parameter,
  along with overridable GitHub dependency injection and optional Slack
  notification.
- `tests/test_api_analyze.py` covers delivery with fake dependencies, missing
  GitHub configuration, and the unchanged response when delivery is omitted.
- `demo_week4b.py` loads the default runbooks and reports token usage for each
  incident.
- `.env.example` contains empty placeholders for GitHub, Slack, and repository
  path prefix configuration.
- `WEEK_3_IMPLEMENTATION.md` updates a stale provider-search note.

## Demo outcomes

`demo_week4c.py` was run in its default dry-run mode. It used the canned LLM
response and local Docker sandbox, printed a draft PR preview for the verified
user-service fix, and did not contact GitHub or Slack.

The Week 4B pretend-AI demo still produced these outcomes:

| Incident | Outcome | Attempts |
|---|---|---:|
| User lookup | `verified` | 2 |
| Missing Stripe key | `verified` | 1 |
| Database timeout | `no_patch` | 2 |

The Week 3 pretend-AI demo also retained its previous outcomes: user and
payment patches were `patch_ready`, and the order incident returned `no_patch`.

## Verification

```text
pytest -q: 129 passed
Docker integration tests: 11 ran, 0 skipped
Credential-pattern scan: no matches
Anthropic/Claude reference scan: no matches
docker ps -a | findstr aiops: no leftover aiops containers
```

No live LLM, GitHub, or Slack calls were made. The changes were left
uncommitted.
