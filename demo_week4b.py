"""Demonstrate the fake agent with real, isolated sandbox test runs."""
import os
import sys

sys.path.insert(0, "src")

from dotenv import load_dotenv

from aiops.agent.fake_llm import make_demo_llm_4b
from aiops.agent.graph import run_agent
from aiops.agent.llm import GroqLLM, MissingAPIKeyError, MissingModelError
from aiops.rag.store import RunbookStore
from aiops.sandbox.app_sandbox import AppSandbox
from aiops.sandbox.runner import is_docker_available

REPO_ROOT = "mock_services/sample_app"
CRASHES = [
    ("user-service", """Traceback (most recent call last):
  File "/app/users.py", line 19, in get_user_email
    return user.email
AttributeError: 'NoneType' object has no attribute 'email'"""),
    ("payment-service", """Traceback (most recent call last):
  File "/app/config.py", line 6, in load_config
    key = os.environ["STRIPE_API_KEY"]
KeyError: 'STRIPE_API_KEY'"""),
    ("order-service", """Traceback (most recent call last):
  File "/app/db.py", line 9, in get_connection
    conn = self.pool.acquire(timeout=30)
TimeoutError: connection pool timeout after waiting for a free connection"""),
]


def main():
    real = "--real" in sys.argv
    if real:
        load_dotenv()
        if not os.getenv("GROQ_API_KEY"):
            print(
                "GROQ_API_KEY is missing. Add it to your .env file "
                "before using --real."
            )
            return 1
        if not os.getenv("LLM_MODEL"):
            print(
                "LLM_MODEL is missing. Add it to your .env file "
                "before using --real."
            )
            return 1
        try:
            llm = GroqLLM()
        except (MissingAPIKeyError, MissingModelError) as error:
            print(f"Groq configuration error: {error}")
            return 1
    else:
        llm = make_demo_llm_4b()

    if not is_docker_available():
        print("Docker is not running. Start Docker Desktop and try again.")
        return 1

    print("=" * 72)
    if real:
        print("REAL AI: Groq API calls are being made.")
    else:
        print("PRETEND AI: canned LLM answers; REAL Docker sandbox test execution.")
    print("The sandbox is isolated and does not have network access.")
    print("=" * 72)
    sandbox = AppSandbox(REPO_ROOT)
    store = RunbookStore(path="chroma_data")
    store.load_directory("runbooks/default")
    for service_name, trace in CRASHES:
        state = run_agent(
            llm,
            store,
            trace=trace,
            service_name=service_name,
            repo_root=REPO_ROOT,
            sandbox=sandbox,
        )
        verification = state.get("verification")
        print(f"\nService: {service_name}")
        print(f"Model: {state.get('model') or '(no model response)'}")
        print(f"Status: {state.get('status')}")
        print(f"Attempts: {state.get('attempt', 0)}")
        print(f"Tokens used: {state.get('tokens_used', 0)}")
        print(
            "Verification: "
            f"{verification.status if verification else 'skipped'}"
        )
        if verification and verification.message:
            print(f"Verification message: {verification.message}")
        print("Rejected attempts:")
        if state.get("history"):
            for entry in state["history"]:
                print(
                    f"  Attempt {entry['attempt']} "
                    f"[{entry.get('stage', 'diff')}]: {entry['error']}"
                )
        else:
            print("  (none)")
        if state.get("patch"):
            print("Patch:\n{}".format(state["patch"]))
        else:
            print("Reason: {}".format(state.get("reason", "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
