"""Bounded GitHub repository snapshots for incident analysis."""
from __future__ import annotations

import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote
from urllib.request import Request, urlopen

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_FILES = 5_000
DOWNLOAD_TIMEOUT_SECONDS = 5


class GitHubSourceError(RuntimeError):
    """A safe, user-presentable repository snapshot failure."""


@dataclass(frozen=True)
class RepositorySource:
    repo_root: str
    path_prefix: str | None = None


def download_repository_zip(
    repository: str, token: str, ref: str | None = None
) -> bytes:
    """Download a repository archive without including credentials in errors."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise GitHubSourceError("GITHUB_REPO must be an owner/name repository.")
    suffix = f"/{quote(ref, safe='')}" if ref else ""
    url = f"https://api.github.com/repos/{repository}/zipball{suffix}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            archive = response.read(MAX_DOWNLOAD_BYTES + 1)
    except Exception:  # noqa: BLE001 - prevent transport details leaking credentials
        raise GitHubSourceError(
            "Could not download the GitHub repository snapshot."
        ) from None
    if len(archive) > MAX_DOWNLOAD_BYTES:
        raise GitHubSourceError(
            "GitHub repository snapshot exceeds the 20 MB download limit."
        )
    return archive


def _validated_members(
    archive: zipfile.ZipFile,
) -> tuple[list[tuple[zipfile.ZipInfo, str]], str | None]:
    members: list[tuple[zipfile.ZipInfo, str]] = []
    file_count = 0
    unpacked_size = 0
    top_levels: set[str] = set()
    has_top_level_directory = False

    for info in archive.infolist():
        raw_name = info.filename
        name = raw_name.replace("\\", "/")
        path = PurePosixPath(name)
        windows_path = PureWindowsPath(name)
        if (
            not name
            or name.startswith("/")
            or path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
        ):
            raise GitHubSourceError(
                "GitHub repository snapshot contains an unsafe archive path."
            )

        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise GitHubSourceError(
                "GitHub repository snapshot contains a symbolic link."
            )

        is_directory = name.endswith("/")
        if not is_directory:
            file_count += 1
            unpacked_size += info.file_size
            if file_count > MAX_FILES:
                raise GitHubSourceError(
                    "GitHub repository snapshot exceeds the 5,000 file limit."
                )
            if unpacked_size > MAX_UNPACKED_BYTES:
                raise GitHubSourceError(
                    "GitHub repository snapshot exceeds the 50 MB unpacked limit."
                )

        parts = name.rstrip("/").split("/")
        top_levels.add(parts[0])
        if is_directory and len(parts) == 1:
            has_top_level_directory = True
        members.append((info, name))

    strip_root = None
    if len(top_levels) == 1:
        candidate = next(iter(top_levels))
        if has_top_level_directory or all(
            name.count("/") > 0
            for info, name in members
            if not name.endswith("/")
        ):
            strip_root = candidate
    return members, strip_root


def extract_repository_zip(archive_data: bytes, destination: Path) -> Path:
    """Safely extract a bounded archive and return its repository root."""
    if len(archive_data) > MAX_DOWNLOAD_BYTES:
        raise GitHubSourceError(
            "GitHub repository snapshot exceeds the 20 MB download limit."
        )
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(BytesIO(archive_data)) as archive:
            members, strip_root = _validated_members(archive)
            actual_unpacked_size = 0
            for info, name in members:
                relative_name = name
                if strip_root is not None:
                    if name.rstrip("/") == strip_root:
                        continue
                    relative_name = name[len(strip_root):].lstrip("/")
                if not relative_name:
                    continue
                target = (destination / Path(*relative_name.rstrip("/").split("/"))).resolve()
                try:
                    target.relative_to(destination.resolve())
                except ValueError:
                    raise GitHubSourceError(
                        "GitHub repository snapshot contains an unsafe archive path."
                    ) from None
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    copied = 0
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        actual_unpacked_size += len(chunk)
                        if actual_unpacked_size > MAX_UNPACKED_BYTES:
                            raise GitHubSourceError(
                                "GitHub repository snapshot exceeds the 50 MB unpacked limit."
                            )
                        output.write(chunk)
                    if copied != info.file_size:
                        raise GitHubSourceError(
                            "Could not safely extract the GitHub repository snapshot."
                        )
    except GitHubSourceError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
        raise GitHubSourceError(
            "Could not safely extract the GitHub repository snapshot."
        ) from None
    return destination


@contextmanager
def github_repository_snapshot(
    repository: str,
    token: str,
    *,
    ref: str | None = None,
    downloader: Callable[[str, str, str | None], bytes] | None = None,
) -> Iterator[Path]:
    """Yield a temporary repository root, removing it on every exit path."""
    fetch = download_repository_zip if downloader is None else downloader
    try:
        archive_data = fetch(repository, token, ref)
    except GitHubSourceError:
        raise
    except Exception:  # noqa: BLE001 - downloader errors may contain credentials
        raise GitHubSourceError(
            "Could not download the GitHub repository snapshot."
        ) from None

    try:
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="aiops-github-source-"
        )
    except OSError:
        raise GitHubSourceError(
            "Could not prepare the GitHub repository snapshot."
        ) from None
    with temporary_directory as temp_dir:
        repo_root = Path(temp_dir) / "repository"
        try:
            extract_repository_zip(archive_data, repo_root)
        except GitHubSourceError:
            raise
        except Exception:  # noqa: BLE001 - return a sanitized source failure
            raise GitHubSourceError(
                "Could not prepare the GitHub repository snapshot."
            ) from None
        yield repo_root


@contextmanager
def repository_source(
    local_repo_root: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
    downloader: Callable[[str, str, str | None], bytes] | None = None,
) -> Iterator[RepositorySource]:
    """Yield the configured source and clean up any GitHub snapshot on exit."""
    settings = os.environ if environ is None else environ
    source = settings.get("AIOPS_CODE_SOURCE", "local").strip().lower()
    if source == "local":
        yield RepositorySource(repo_root=str(local_repo_root))
        return
    if source != "github":
        raise GitHubSourceError(
            "AIOPS_CODE_SOURCE must be either 'local' or 'github'."
        )

    repository = settings.get("GITHUB_REPO", "")
    token = settings.get("GITHUB_TOKEN", "")
    ref = settings.get("GITHUB_REF") or None
    if not repository or not token:
        raise GitHubSourceError(
            "GitHub code source requires GITHUB_REPO and GITHUB_TOKEN."
        )
    with github_repository_snapshot(
        repository, token, ref=ref, downloader=downloader
    ) as repo_root:
        yield RepositorySource(repo_root=str(repo_root), path_prefix="/app/")


def uses_github_source(environ: dict[str, str] | None = None) -> bool:
    settings = os.environ if environ is None else environ
    return settings.get("AIOPS_CODE_SOURCE", "local").strip().lower() == "github"
