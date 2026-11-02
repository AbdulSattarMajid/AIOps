# Self-Healing Microservices AIOps System

Receives microservice error logs, deduplicates them, finds the relevant runbook,
has an AI agent write a patch, tests it in a Docker sandbox, and opens a Pull Request.

## Run the API
    uvicorn aiops.api.main:app --reload --app-dir src

Then open http://127.0.0.1:8000/health and http://127.0.0.1:8000/docs

## Run tests
    pytest

## Run the crash-analysis demo
    python demo_week2.py

The demo extracts the failing function from each sample traceback and retrieves
matching runbooks from `runbooks/default`.

## Check your setup
    python check_setup.py
