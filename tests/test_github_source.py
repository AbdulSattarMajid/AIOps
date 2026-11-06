from __future__ import annotations

import logging
import stat
import zipfile
from dataclasses import dataclass
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tests.conftest import DEVELOPER_SETTINGS_TO_CLEAR
from aiops.context import github_source
from aiops.context.extract import resolve_repo_path
from aiops.delivery.github_pr import DeliveryResult, _safe_repo_path
from aiops.worker import worker

TOKEN = "github-token-that-must-not-leak"
SETTINGS = {
    "AIOPS_CODE_SOURCE": "github",
    "GITHUB_REPO": "example/shop",
    "GITHUB_TOKEN": TOKEN,
    "GITHUB_REF": "feature/demo",
}


def test_autouse_fixture_clears_github_source_settings():
    assert "AIOPS_CODE_SOURCE" in DEVELOPER_SETTINGS_TO_CLEAR
    assert "GITHUB_REF" in DEVELOPER_SETTINGS_TO_CLEAR


def _zip_bytes(files: dict[str, bytes] | None = None) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("example-shop-abc123/", "")
        for name, contents in (files or {}).items():
            archive.writestr(f"example-shop-abc123/{name}", contents)
    return output.getvalue()


def _downloader_for(payload: bytes, calls: list[tuple[str, str, str | None]]):
    def download(repository: str, token: str, ref: str | None) -> bytes:
        calls.append((repository, token, ref))
        return payload

    return download


def test_github_snapshot_extracts_repository_root_and_uses_settings(tmp_path):
    calls = []
    archive = _zip_bytes({"demo_shop/store.py": b"def cart_total(): pass\n"})
    with github_source.repository_source(
        tmp_path,
        environ=SETTINGS,
        downloader=_downloader_for(archive, calls),
    ) as source:
        root = Path(source.repo_root)
        assert source.path_prefix == "/app/"
        assert (root / "demo_shop" / "store.py").read_text() == (
            "def cart_total(): pass\n"
        )
        snapshot_root = root
    assert not snapshot_root.exists()
    assert calls == [("example/shop", TOKEN, "feature/demo")]


@pytest.mark.parametrize(
    "member",
    ["../evil.py", "/rooted.py", "C:/drive.py", "repo/../../outside.py"],
)
def test_snapshot_rejects_unsafe_zip_paths(member, tmp_path):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(member, "not safe")

    with pytest.raises(github_source.GitHubSourceError, match="unsafe"):
        github_source.extract_repository_zip(output.getvalue(), tmp_path / "extract")


def test_snapshot_rejects_download_larger_than_limit(tmp_path):
    with pytest.raises(github_source.GitHubSourceError, match="20 MB"):
        github_source.extract_repository_zip(
            b"x" * (github_source.MAX_DOWNLOAD_BYTES + 1),
            tmp_path / "extract",
        )


def test_snapshot_rejects_more_than_5000_files(tmp_path):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(github_source.MAX_FILES + 1):
            archive.writestr(f"repo/file-{index}.txt", "")

    with pytest.raises(github_source.GitHubSourceError, match="5,000"):
        github_source.extract_repository_zip(output.getvalue(), tmp_path / "extract")


def test_snapshot_rejects_unpacked_size_over_limit(tmp_path):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "repo/large.txt",
            b"x" * (github_source.MAX_UNPACKED_BYTES + 1),
        )

    with pytest.raises(github_source.GitHubSourceError, match="50 MB"):
        github_source.extract_repository_zip(output.getvalue(), tmp_path / "extract")


