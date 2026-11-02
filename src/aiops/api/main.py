from fastapi import FastAPI

app = FastAPI(title="Self-Healing AIOps")


@app.get("/health")
def health():
    return {"status": "ok"}
