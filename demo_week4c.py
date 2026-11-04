"""Preview or open a draft PR for the verified user-service fix."""
import os
import sys

sys.path.insert(0, "src")

from dotenv import load_dotenv

from aiops.agent.fake_llm import make_demo_llm_4b
from aiops.agent.graph import run_agent, summarize
from aiops.delivery.github_pr import (
    PyGithubClient,
    deliver,
)
from aiops.delivery.pr_text import make_branch_name, render_pr
from aiops.delivery.slack import build_slack_text, post_slack
from aiops.rag.store import RunbookStore
from aiops.sandbox.app_sandbox import AppSandbox
from aiops.sandbox.runner import is_docker_available

APP_DIR = "mock_services/sample_app"
TRACE = """Traceback (most recent call last):
  File "/app/users.py", line 19, in get_user_email
    return user.email
AttributeError: 'NoneType' object has no attribute 'email'"""


def main():
    live = "--live" in sys.argv
    if live:
        load_dotenv()
        token = os.getenv("GITHUB_TOKEN")
        repository = os.getenv("GITHUB_REPO")
        if not token or not repository:
            print(
                "GitHub delivery needs GITHUB_TOKEN and GITHUB_REPO in .env."
            )
            return 1
        print(f"Repository: {repository}")
        if input("Type the repository name exactly to continue: ") != repository:
            print("Confirmation did not match; no GitHub changes were made.")
            return 1
    else:
        token = ""
        repository = ""

    if not is_docker_available():
        print("Docker is not running. Start Docker Desktop and try again.")
        return 1

    store = RunbookStore(path="chroma_data")
    store.load_directory("runbooks/default")
    state = run_agent(
        make_demo_llm_4b(),
        store,
        trace=TRACE,
        service_name="user-service",
        repo_root=APP_DIR,
        incident_id=1,
        sandbox=AppSandbox(APP_DIR),
    )
    summary = summarize(state)
    incident = {
        "id": 1,
        "service_name": "user-service",
        "signature": state.get("signature", ""),
        "occurrences": 1,
    }

    if summary["status"] != "verified":
        print(f"Patch was not verified: {state.get('reason', summary['status'])}")
        return 1

    branch = make_branch_name(incident["id"], summary["patch"])
    path_prefix = os.getenv("TARGET_REPO_PATH_PREFIX", "") if live else ""
    repo_path = path_prefix + summary["file"]
    title, body = render_pr(summary, incident)
    if not live:
        print("DRY RUN: pretend AI, real sandbox, no GitHub or Slack calls.")
        print(f"Title: {title}")
        print(f"Branch: {branch}")
        print(f"Target file: {repo_path}")
        print("Body:\n{}".format(body))
        return 0

    webhook = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook:
        print("(Slack skipped: no webhook)")
        slack_callback = None
    else:
        slack_callback = lambda url: post_slack(
            webhook, build_slack_text(summary, incident, url)
        )
    result = deliver(
        summary,
        incident,
        PyGithubClient(token, repository),
        path_prefix=path_prefix,
        slack=slack_callback,
    )
    print(f"Delivery status: {result.status}")
    if result.pr_url:
        print(f"Draft PR: {result.pr_url}")
    else:
        print(result.message)
    return 0 if result.status == "opened" else 1


if __name__ == "__main__":
    raise SystemExit(main())