def test_snapshot_rejects_symlink_members(tmp_path):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("repo/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "target.py")

    with pytest.raises(github_source.GitHubSourceError, match="symbolic link"):
        github_source.extract_repository_zip(output.getvalue(), tmp_path / "extract")


def test_snapshot_directory_is_removed_if_archive_extraction_fails(
    monkeypatch, tmp_path
):
    created: list[Path] = []
    real_temporary_directory = github_source.tempfile.TemporaryDirectory

    def tracking_temporary_directory(*args, **kwargs):
        directory = real_temporary_directory(*args, **kwargs)
        created.append(Path(directory.name))
        return directory

    monkeypatch.setattr(
        github_source.tempfile,
        "TemporaryDirectory",
        tracking_temporary_directory,
    )
    with (
        pytest.raises(github_source.GitHubSourceError, match="unsafe"),
        github_source.github_repository_snapshot(
            "example/shop",
            TOKEN,
            downloader=_downloader_for(_zip_bytes({"../escape.py": b"no"}), []),
        ),
    ):
        pytest.fail("unsafe archive unexpectedly succeeded")

    assert len(created) == 1
    assert not created[0].exists()


def test_download_uses_auth_header_and_short_timeout_without_leaking_token(
    monkeypatch, caplog
):
    observed = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def getcode(self):
            return self.status

        def read(self, size):
            observed["read_size"] = size
            return b"fake archive"

    class FakeOpener:
        def open(self, request, timeout):
            observed["url"] = request.full_url
            observed["authorization"] = request.get_header("Authorization")
            observed["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(
        github_source, "build_opener", lambda *_handlers: FakeOpener()
    )
    with caplog.at_level(logging.ERROR):
        payload = github_source.download_repository_zip(
            "example/shop", TOKEN, "feature/demo"
        )

    assert payload == b"fake archive"
    assert observed["url"].endswith("/zipball/feature%2Fdemo")
    assert observed["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in observed["url"]
    assert observed["timeout"] <= 5
    assert observed["read_size"] == github_source.MAX_DOWNLOAD_BYTES + 1
    assert TOKEN not in caplog.text


def _redirect(location: str) -> HTTPError:
    headers = Message()
    headers["Location"] = location
    return HTTPError(
        "https://api.github.com/archive",
        302,
        "Found",
        headers,
        None,
    )


class _ArchiveResponse:
    def __init__(self, payload: bytes = b"archive"):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def getcode(self):
        return 200

    def read(self, size):
        assert size == github_source.MAX_DOWNLOAD_BYTES + 1
        return self.payload


def test_redirect_download_omits_authorization_on_second_request(monkeypatch):
    observed = []
    responses = [
        _redirect("https://codeload.github.com/example/shop/legacy.zip/main"),
        _ArchiveResponse(b"redirected archive"),
    ]

    class FakeOpener:
        def open(self, request, timeout):
            observed.append((request, timeout))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(
        github_source, "build_opener", lambda *_handlers: FakeOpener()
    )
    payload = github_source.download_repository_zip("example/shop", TOKEN)

    assert payload == b"redirected archive"
    assert len(observed) == 2
    assert observed[0][0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert observed[1][0].get_header("Authorization") is None
    assert observed[0][1] == observed[1][1] == github_source.DOWNLOAD_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "location",
    [
        "http://github.com/archive.zip",
        "https://evil.example.com/archive.zip",
        "https://github.com.evil.example.com/archive.zip",
    ],
)
def test_untrusted_redirect_locations_are_rejected_without_leaking_token(
    monkeypatch, location
):
    class FakeOpener:
        def open(self, _request, timeout):
            raise _redirect(location)

    monkeypatch.setattr(
        github_source, "build_opener", lambda *_handlers: FakeOpener()
    )
    with pytest.raises(github_source.GitHubSourceError) as error:
        github_source.download_repository_zip("example/shop", TOKEN)

    assert "redirect" in str(error.value)
    assert location not in str(error.value)
    assert TOKEN not in str(error.value)


def test_second_redirect_is_rejected_without_leaking_token(monkeypatch):
    observed = []

    class FakeOpener:
        def open(self, request, timeout):
            observed.append(request)
            if len(observed) == 1:
                raise _redirect("https://codeload.github.com/archive.zip")
            raise _redirect("https://github.com/another-redirect.zip")

    monkeypatch.setattr(
        github_source, "build_opener", lambda *_handlers: FakeOpener()
    )
    with pytest.raises(github_source.GitHubSourceError) as error:
        github_source.download_repository_zip("example/shop", TOKEN)

    assert len(observed) == 2
    assert observed[1].get_header("Authorization") is None
    assert TOKEN not in str(error.value)


def test_direct_200_response_is_read_without_redirect(monkeypatch):
    class FakeOpener:
        def open(self, _request, timeout):
            return _ArchiveResponse(b"direct archive")

    monkeypatch.setattr(
        github_source, "build_opener", lambda *_handlers: FakeOpener()
    )

    assert (
        github_source.download_repository_zip("example/shop", TOKEN)
        == b"direct archive"
    )


def test_github_snapshot_is_removed_when_analysis_body_raises(tmp_path):
    archive = _zip_bytes({"app.py": b"pass\n"})
    with (
        pytest.raises(RuntimeError, match="analysis failed"),
        github_source.github_repository_snapshot(
            "example/shop",
            TOKEN,
            downloader=_downloader_for(archive, []),
        ) as repo_root,
    ):
        snapshot_root = repo_root.parent
        raise RuntimeError("analysis failed")

    assert not snapshot_root.exists()


def test_download_failures_do_not_expose_token_or_log_it(caplog):
    def fail(_repository: str, _token: str, _ref: str | None) -> bytes:
        raise RuntimeError(f"transport rejected {TOKEN}")

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(github_source.GitHubSourceError) as error,
        github_source.github_repository_snapshot(
            "example/shop", TOKEN, downloader=fail
        ),
    ):
        pytest.fail("snapshot unexpectedly succeeded")

    assert TOKEN not in str(error.value)
    assert TOKEN not in caplog.text


def test_env_file_inside_snapshot_is_still_refused(tmp_path):
    archive = _zip_bytes({".env": b"SECRET=value\n"})
    with github_source.github_repository_snapshot(
        "example/shop",
        TOKEN,
        downloader=_downloader_for(archive, []),
    ) as repo_root:
        assert resolve_repo_path("/app/.env", str(repo_root), "/app/") is None


def test_unset_source_keeps_local_root_without_downloading(tmp_path):
    with github_source.repository_source(
        tmp_path,
        environ={},
        downloader=lambda *_: pytest.fail("local source must not download"),
    ) as source:
        assert source.repo_root == str(tmp_path)
        assert source.path_prefix is None


def test_analyze_endpoint_defaults_to_original_local_repository(
    monkeypatch, tmp_path
):
    from aiops.api import main

    monkeypatch.delenv("AIOPS_CODE_SOURCE", raising=False)
    monkeypatch.setattr(main, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "engine", _Engine())
    observed = {}
    monkeypatch.setattr(
        main,
        "run_agent",
        lambda _llm, _store, **kwargs: observed.update(kwargs) or {},
    )
    monkeypatch.setattr(main, "summarize", lambda _state: {"status": "no_patch"})

    main.analyze_incident(
        incident_id=17,
        slot_info=(None, None),
        llm=None,
        store=None,
        sandbox=None,
        deliver_requested=False,
        github=None,
    )

    assert observed["repo_root"] == str(tmp_path)
    assert "path_prefix" not in observed


@dataclass
class _Incident:
    id: int = 17
    service_name: str = "shop-service"
    sample_trace: str = "trace"
    signature: str = "signature"
    count: int = 1


class _Engine:
    def get_incident(self, _incident_id):
        return _Incident()


def test_analyze_endpoint_passes_snapshot_root_and_pr_uses_patch_path(
    monkeypatch, tmp_path
):
    from aiops.api import main

    archive = _zip_bytes({"demo_shop/store.py": b"def cart_total(): pass\n"})
    calls = []
    monkeypatch.setattr(main, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "engine", _Engine())
    monkeypatch.setattr(main, "get_sandbox_for_repo", lambda _root: None)
    monkeypatch.setattr(
        main,
        "repository_source",
        lambda root: github_source.repository_source(
            root,
            environ=SETTINGS,
            downloader=_downloader_for(archive, calls),
        ),
    )
    run_args = {}

    def run_agent(_llm, _store, **kwargs):
        run_args.update(kwargs)
        assert Path(kwargs["repo_root"], "demo_shop", "store.py").is_file()
        return {"analysis": "complete"}

    monkeypatch.setattr(main, "run_agent", run_agent)
    monkeypatch.setattr(
        main,
        "summarize",
        lambda _state: {
            "status": "verified",
            "file": "demo_shop/store.py",
            "patch": "validated patch",
        },
    )
    delivery = {}

    def fake_deliver(summary, _incident, _github, *, path_prefix, slack):
        delivery["path_prefix"] = path_prefix
        delivery["repo_path"] = _safe_repo_path(path_prefix, summary["file"])
        return DeliveryResult("opened", "https://github.invalid/pr/1", "branch", "ok")

    monkeypatch.setattr(main, "deliver", fake_deliver)
    result = main.analyze_incident(
        incident_id=17,
        slot_info=(None, None),
        llm=None,
        store=None,
        sandbox=None,
        deliver_requested=True,
        github=object(),
    )

    assert run_args["path_prefix"] == "/app/"
    assert run_args["repo_root"] != str(tmp_path)
    assert delivery["path_prefix"] == ""
    assert delivery["repo_path"] == "demo_shop/store.py"
    assert result["delivery"]["status"] == "opened"
    assert not Path(run_args["repo_root"]).exists()


def test_worker_analyzer_passes_github_snapshot_to_agent(monkeypatch, tmp_path):
    from aiops.api import main

    archive = _zip_bytes({"demo_shop/store.py": b"def cart_total(): pass\n"})
    monkeypatch.setattr(main, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "get_llm", lambda: None)
    monkeypatch.setattr(main, "get_store", lambda: None)
    monkeypatch.setattr(main, "get_sandbox_for_repo", lambda _root: None)
    monkeypatch.setattr(
        worker,
        "repository_source",
        lambda root: github_source.repository_source(
            root,
            environ=SETTINGS,
            downloader=_downloader_for(archive, []),
        ),
    )
    observed = {}

    def run_agent(_llm, _store, **kwargs):
        observed.update(kwargs)
        return {"state": "ok"}

    monkeypatch.setattr(main, "run_agent", run_agent)
    monkeypatch.setattr(main, "summarize", lambda state: state)
    result = worker._default_auto_analyzer(_Incident(), limits=None)

    assert observed["path_prefix"] == "/app/"
    assert observed["repo_root"] != str(tmp_path)
    assert result == {"state": "ok"}
    assert not Path(observed["repo_root"]).exists()


def test_source_failure_is_a_clear_escalation(monkeypatch, tmp_path):
    from aiops.api import main

    monkeypatch.setattr(main, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "engine", _Engine())
    monkeypatch.setattr(
        main,
        "repository_source",
        lambda root: github_source.repository_source(
            root,
            environ=SETTINGS,
            downloader=lambda *_: (_ for _ in ()).throw(
                RuntimeError(f"download failed with {TOKEN}")
            ),
        ),
    )

    result = main.analyze_incident(
        incident_id=17,
        slot_info=(None, None),
        llm=None,
        store=None,
        sandbox=None,
        deliver_requested=False,
        github=None,
    )

    assert result["status"] == "escalated"
    assert "Could not download" in result["reason"]
    assert TOKEN not in result["reason"]
