import os
from typing import Optional

from aiops.sandbox.deps import find_requirements_files, get_or_build_deps_image
from aiops.sandbox.runner import SandboxResult
from aiops.sandbox.verify import run_app_tests, verify_patch


class AppSandbox:
    def __init__(self, app_dir, **runner_kwargs):
        self.app_dir = app_dir
        self.runner_kwargs = runner_kwargs
        self._resolved_image: Optional[str] = None
        self._deps_error: Optional[SandboxResult] = None
        self._initialized: bool = False

    def _ensure_image(self) -> Optional[SandboxResult]:
        if self._initialized:
            return self._deps_error
        self._initialized = True

        enabled = os.getenv("AIOPS_SANDBOX_BUILD_DEPS", "off").lower() in (
            "on", "true", "1", "yes"
        )
        base_image = self.runner_kwargs.get(
            "image", os.getenv("AIOPS_SANDBOX_BASE_IMAGE", "aiops-sandbox:py311")
        )
        if not enabled:
            self._resolved_image = base_image
            return None

        req_files = find_requirements_files(self.app_dir)
        if not req_files:
            self._resolved_image = base_image
            return None

        timeout = int(os.getenv("AIOPS_SANDBOX_BUILD_TIMEOUT", "300"))
        client = self.runner_kwargs.get("client")
        result = get_or_build_deps_image(
            repo_root=self.app_dir,
            client=client,
            base_image=base_image,
            timeout=timeout,
        )
        if isinstance(result, SandboxResult):
            self._deps_error = result
            return self._deps_error

        self._resolved_image = result
        return None

    def baseline(self) -> SandboxResult:
        err = self._ensure_image()
        if err is not None:
            return err
        kwargs = dict(self.runner_kwargs)
        if self._resolved_image:
            kwargs["image"] = self._resolved_image
        return run_app_tests(self.app_dir, **kwargs)

    def verify(self, rel_path: str, new_source: str) -> SandboxResult:
        err = self._ensure_image()
        if err is not None:
            return err
        kwargs = dict(self.runner_kwargs)
        if self._resolved_image:
            kwargs["image"] = self._resolved_image
        return verify_patch(
            self.app_dir, rel_path, new_source, **kwargs
        )
