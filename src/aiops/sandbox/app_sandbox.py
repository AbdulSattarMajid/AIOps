from aiops.sandbox.runner import SandboxResult
from aiops.sandbox.verify import run_app_tests, verify_patch


class AppSandbox:
    def __init__(self, app_dir, **runner_kwargs):
        self.app_dir = app_dir
        self.runner_kwargs = runner_kwargs

    def baseline(self) -> SandboxResult:
        return run_app_tests(self.app_dir, **self.runner_kwargs)

    def verify(self, rel_path: str, new_source: str) -> SandboxResult:
        return verify_patch(
            self.app_dir, rel_path, new_source, **self.runner_kwargs
        )
