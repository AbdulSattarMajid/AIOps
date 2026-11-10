from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Dict, List, Literal, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph

from aiops.agent.diff_validator import validate_diff
from aiops.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from aiops.context.extract import (
    extract_function,
    resolve_repo_path,
    select_application_frame,
)
from aiops.context.stacktrace import parse_python_traceback
from aiops.limits import LimitStoreError, RedisLimits
from aiops.sandbox.evaluate import Verification, evaluate_verification
from aiops.sandbox.runner import SandboxResult
from aiops.security.redaction import redact_text, source_secret_findings


class IncidentState(TypedDict, total=False):
    incident_id: int
    service_name: str
    trace: str
    repo_root: str
    path_prefix: str
    max_retries: int
    signature: str
    rel_path: str
    function_name: str
    function_range: Tuple[int, int]
    function_source: str
    crash_line_text: str
    file_source: str
    runbooks: List[Dict]
    attempt: int
    tokens_used: int
    last_reply: str
    model: str
    last_error: str
    history: List[Dict]
    patch: str
    status: Literal[
        "running", "patch_ready", "verified", "no_patch", "escalated"
    ]
    reason: str
    baseline_result: object
    verification: object
    sandbox_ok: bool
    patch_source: str
    last_test_output: str
    test_source: str
    baseline_failed_tests: List[str]
    baseline_test_output: str


