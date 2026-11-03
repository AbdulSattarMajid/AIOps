"""Week 3 demo: generate and validate a patch without writing to the repository."""
import re
import sys

sys.path.insert(0, "src")

from dotenv import load_dotenv

from aiops.agent.fake_llm import make_demo_llm
from aiops.agent.graph import run_agent
from aiops.agent.llm import GroqLLM, MissingAPIKeyError, MissingModelError
from aiops.rag.store import RunbookStore

REPO_ROOT = "mock_services/sample_app"
_API_KEY_PATTERN = re.compile(r"(?:gsk_[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]+)")
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
    debug = "--debug" in sys.argv
    if "--real" in sys.argv:
        load_dotenv()
        try:
            llm = GroqLLM()
        except (MissingAPIKeyError, MissingModelError) as error:
            print(str(error))
            return 1
    else:
        llm = make_demo_llm()
        print("=" * 72)
        print("PRETEND AI: using canned answers; no real API calls will be made.")
        print("=" * 72)

    store = RunbookStore(path="chroma_data")
    store.load_directory("runbooks/default")
    for service_name, trace in CRASHES:
        state = run_agent(
            llm,
            store,
            trace=trace,
            service_name=service_name,
            repo_root=REPO_ROOT,
        )
        print("\n" + "=" * 72)
        print(f"Service: {service_name}")
        print(f"Status: {state.get('status')}")
        print(f"Model: {state.get('model') or 'fake (canned answers)'}")
        print(f"Attempts: {state.get('attempt', 0)}")
        print(f"Tokens: {state.get('tokens_used', 0)}")
        print("Runbooks: {}".format(
            ", ".join(item.get("title", "") for item in state.get("runbooks", []))
            or "(none)"
        ))
        print("Failed attempts:")
        if state.get("history"):
            for entry in state["history"]:
                print(f"  Attempt {entry['attempt']}: {entry['error']}")
                if debug and "reply" in entry:
                    safe_reply = _API_KEY_PATTERN.sub(
                        "<REDACTED_API_KEY>", entry["reply"]
                    )
                    rendered = repr(safe_reply)
                    if len(rendered) > 1500:
                        rendered = rendered[:1497] + "..."
                    print(f"    Raw LLM reply: {rendered}")
        else:
            print("  (none)")
        if state.get("patch"):
            print("Patch:\n{}".format(state["patch"]))
        else:
            print("Reason: {}".format(state.get("reason", "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
