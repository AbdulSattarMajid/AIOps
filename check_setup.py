"""Checks that your machine is ready for the project. Run: python check_setup.py"""
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
               "langgraph", "groq", "docker", "dotenv", "pytest"]:
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
report("GROQ_API_KEY set in .env", bool(os.getenv("GROQ_API_KEY")),
       "Copy .env.example to .env and add your Groq API key")

print()
print("Everything is ready." if ok else "Fix the [FAIL] items above, then run this again.")
