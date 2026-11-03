import os
from dataclasses import asdict

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException

from aiops.agent.fake_llm import make_demo_llm
from aiops.agent.graph import run_agent, summarize
from aiops.agent.llm import GroqLLM, MissingAPIKeyError, MissingModelError
from aiops.dedup.engine import DedupEngine
from aiops.ingest.schemas import LogEvent
from aiops.rag.store import RunbookStore

load_dotenv()

app = FastAPI(title="Self-Healing AIOps")
REPO_ROOT = os.getenv("REPO_ROOT", "mock_services/sample_app")

# Week 1: seen-error memory lives in this Python object. It is lost when the server
# restarts. (Redis replaces this in Week 6.)
engine = DedupEngine()
_store = None


def get_store():
    global _store
    if _store is None:
        _store = RunbookStore(path="chroma_data")
        _store.load_directory("runbooks/default")
    return _store


def get_llm():
    if os.getenv("AIOPS_LLM") == "fake":
        return make_demo_llm()
    try:
        return GroqLLM()
    except (MissingAPIKeyError, MissingModelError) as error:
        raise HTTPException(status_code=503, detail=str(error))


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


@app.post("/api/v1/incidents/{incident_id}/analyze")
def analyze_incident(incident_id: int, llm=Depends(get_llm),
                     store=Depends(get_store)):
    incident = engine.incidents.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    state = run_agent(
        llm,
        store,
        trace=incident.sample_trace,
        service_name=incident.service_name,
        repo_root=REPO_ROOT,
        incident_id=incident.id,
    )
    return summarize(state)