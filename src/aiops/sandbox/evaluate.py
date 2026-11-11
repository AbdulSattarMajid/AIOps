import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional

from aiops.sandbox.runner import SandboxResult

_FAILED_SUMMARY = re.compile(
    r"^FAILED (?P<test>\S+) - (?P<error_class>[A-Za-z_][\w.]*)"
    r":\s*(?P<message>\S(?:.*\S)?)$"
)
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_HEX = re.compile(r"(?<![a-z0-9])(?:0x)?[0-9a-f]{8,}(?![a-z0-9])")
_DIGITS = re.compile(r"\d+")
_QUOTES = re.compile(r"""['"`]""")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Verification:
    status: str
    tests_passed: Optional[int] = None
    tests_failed: Optional[int] = None
    newly_failing: List[str] = field(default_factory=list)
    still_failing: List[str] = field(default_factory=list)
    fixed: List[str] = field(default_factory=list)
    related_tests: List[str] = field(default_factory=list)
    related_test_reasons: Dict[str, str] = field(default_factory=dict)
    after_failed_tests: List[str] = field(default_factory=list)
    message: str = ""


def _baseline_failure_details(output: str) -> Dict[str, tuple[str, str]]:
    details = {}
    for line in output.splitlines():
        match = _FAILED_SUMMARY.fullmatch(line)
        if match:
            details[match.group("test")] = (
                match.group("error_class"),
                match.group("message"),
            )
    return details


def _normalise_message(message: str) -> str:
    normalised = message.lower()
    normalised = _UUID.sub("<value>", normalised)
    normalised = _HEX.sub("<value>", normalised)
    normalised = _DIGITS.sub("<value>", normalised)
    normalised = _QUOTES.sub("", normalised)
    return _WHITESPACE.sub(" ", normalised).strip()


def _messages_match(first: str, second: str) -> bool:
    first_normalised = _normalise_message(first)
    second_normalised = _normalise_message(second)
    if first_normalised == second_normalised:
        return True
    return (
        min(len(first_normalised), len(second_normalised)) >= 12
        and (
            first_normalised in second_normalised
            or second_normalised in first_normalised
        )
    )


def evaluate_verification(baseline: SandboxResult, after: SandboxResult,
                          module_stem: str, incident_error_class: str = "",
                          incident_error_message: str = "") -> Verification:
    baseline_failures = set(baseline.failed_tests)
    after_failures = set(after.failed_tests)
    newly_failing = sorted(after_failures - baseline_failures)
    fixed = sorted(baseline_failures - after_failures)
    failure_details = _baseline_failure_details(baseline.output)
    related_reasons: Dict[str, str] = {}
    for test in baseline_failures:
        if PurePosixPath(test.split("::", 1)[0]).name == f"test_{module_stem}.py":
            related_reasons[test] = "file name"
            continue
        failure_detail = failure_details.get(test)
        if (
            failure_detail is not None
            and incident_error_class
            and failure_detail[0] == incident_error_class
            and incident_error_message
            and failure_detail[1]
            and _messages_match(failure_detail[1], incident_error_message)
        ):
            related_reasons[test] = "same error class and message"
    related_tests = sorted(related_reasons)
    fixed_related = sorted(set(related_tests) & set(fixed))
    still_failing = sorted(set(related_tests) & after_failures)
    common = {
        "tests_passed": after.tests_passed,
        "tests_failed": after.tests_failed,
        "newly_failing": newly_failing,
        "still_failing": still_failing,
        "fixed": fixed,
        "related_tests": related_tests,
        "related_test_reasons": related_reasons,
        "after_failed_tests": sorted(after_failures),
    }
    if after.status == "dependencies_unavailable":
        return Verification(
            "dependencies_unavailable",
            message=after.message or "Sandbox dependencies unavailable.",
            **common,
        )
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
    if fixed_related:
        evidence = "; ".join(
            f"{test} ({related_reasons[test]})" for test in fixed_related
        )
        return Verification(
            "fix_confirmed",
            message="Related tests now pass: " + evidence,
            **common,
        )
    if still_failing:
        return Verification(
            "failed",
            message="The bug's tests still fail: " + ", ".join(still_failing),
            **common,
        )
    return Verification(
        "no_regressions",
        message="No tests broke, but no failing test shows this bug is fixed.",
        **common,
    )
