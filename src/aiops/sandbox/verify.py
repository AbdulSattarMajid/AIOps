import os
from pathlib import Path
from typing import Dict, Optional

from aiops.sandbox.runner import SandboxResult, run_tests_in_sandbox

_MAX_FILE_BYTES = 200 * 1024
_DENIED_NAMES = {".env", "credentials", "secrets.json"}
_DENIED_SUFFIXES = (".pem", ".key")


def collect_app_files(app_dir: str) -> Dict[str, str]:
    root = Path(app_dir).resolve()
    files = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories
            if name != "__pycache__"
            and not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink():
                continue
            if filename.lower() in _DENIED_NAMES or filename.lower().endswith(
                _DENIED_SUFFIXES
            ):
                continue
            if path.suffix != ".py":
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                relative = path.relative_to(root).as_posix()
                files[relative] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if len(files) > 200:
                raise ValueError("The app contains more than 200 Python files.")
    return files


def run_app_tests(app_dir: str, overrides: Optional[Dict[str, str]] = None,
                  **kwargs) -> SandboxResult:
    files = collect_app_files(app_dir)
    if overrides:
        files.update(overrides)
    return run_tests_in_sandbox(files, **kwargs)


def verify_patch(app_dir: str, rel_path: str, new_source: str,
                 **kwargs) -> SandboxResult:
    files = collect_app_files(app_dir)
    if rel_path not in files:
        raise ValueError(f"Patch path {rel_path!r} is not an existing app file.")
    files[rel_path] = new_source
    return run_tests_in_sandbox(files, **kwargs)
