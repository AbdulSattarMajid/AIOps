from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Optional, Protocol, Tuple

from aiops.agent.diff_validator import validate_diff
from aiops.delivery.pr_text import make_branch_name, render_pr


class DeliveryError(RuntimeError):
    pass


def redact(text: str, secrets) -> str:
    safe = str(text)
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    return safe


class GitHubClient(Protocol):
    def default_branch(self) -> str: ...
    def branch_sha(self, branch: str) -> str: ...
    def branch_exists(self, name: str) -> bool: ...
    def create_branch(self, name: str, sha: str) -> None: ...
    def get_file(self, path: str, ref: str) -> Optional[Tuple[str, str]]: ...
    def update_file(self, path: str, message: str, content: str,
                    blob_sha: str, branch: str) -> None: ...
    def find_open_pr(self, head_branch: str) -> Optional[str]: ...
    def create_pr(self, title: str, body: str, head: str, base: str,
                  draft: bool) -> str: ...


@dataclass
class DeliveryResult:
    status: str
    pr_url: Optional[str]
    branch: str
    message: str


class PyGithubClient:
    def __init__(self, token: str, repository: str):
        self.token = token
        self.secrets = [token]
        self.repository = repository
        try:
            from github import Auth, Github

            self.github = Github(auth=Auth.Token(token))
            self.repo = self.github.get_repo(repository)
        except Exception as error:
            raise DeliveryError(
                redact(f"Could not connect to GitHub: {error}", self.secrets)
            ) from None

    def _call(self, operation):
        try:
            return operation()
        except Exception as error:
            raise DeliveryError(
                redact(f"GitHub request failed: {error}", self.secrets)
            ) from None

    def default_branch(self) -> str:
        return self._call(lambda: self.repo.default_branch)

    def branch_sha(self, branch: str) -> str:
        return self._call(lambda: self.repo.get_branch(branch).commit.sha)

    def branch_exists(self, name: str) -> bool:
        try:
            self.repo.get_branch(name)
            return True
        except Exception as error:
            if getattr(error, "status", None) == 404:
                return False
            raise DeliveryError(
                redact(f"GitHub request failed: {error}", self.secrets)
            ) from None

    def create_branch(self, name: str, sha: str) -> None:
        self._call(lambda: self.repo.create_git_ref(
            ref=f"refs/heads/{name}", sha=sha
        ))

    def get_file(self, path: str, ref: str) -> Optional[Tuple[str, str]]:
        def read():
            try:
                content = self.repo.get_contents(path, ref=ref)
            except Exception as error:
                if getattr(error, "status", None) == 404:
                    return None
                raise
            if isinstance(content, list):
                raise DeliveryError("Expected a single file at the requested path.")
            return content.decoded_content.decode("utf-8"), content.sha

        return self._call(read)

    def update_file(self, path: str, message: str, content: str,
                    blob_sha: str, branch: str) -> None:
        self._call(lambda: self.repo.update_file(
            path, message, content, blob_sha, branch=branch
        ))

    def find_open_pr(self, head_branch: str) -> Optional[str]:
        def find():
            owner = self.repo.owner.login
            pulls = self.repo.get_pulls(
                state="open", head=f"{owner}:{head_branch}"
            )
            pull = next(iter(pulls), None)
            return pull.html_url if pull else None

        return self._call(find)

    def create_pr(self, title: str, body: str, head: str, base: str,
                  draft: bool) -> str:
        pull = self._call(lambda: self.repo.create_pull(
            title=title, body=body, head=head, base=base, draft=draft
        ))
        return pull.html_url


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _safe_repo_path(path_prefix: str, rel_path: str) -> Optional[str]:
    if not isinstance(rel_path, str) or not rel_path:
        return None
    if not isinstance(path_prefix, str):
        return None
    path = path_prefix + rel_path
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or PurePosixPath(path).is_absolute()
        or PureWindowsPath(path).is_absolute()
    ):
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def deliver(summary: dict, incident: dict, client: GitHubClient,
            path_prefix: str = "", allow_unverified: bool = False,
            dry_run: bool = False, slack=None) -> DeliveryResult:
    status = summary.get("status")
    unverified = status == "patch_ready"
    if status != "verified" and not (unverified and allow_unverified):
        return DeliveryResult(
            "refused", None, "",
            f"Incident status {status!r} is not approved for delivery.",
        )

    rel_path = summary.get("file", "")
    file_source = summary.get("file_source")
    patch = summary.get("patch", "")
    if not isinstance(file_source, str) or not isinstance(patch, str) or not patch:
        return DeliveryResult(
            "refused", None, "",
            "The analyzed file source or validated patch is missing.",
        )
    validation = validate_diff(
        patch, rel_path, file_source, summary.get("function_range")
    )
    if not validation.ok:
        return DeliveryResult(
            "refused", None, "",
            f"The patch failed validation again: {validation.error}",
        )
    patched_source = validation.new_source
    if summary.get("final_source") is not None \
            and _normalise_newlines(summary["final_source"]) \
            != _normalise_newlines(patched_source):
        return DeliveryResult(
            "refused", None, "",
            "The supplied final source does not match the validated patch.",
        )
    repo_path = _safe_repo_path(path_prefix, rel_path)
    if repo_path is None:
        return DeliveryResult(
            "refused", None, "",
            f"Unsafe repository file path: {rel_path!r}.",
        )

    secrets = getattr(client, "secrets", [])
    try:
        base = client.default_branch()
        remote_file = client.get_file(repo_path, base)
        if remote_file is None or _normalise_newlines(remote_file[0]) \
                != _normalise_newlines(file_source):
            return DeliveryResult(
                "refused", None, "",
                "The file changed since the analysis; run the analysis again.",
            )
        branch = make_branch_name(incident.get("id", 0), patch)
        if branch == base:
            return DeliveryResult(
                "refused", None, branch,
                "Refusing to modify the default branch.",
            )
        if client.branch_exists(branch):
            existing_url = client.find_open_pr(branch)
            return DeliveryResult(
                "already_open", existing_url, branch,
                "The generated branch already exists; no second PR was created.",
            )
        title, body = render_pr(summary, incident, unverified=unverified)
        if dry_run:
            return DeliveryResult(
                "dry_run", None, branch,
                f"Planned PR for {repo_path}: {title}",
            )
        base_sha = client.branch_sha(base)
        client.create_branch(branch, base_sha)
        client.update_file(
            repo_path,
            f"AIOps fix for incident {incident.get('id', 0)}",
            patched_source,
            remote_file[1],
            branch,
        )
        pr_url = client.create_pr(
            title, body, head=branch, base=base, draft=True
        )
        if slack is not None:
            try:
                slack(pr_url)
            except Exception:
                pass
        return DeliveryResult("opened", pr_url, branch, "Draft PR opened.")
    except Exception as error:
        message = redact(f"GitHub delivery failed: {error}", secrets)
        return DeliveryResult("refused", None, "", message)
