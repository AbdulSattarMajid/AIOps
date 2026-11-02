"""Week 2 demo: for each mock crash, find the broken function and the matching runbook.
Run from the project folder:   python demo_week2.py
"""
import sys

sys.path.insert(0, "src")

from aiops.context.extract import extract_function, with_line_numbers
from aiops.context.stacktrace import parse_python_traceback
from aiops.rag.store import RunbookStore

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

store = RunbookStore(path="chroma_data")
loaded = store.load_directory("runbooks/default")
print(f"Runbook chunks stored in ChromaDB: {store.count()} (processed {loaded})\n")

for service, trace in CRASHES:
    parsed = parse_python_traceback(trace)
    frame = parsed.crash_frame
    print("=" * 70)
    print(f"SERVICE: {service}")
    print(f"ERROR:   {parsed.signature}")
    print(f"CRASHED: {frame.file}, line {frame.line}, in {frame.function}")

    code = extract_function(frame.file, frame.line, REPO_ROOT)
    print("\nBROKEN FUNCTION:")
    print(with_line_numbers(code) if code else "  (could not extract)")

    print("\nBEST RUNBOOKS:")
    for hit in store.get_relevant_context(service, parsed.signature, top_k=2):
        print(f"  - {hit['title']}  [{hit['source']}, severity={hit['severity']}, distance={hit['distance']}]")
    print()
