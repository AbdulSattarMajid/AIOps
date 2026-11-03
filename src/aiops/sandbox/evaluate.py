from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import List, Optional

from aiops.sandbox.runner import SandboxResult


@dataclass
class Verification:
    status: str
    tests_passed: Optional[int] = None
    tests_failed: Optional[int] = None
    newly_failing: List[str] = field(default_factory=list)
    still_failing: List[str] = field(default_factory=list)
    fixed: List[str] = field(default_factory=list)
    message: str = ""


def evaluate_verification(baseline: SandboxResult, after: SandboxResult,
                          module_stem: str) -> Verification:
    baseline_failures = set(baseline.failed_tests)
    after_failures = set(after.failed_tests)
    newly_failing = sorted(after_failures - baseline_failures)
    fixed = sorted(baseline_failures - after_failures)
    target_before = {
        test for test in baseline_failures
        if PurePosixPath(test.split("::", 1)[0]).name
        == f"test_{module_stem}.py"
    }
    still_failing = sorted(target_before & after_failures)
    common = {
        "tests_passed": after.tests_passed,
        "tests_failed": after.tests_failed,
        "newly_failing": newly_failing,
        "still_failing": still_failing,
        "fixed": fixed,
    }
    if after.status == "error":
        return Verification(
            "unavailable", message=after.message or "The sandbox could not run tests.",
            **common,
        )
    if after.status == "timeout":
        return Verification(
            "failed",
            message="The tests timed out (possible infinite loop or very slow code).",
            **common,
        )
    if newly_failing:
        return Verification(
            "failed",
            message="Newly failing tests: " + ", ".join(newly_failing),
            **common,
        )
    if still_failing:
        return Verification(
            "failed",
            message="The bug's tests still fail: " + ", ".join(still_failing),
            **common,
        )
    if target_before:
        return Verification(
            "fix_confirmed",
            message="The previously failing tests for this file now pass.",
            **common,
        )
    return Verification(
        "no_regressions",
        message="No tests broke, but no failing test shows this bug is fixed.",
        **common,
    )
