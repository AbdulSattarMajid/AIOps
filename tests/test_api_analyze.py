import pytest
from fastapi.testclient import TestClient

from aiops.agent.fake_llm import make_demo_llm
from aiops.api import main
from aiops.dedup.engine import DedupEngine
from aiops.rag.store import RunbookStore
from aiops.sandbox.runner import SandboxResult
from tests.conftest import TEST_API_KEY


@pytest.fixture
def client_and_store(tmp_path):
    main.engine = DedupEngine()
    main.app.dependency_overrides.clear()
    store = RunbookStore(path=str(tmp_path / "chroma"))
    store.load_directory("runbooks/default")
    main.app.dependency_overrides[main.get_store] = lambda: store
    main.app.dependency_overrides[main.get_sandbox] = lambda: None
    main.app.dependency_overrides.pop(main.get_github, None)
    with TestClient(main.app, headers={"X-API-Key": TEST_API_KEY}) as client:
        yield client, store
    main.app.dependency_overrides.clear()


def _send_users_crash(client):
    return client.post("/api/v1/telemetry/logs", json={
        "timestamp": "2026-11-03T10:00:00Z",
        "service_name": "user-service",
        "error_class": "AttributeError",
        "message": "'NoneType' object has no attribute 'email'",
        "stack_trace": (
            '  File "/app/users.py", line 19, in get_user_email\n'
            "    return user.email\n"
            "AttributeError: 'NoneType' object has no attribute 'email'"
        ),
    }).json()["incident_id"]


def test_analyze_endpoint_returns_patch(client_and_store):
    client, _ = client_and_store
    main.app.dependency_overrides[main.get_llm] = make_demo_llm
    incident_id = _send_users_crash(client)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/analyze"
    )
    assert response.status_code == 200
    assert response.json()["status"] == "patch_ready"
    assert "delivery" not in response.json()


def test_unknown_incident_returns_404(client_and_store):
    client, _ = client_and_store
    main.app.dependency_overrides[main.get_llm] = make_demo_llm
    response = client.post("/api/v1/incidents/999/analyze")
    assert response.status_code == 404


def test_analyze_without_api_key_returns_503(client_and_store, monkeypatch):
    client, _ = client_and_store
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("AIOPS_LLM", raising=False)
    incident_id = _send_users_crash(client)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/analyze"
    )
    assert response.status_code == 503


def test_analyze_without_model_returns_503(client_and_store, monkeypatch):
    client, _ = client_and_store
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("AIOPS_LLM", raising=False)
    incident_id = _send_users_crash(client)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/analyze"
    )
    assert response.status_code == 503
    assert "LLM_MODEL" in response.json()["detail"]


def test_analyze_includes_sandbox_verification(client_and_store):
    class FakeSandbox:
        def baseline(self):
            return SandboxResult(
                "failed", 1, "", 0.1, 4, 2,
                failed_tests=["test_users.py::test_missing_user_gives_clear_error_not_attributeerror"],
            )

        def verify(self, rel_path, new_source):
            return SandboxResult("passed", 0, "", 0.1, 6, 0)

    client, _ = client_and_store
    main.app.dependency_overrides[main.get_llm] = make_demo_llm
    main.app.dependency_overrides[main.get_sandbox] = lambda: FakeSandbox()
    incident_id = _send_users_crash(client)
    response = client.post(f"/api/v1/incidents/{incident_id}/analyze")
    assert response.status_code == 200
    assert response.json()["verification"]["status"] == "fix_confirmed"


def test_analyze_delivery_uses_overridden_github(client_and_store):
    class FakeSandbox:
        def baseline(self):
            return SandboxResult(
                "failed", 1, "", 0.1, 4, 2,
                failed_tests=[
                    "test_users.py::test_missing_user_gives_clear_error_not_attributeerror"
                ],
            )

        def verify(self, rel_path, new_source):
            return SandboxResult("passed", 0, "", 0.1, 6, 0)

    class FakeGitHub:
        def __init__(self):
            self.content = open(
                "mock_services/sample_app/users.py", encoding="utf-8"
            ).read()
            self.writes = []
            self.secrets = []

        def default_branch(self):
            return "main"

        def get_file(self, path, ref):
            return self.content, "blob-sha"

        def branch_exists(self, name):
            return False

        def branch_sha(self, branch):
            return "base-sha"

        def create_branch(self, name, sha):
            self.writes.append(("branch", name, sha))

        def update_file(self, path, message, content, blob_sha, branch):
            self.writes.append(("file", path, content, branch))

        def create_pr(self, title, body, head, base, draft):
            self.writes.append(("pr", head, base, draft))
            return "https://github.com/example/app/pull/42"

        def find_open_pr(self, head_branch):
            return None

    client, _ = client_and_store
    github = FakeGitHub()
    main.app.dependency_overrides[main.get_llm] = make_demo_llm
    main.app.dependency_overrides[main.get_sandbox] = lambda: FakeSandbox()
    main.app.dependency_overrides[main.get_github] = lambda: github
    incident_id = _send_users_crash(client)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/analyze?deliver=true"
    )
    assert response.status_code == 200
    delivery = response.json()["delivery"]
    assert delivery["status"] == "opened"
    assert delivery["pr_url"] == "https://github.com/example/app/pull/42"
    assert github.writes[-1][3] is True


def test_analyze_delivery_without_github_config_returns_503(
    client_and_store, monkeypatch
):
    client, _ = client_and_store
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    main.app.dependency_overrides[main.get_llm] = make_demo_llm
    incident_id = _send_users_crash(client)
    response = client.post(
        f"/api/v1/incidents/{incident_id}/analyze?deliver=true"
    )
    assert response.status_code == 503
    assert "GITHUB_TOKEN and GITHUB_REPO" in response.json()["detail"]
