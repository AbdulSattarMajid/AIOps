"""
Creates the starting folder structure for the Self-Healing AIOps project.

Usage:
    python setup_project.py            (creates ./aiops)
    python setup_project.py my_folder  (creates ./my_folder)

Safe to re-run: it never overwrites a file that already exists.
"""
import sys
from pathlib import Path

PROJECT = Path(sys.argv[1] if len(sys.argv) > 1 else "aiops")

PACKAGES = [
    "api", "ingest", "dedup", "context", "rag",
    "agent", "sandbox", "delivery", "audit",
]
OTHER_DIRS = ["mock_services", "runbooks", "eval", "tests", "sandbox_images"]

FILES = {}

FILES["requirements.txt"] = """# Phase 1 (Weeks 1-4) dependencies only.
# Redis, PostgreSQL, Presidio, tree-sitter, Prometheus come later (Phase 2).
fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.6
numpy>=1.26
chromadb>=0.5
langgraph>=0.2
anthropic>=0.30
docker>=7.0
python-dotenv>=1.0
httpx>=0.27
PyGithub>=2.3
slack-sdk>=3.27
pytest>=8.0
ruff>=0.4
"""

FILES[".gitignore"] = """.venv/
__pycache__/
*.pyc
.env
.pytest_cache/
.ruff_cache/
chroma_data/
*.log
.idea/
.vscode/
"""

FILES[".env.example"] = """# Copy this file to .env and fill in real values. NEVER commit .env.
ANTHROPIC_API_KEY=
GITHUB_TOKEN=
GITHUB_REPO=owner/repo
SLACK_WEBHOOK_URL=
MAX_RETRIES=3
"""

FILES["pytest.ini"] = """[pytest]
pythonpath = src
testpaths = tests
"""

FILES["README.md"] = """# Self-Healing Microservices AIOps System

Receives microservice error logs, deduplicates them, finds the relevant runbook,
has an AI agent write a patch, tests it in a Docker sandbox, and opens a Pull Request.

## Run the API
    uvicorn aiops.api.main:app --reload --app-dir src

Then open http://127.0.0.1:8000/health and http://127.0.0.1:8000/docs

## Run tests
    pytest

## Check your setup
    python check_setup.py
"""

FILES["src/aiops/api/main.py"] = """from fastapi import FastAPI

app = FastAPI(title="Self-Healing AIOps")


@app.get("/health")
def health():
    return {"status": "ok"}
"""

FILES["tests/test_smoke.py"] = """from fastapi.testclient import TestClient

from aiops.api.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
"""

FILES["check_setup.py"] = """\"\"\"Checks that your machine is ready for the project. Run: python check_setup.py\"\"\"
import importlib
import os
import shutil
import sys

ok = True


def report(name, passed, hint=""):
    global ok
    print(("[ OK ] " if passed else "[FAIL] ") + name + ("" if passed else "  -> " + hint))
    if not passed:
        ok = False


report("Python 3.10+", sys.version_info >= (3, 10), "Install Python 3.11 or 3.12")
report("git installed", shutil.which("git") is not None, "Install git from git-scm.com")

for module in ["fastapi", "uvicorn", "pydantic", "numpy", "chromadb",
               "langgraph", "anthropic", "docker", "dotenv", "pytest"]:
    try:
        importlib.import_module(module)
        report("package: " + module, True)
    except Exception:
        report("package: " + module, False, "Run: pip install -r requirements.txt")

try:
    import docker
    docker.from_env().ping()
    report("Docker is running", True)
except Exception:
    report("Docker is running", False, "Install Docker Desktop and start it")

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
report("ANTHROPIC_API_KEY set in .env", bool(os.getenv("ANTHROPIC_API_KEY")),
       "Copy .env.example to .env and add your key")

print()
print("Everything is ready." if ok else "Fix the [FAIL] items above, then run this again.")
"""


def main():
    for pkg in PACKAGES:
        d = PROJECT / "src" / "aiops" / pkg
        d.mkdir(parents=True, exist_ok=True)
        init = d / "__init__.py"
        if not init.exists():
            init.write_text("")
    top_init = PROJECT / "src" / "aiops" / "__init__.py"
    if not top_init.exists():
        top_init.write_text("")

    for name in OTHER_DIRS:
        (PROJECT / name).mkdir(parents=True, exist_ok=True)
        keep = PROJECT / name / ".gitkeep"
        if name != "tests" and not keep.exists():
            keep.write_text("")

    for rel, content in FILES.items():
        path = PROJECT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            print("skipped (already exists): " + rel)
        else:
            path.write_text(content, encoding="utf-8")
            print("created: " + rel)

    print()
    print("Project folder ready: " + str(PROJECT.resolve()))
    print("Next: follow PRE_WEEK_1_SETUP.md from step 5 onward.")


if __name__ == "__main__":
    main()