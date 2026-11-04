# Local AIOps Streamlit demo

## Prerequisites

- Windows PowerShell, Python, the project dependencies, and Docker Desktop.
- The `aiops-redis` Docker container for queue-backed ingestion.
- A local API key: the API stores only its hash, while Streamlit uses the raw
  key from `.streamlit/secrets.toml`.
- Run every service on `127.0.0.1`; this demo is not designed for remote access.

Install the requirements in the active virtual environment:

```powershell
python -m pip install -r requirements.txt
```

## Configure the API key

Generate a key and hash:

```powershell
python scripts/make_api_key.py
```

Put **only** the generated `AIOPS_API_KEY_HASHES` value in the local `.env`.
Put the raw key in `.streamlit/secrets.toml`:

```toml
AIOPS_DEMO_API_KEY = "paste-the-generated-key-here"
```

Never put the raw key in `.env`, source control, screenshots, logs, or messages.
The checked-in `.streamlit/secrets.toml.example` contains placeholders only.

## Start order

Open four PowerShell windows at the repository root, in this order:

1. Start Redis:

   ```powershell
   docker start aiops-redis
   ```

2. Start the API:

   ```powershell
   uvicorn aiops.api.main:app --app-dir src --host 127.0.0.1 --port 8000
   ```

3. Start the queue worker:

   ```powershell
   $env:PYTHONPATH = (Join-Path (Get-Location) "src")
   python -m aiops.worker
   ```

4. Start the target app:

   ```powershell
   streamlit run demo_ui/target_app.py --server.address 127.0.0.1 --server.port 8501
   ```

5. Start the dashboard:

   ```powershell
   streamlit run demo_ui/dashboard.py --server.address 127.0.0.1 --server.port 8502
   ```

`.\run_demo.ps1` checks that Docker Desktop is running, starts the existing
`aiops-redis` container if it is stopped, and opens separate windows for the
API, worker, target app, and dashboard. It does not create the Redis container
or display secret values.

## Demo story

1. Open the target app at <http://127.0.0.1:8501> and confirm that it reports
   the API reachable and the key configured without showing the key.
2. Click **Load user 999**, **Load payment config**, and **Get DB connection**.
   The sample app deliberately raises a missing-user error, missing payment
   configuration error, and database-pool timeout. Each caught exception is
   sent as a size-limited `LogEvent`.
3. Open the dashboard at <http://127.0.0.1:8502>. Refresh to see the queue,
   pending/dead-letter counts, and incidents after the worker processes events.
4. Select an incident and press **Analyze**. Review the agent status, diff,
   verification, token usage, rejected attempts, and test evidence.
5. Opening a draft pull request requires verified/fix-confirmed evidence and
   a separate confirmation. The target repository is shown before the action.
   Delivery is never automatic.
6. Use **Crash x N** on the target app or **Storm simulator** on the dashboard
   to send repeated local demo events. Refresh the dashboard after draining.

## Free local analysis and no Docker sandbox

For deterministic fake AI, start the API with `AIOPS_LLM=fake` set in that
PowerShell window:

```powershell
$env:AIOPS_LLM = "fake"
uvicorn aiops.api.main:app --app-dir src --host 127.0.0.1 --port 8000
```

To disable Docker-based verification, also set:

```powershell
$env:AIOPS_SANDBOX = "off"
```

The fake model is for local demonstration only; it is not a production model.

## Rate limits

Ingestion defaults to **300 requests/minute** and analysis to **10
requests/minute**. For a local storm demonstration, raise
`RATE_LIMIT_INGEST_PER_MINUTE` in the local `.env` and restart the API. Do not
raise limits on an exposed service.

## Troubleshooting

| Symptom | Check |
|---|---|
| `key not configured` | Put `AIOPS_DEMO_API_KEY` in `.streamlit/secrets.toml` or the environment. |
| `unauthorized` | Confirm `.env` contains the hash for the key in Streamlit secrets; restart the API after changing `.env`. |
| Backend unavailable | Confirm the API is listening at `127.0.0.1:8000`. |
| Redis/queue unavailable | Start Docker Desktop and the existing `aiops-redis` container; check the worker console. |
| Rate limited | Wait for `Retry-After`, or raise the local ingest limit for the demo. Analysis remains limited to 10/minute by default. |
| Sandbox unavailable | Confirm Docker is running and the sandbox image is present, or use `AIOPS_SANDBOX=off`. |
| No incidents visible | Wait for the worker to drain the queue, then press Refresh. |

## Honest limits

- The demo analyzes Python code from the local sample-app folder only.
- It is a local-only demonstration and should be bound to loopback.
- It does not provide general-purpose repair, support non-Python projects, or
  guarantee that an AI-generated patch is correct.
- Draft PR delivery requires separate human confirmation and GitHub
  configuration.
