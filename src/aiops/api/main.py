from dataclasses import asdict

from fastapi import FastAPI

from aiops.dedup.engine import DedupEngine
from aiops.ingest.schemas import LogEvent

app = FastAPI(title="Self-Healing AIOps")

# Week 1: seen-error memory lives in this Python object. It is lost when the server
# restarts. (Redis replaces this in Week 6.)
engine = DedupEngine()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/telemetry/logs")
def ingest_log(event: LogEvent):
    incident, is_new = engine.process(event)
    return {
        "incident_id": incident.id,
        "is_new": is_new,
        "occurrence_count": incident.count,
    }


@app.get("/api/v1/incidents")
def list_incidents():
    return [asdict(i) for i in engine.incidents.values()]