def _related_test_source(state: IncidentState) -> str:
    rel_path = Path(state.get("rel_path", ""))
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return ""
    try:
        root = Path(state.get("repo_root", "")).resolve()
        test_path = (
            root / rel_path.parent / f"test_{rel_path.stem}.py"
        ).resolve()
        test_path.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return ""
    try:
        return test_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def build_graph(llm, store, sandbox=None, limits: RedisLimits | None = None):
    def analyze_error(state: IncidentState) -> Dict:
        trace, _ = redact_text(state.get("trace", ""))
        parsed = parse_python_traceback(trace)
        path_prefix = state.get("path_prefix", "/app/")
        repo_root = state.get("repo_root", "")
        frame = select_application_frame(
            parsed.frames, repo_root, path_prefix
        )
        if frame is None:
            return {
                "signature": state.get("signature") or parsed.signature,
                "status": "escalated",
                "reason": (
                    "No Python traceback frame was found in the incident."
                    if not parsed.frames
                    else "no application frame found in the traceback"
                ),
            }
        path = resolve_repo_path(
            frame.file, repo_root, path_prefix
        )
        if path is None:
            return {
                "signature": state.get("signature") or parsed.signature,
                "status": "escalated",
                "reason": (
                    "The traceback file is missing, outside the repository, "
                    "or not allowed."
                ),
            }
        context = extract_function(
            frame.file, frame.line, state.get("repo_root", ""), path_prefix
        )
        if context is None:
            return {
                "signature": state.get("signature") or parsed.signature,
                "status": "escalated",
                "reason": "Could not find a Python function containing the crash line.",
            }
        root = Path(state.get("repo_root", "")).resolve()
        try:
            rel_path = path.relative_to(root).as_posix()
        except ValueError:
            return {
                "signature": state.get("signature") or parsed.signature,
                "status": "escalated",
                "reason": "The traceback file is outside the configured repository.",
            }
        file_source = path.read_text(encoding="utf-8", errors="replace")
        source_lines = file_source.splitlines()
        crash_line_text = (
            source_lines[frame.line - 1]
            if 1 <= frame.line <= len(source_lines) else ""
        )
        if source_secret_findings(context.source):
            return {
                "signature": state.get("signature") or parsed.signature,
                "rel_path": rel_path,
                "function_name": context.function,
                "function_range": (context.start_line, context.end_line),
                "status": "escalated",
                "reason": "secret found in source code",
                "trace": trace,
            }
        return {
            "signature": state.get("signature") or parsed.signature,
            "rel_path": rel_path,
            "function_name": context.function,
            "function_range": (context.start_line, context.end_line),
            "function_source": context.source,
            "crash_line_text": crash_line_text,
            "file_source": file_source,
            "status": "running",
            "reason": "",
            "trace": trace,
        }

    def retrieve_runbook(state: IncidentState) -> Dict:
        if store is None:
            return {"runbooks": []}
        runbooks = store.get_relevant_context(
            state.get("service_name", ""),
            state.get("signature", ""),
            top_k=2,
        )
        cleaned = []
        for runbook in runbooks:
            item = dict(runbook)
            item["text"], _ = redact_text(item.get("text", ""))
            cleaned.append(item)
        return {"runbooks": cleaned}

    def baseline_tests(state: IncidentState) -> Dict:
        slot = None
        if limits is not None:
            try:
                slot = limits.acquire_slot("sandboxes")
            except LimitStoreError:
                slot = None
            if slot is None:
                return {
                    "verification": Verification(
                        "unavailable",
                        message="Sandbox concurrency capacity is unavailable.",
                    ),
                    "sandbox_ok": False,
                }
        try:
            baseline = sandbox.baseline()
        except Exception:
            return {
                "verification": Verification(
                    "unavailable", message="Sandbox execution failed."
                ),
                "sandbox_ok": False,
            }
        finally:
            if slot is not None:
                try:
                    limits.release_slot("sandboxes", slot)
                except LimitStoreError:
                    pass
        if baseline.status in ("error", "timeout", "dependencies_unavailable"):
            message = baseline.message or (
                "Baseline sandbox tests " + baseline.status + "."
            )
            v_status = (
                "dependencies_unavailable"
                if baseline.status == "dependencies_unavailable"
                else "unavailable"
            )
            return {
                "baseline_result": baseline,
                "verification": Verification(
                    v_status, message=message
                ),
                "sandbox_ok": False,
            }
        test_source = _related_test_source(state)
        return {
            "baseline_result": baseline,
            "sandbox_ok": True,
            **({
                "test_source": test_source,
                "baseline_failed_tests": list(baseline.failed_tests),
                "baseline_test_output": baseline.output,
            } if test_source else {}),
        }

    def generate_patch(state: IncidentState) -> Dict:
        attempt = state.get("attempt", 0) + 1
        updated = {"attempt": attempt}
        incident_id = state.get("incident_id", 0)
        if limits is not None:
            try:
                budget_reason = limits.check_token_budget(incident_id)
            except LimitStoreError:
                return {
                    "attempt": attempt,
                    "status": "escalated",
                    "reason": "Token budget could not be checked; analysis stopped.",
                }
            if budget_reason:
                return {
                    "attempt": attempt,
                    "status": "escalated",
                    "reason": budget_reason,
                }
        try:
            reply = llm.complete(
                SYSTEM_PROMPT,
                build_user_prompt(state),
            )
        except Exception:
            updated.update({
                "status": "escalated",
                "reason": "AI call failed; analysis stopped.",
            })
            return updated
        text = reply.text
        used_tokens = reply.input_tokens + reply.output_tokens
        if limits is not None:
            try:
                limits.record_tokens(incident_id, used_tokens)
            except LimitStoreError:
                return {
                    "attempt": attempt,
                    "status": "escalated",
                    "reason": (
                        "Token usage could not be recorded; analysis stopped."
                    ),
                    "tokens_used": state.get("tokens_used", 0) + used_tokens,
                }
        updated.update({
            "last_reply": text,
            "model": reply.model,
            "tokens_used": state.get("tokens_used", 0)
            + used_tokens,
        })
        return updated

    def validate_patch(state: IncidentState) -> Dict:
        reply = state.get("last_reply", "")
        if reply.lstrip().lower().startswith("no_patch"):
            reason = reply.split(":", 1)[1].strip() if ":" in reply else reply.strip()
            return {"status": "no_patch", "reason": reason}
        result = validate_diff(
            reply,
            state.get("rel_path", ""),
            state.get("file_source", ""),
            state.get("function_range"),
        )
        if result.ok:
            reason = ""
            if sandbox is not None and not state.get("sandbox_ok", False):
                verification = state.get("verification")
                detail = verification.message if verification else "baseline unavailable"
                reason = f"Patch could not be tested: {detail}"
            return {
                "status": "patch_ready",
                "patch": result.clean_diff,
                "patch_source": result.new_source,
                "reason": reason,
            }
        error = result.error or "Diff validation failed."
        history = list(state.get("history", []))
        history.append({
            "attempt": state.get("attempt", 0),
            "stage": "diff",
            "ok": False,
            "error": error,
            "reply": reply,
        })
        updated = {
            "history": history,
            "last_error": error,
            "last_test_output": "",
        }
        if state.get("attempt", 0) >= state.get("max_retries", 3):
            updated.update({
                "status": "escalated",
                "reason": "No valid patch after {} attempts. Last problem: {}".format(
                    state.get("attempt", 0), error
                ),
            })
        return updated

    def verify_in_sandbox(state: IncidentState) -> Dict:
        slot = None
        if limits is not None:
            try:
                slot = limits.acquire_slot("sandboxes")
            except LimitStoreError:
                slot = None
            if slot is None:
                after = SandboxResult(
                    status="error",
                    exit_code=None,
                    output="",
                    duration_seconds=0.0,
                    tests_passed=None,
                    tests_failed=None,
                    message="Sandbox concurrency capacity is unavailable.",
                )
            else:
                after = None
        else:
            after = None
        try:
            if after is None:
                after = sandbox.verify(
                    state.get("rel_path", ""),
                    state.get("patch_source", ""),
                )
        except Exception:
            after = SandboxResult(
                status="error",
                exit_code=None,
                output="",
                duration_seconds=0.0,
                tests_passed=None,
                tests_failed=None,
                message="Sandbox execution failed.",
            )
        finally:
            if slot is not None:
                try:
                    limits.release_slot("sandboxes", slot)
                except LimitStoreError:
                    pass
        verification = evaluate_verification(
            state["baseline_result"],
            after,
            Path(state.get("rel_path", "")).stem,
        )
        if verification.status == "fix_confirmed":
            return {
                "status": "verified",
                "verification": verification,
                "reason": verification.message,
            }
        if verification.status == "no_regressions":
            return {
                "status": "patch_ready",
                "verification": verification,
                "reason": verification.message,
            }
        if verification.status in ("unavailable", "dependencies_unavailable"):
            return {
                "status": "patch_ready",
                "verification": verification,
                "reason": f"Patch could not be tested: {verification.message}",
            }

        problems = []
        if verification.newly_failing:
            problems.append(
                "Newly failing tests: "
                + ", ".join(verification.newly_failing)
            )
        if verification.still_failing:
            problems.append(
                "Previously failing target tests still fail: "
                + ", ".join(verification.still_failing)
            )
        if not problems:
            problems.append(verification.message)
        error = " ".join(problems)
        history = list(state.get("history", []))
        history.append({
            "attempt": state.get("attempt", 0),
            "stage": "tests",
            "ok": False,
            "error": error,
        })
        updated = {
            "verification": verification,
            "history": history,
            "last_error": error,
            "last_test_output": redact_text(after.output[-3000:])[0],
        }
        if state.get("attempt", 0) >= state.get("max_retries", 3):
            updated.update({
                "status": "escalated",
                "reason": (
                    "Tests still failing after {} attempts. Last problem: {}"
                    .format(state.get("attempt", 0), error)
                ),
            })
        else:
            updated["status"] = "running"
        return updated

    def after_analyze(state: IncidentState) -> str:
        if state.get("status") == "escalated":
            return "end"
        return "baseline_tests" if sandbox is not None else "retrieve_runbook"

    def after_baseline(state: IncidentState) -> str:
        return "retrieve_runbook"

    def after_generate(state: IncidentState) -> str:
        return "end" if state.get("status") == "escalated" else "validate_patch"

    def after_validation(state: IncidentState) -> str:
        if state.get("status") in ("no_patch", "escalated"):
            return "end"
        if state.get("status") == "patch_ready":
            if sandbox is None or not state.get("sandbox_ok", False):
                return "end"
            return "verify_in_sandbox"
        return "generate_patch"

    def after_verification(state: IncidentState) -> str:
        if state.get("status") == "running":
            return "generate_patch"
        return "end"

    workflow = StateGraph(IncidentState)
    workflow.add_node("analyze_error", analyze_error)
    if sandbox is not None:
        workflow.add_node("baseline_tests", baseline_tests)
    workflow.add_node("retrieve_runbook", retrieve_runbook)
    workflow.add_node("generate_patch", generate_patch)
    workflow.add_node("validate_patch", validate_patch)
    if sandbox is not None:
        workflow.add_node("verify_in_sandbox", verify_in_sandbox)
    workflow.add_edge(START, "analyze_error")
    workflow.add_conditional_edges(
        "analyze_error", after_analyze,
        {
            "end": END,
            "baseline_tests": "baseline_tests",
            "retrieve_runbook": "retrieve_runbook",
        } if sandbox is not None else {
            "end": END, "retrieve_runbook": "retrieve_runbook",
        },
    )
    if sandbox is not None:
        workflow.add_conditional_edges(
            "baseline_tests", after_baseline,
            {"retrieve_runbook": "retrieve_runbook"},
        )
    workflow.add_edge("retrieve_runbook", "generate_patch")
    workflow.add_conditional_edges(
        "generate_patch", after_generate,
        {"end": END, "validate_patch": "validate_patch"},
    )
    workflow.add_conditional_edges(
        "validate_patch", after_validation,
        {
            "end": END,
            "generate_patch": "generate_patch",
            "verify_in_sandbox": "verify_in_sandbox",
        } if sandbox is not None else {
            "end": END, "generate_patch": "generate_patch",
        },
    )
    if sandbox is not None:
        workflow.add_conditional_edges(
            "verify_in_sandbox", after_verification,
            {"end": END, "generate_patch": "generate_patch"},
        )
    return workflow.compile()


def run_agent(llm, store, trace: str, service_name: str,
              repo_root: str, incident_id: int = 0,
              path_prefix: str = "/app/", max_retries: int = 3,
              sandbox=None, limits: RedisLimits | None = None,
              signature: str = "") -> Dict:
    graph = build_graph(llm, store, sandbox=sandbox, limits=limits)
    clean_trace, _ = redact_text(trace or "")
    initial: IncidentState = {
        "incident_id": incident_id,
        "signature": signature,
        "service_name": service_name,
        "trace": clean_trace,
        "repo_root": repo_root,
        "path_prefix": path_prefix,
        "max_retries": max_retries,
        "attempt": 0,
        "tokens_used": 0,
        "history": [],
        "status": "running",
    }
    return graph.invoke(initial)


def summarize(state: Dict) -> Dict:
    verification = state.get("verification")
    if is_dataclass(verification):
        verification = asdict(verification)
    baseline_result = state.get("baseline_result")
    baseline_failed_tests = (
        list(getattr(baseline_result, "failed_tests", []))
        if baseline_result is not None else []
    )
    return {
        "incident_id": state.get("incident_id"),
        "status": state.get("status"),
        "reason": state.get("reason", ""),
        "attempts": state.get("attempt", 0),
        "function": state.get("function_name", ""),
        "file": state.get("rel_path", ""),
        "patch": state.get("patch", ""),
        "file_source": state.get("file_source", ""),
        "final_source": state.get("patch_source", ""),
        "function_range": state.get("function_range"),
        "signature": state.get("signature", ""),
        "runbooks": [item.get("title", "") for item in state.get("runbooks", [])],
        "tokens_used": state.get("tokens_used", 0),
        "model": state.get("model", ""),
        "baseline_tests_passed": (
            getattr(baseline_result, "tests_passed", None)
            if baseline_result is not None else None
        ),
        "baseline_tests_failed": (
            getattr(baseline_result, "tests_failed", None)
            if baseline_result is not None else None
        ),
        "baseline_failed_tests": baseline_failed_tests,
        "after_failed_tests": (
            list(verification.get("after_failed_tests", []))
            if isinstance(verification, dict) else []
        ),
        "history": [
            {key: value for key, value in entry.items() if key != "reply"}
            for entry in state.get("history", [])
        ],
        "verification": verification,
    }
