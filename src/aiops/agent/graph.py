from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Dict, List, Literal, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph

from aiops.agent.diff_validator import validate_diff
from aiops.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from aiops.context.extract import extract_function, resolve_repo_path
from aiops.context.stacktrace import parse_python_traceback
from aiops.sandbox.evaluate import Verification, evaluate_verification
from aiops.sandbox.runner import SandboxResult


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


def build_graph(llm, store, sandbox=None):
    def analyze_error(state: IncidentState) -> Dict:
        parsed = parse_python_traceback(state.get("trace", ""))
        frame = parsed.crash_frame
        if frame is None:
            return {
                "signature": parsed.signature,
                "status": "escalated",
                "reason": "No Python traceback frame was found in the incident.",
            }
        path_prefix = state.get("path_prefix", "/app/")
        path = resolve_repo_path(
            frame.file, state.get("repo_root", ""), path_prefix
        )
        if path is None:
            return {
                "signature": parsed.signature,
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
                "signature": parsed.signature,
                "status": "escalated",
                "reason": "Could not find a Python function containing the crash line.",
            }
        root = Path(state.get("repo_root", "")).resolve()
        try:
            rel_path = path.relative_to(root).as_posix()
        except ValueError:
            return {
                "signature": parsed.signature,
                "status": "escalated",
                "reason": "The traceback file is outside the configured repository.",
            }
        file_source = path.read_text(encoding="utf-8", errors="replace")
        source_lines = file_source.splitlines()
        crash_line_text = (
            source_lines[frame.line - 1]
            if 1 <= frame.line <= len(source_lines) else ""
        )
        return {
            "signature": parsed.signature,
            "rel_path": rel_path,
            "function_name": context.function,
            "function_range": (context.start_line, context.end_line),
            "function_source": context.source,
            "crash_line_text": crash_line_text,
            "file_source": file_source,
            "status": "running",
            "reason": "",
        }

    def retrieve_runbook(state: IncidentState) -> Dict:
        if store is None:
            return {"runbooks": []}
        runbooks = store.get_relevant_context(
            state.get("service_name", ""),
            state.get("signature", ""),
            top_k=2,
        )
        return {"runbooks": runbooks}

    def baseline_tests(state: IncidentState) -> Dict:
        try:
            baseline = sandbox.baseline()
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            return {
                "verification": Verification(
                    "unavailable", message=message
                ),
                "sandbox_ok": False,
            }
        if baseline.status in ("error", "timeout"):
            message = baseline.message or (
                "Baseline sandbox tests " + baseline.status + "."
            )
            return {
                "baseline_result": baseline,
                "verification": Verification(
                    "unavailable", message=message
                ),
                "sandbox_ok": False,
            }
        return {
            "baseline_result": baseline,
            "sandbox_ok": True,
        }

    def generate_patch(state: IncidentState) -> Dict:
        attempt = state.get("attempt", 0) + 1
        updated = {"attempt": attempt}
        try:
            reply = llm.complete(
                SYSTEM_PROMPT,
                build_user_prompt(state),
            )
        except Exception as error:
            updated.update({
                "status": "escalated",
                "reason": f"AI call failed: {type(error).__name__}: {error}",
            })
            return updated
        text = reply.text
        updated.update({
            "last_reply": text,
            "model": reply.model,
            "tokens_used": state.get("tokens_used", 0)
            + reply.input_tokens + reply.output_tokens,
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
        try:
            after = sandbox.verify(
                state.get("rel_path", ""),
                state.get("patch_source", ""),
            )
        except Exception as error:
            after = SandboxResult(
                status="error",
                exit_code=None,
                output="",
                duration_seconds=0.0,
                tests_passed=None,
                tests_failed=None,
                message=f"{type(error).__name__}: {error}",
            )
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
        if verification.status == "unavailable":
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
            "last_test_output": after.output[-3000:],
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
              sandbox=None) -> Dict:
    graph = build_graph(llm, store, sandbox=sandbox)
    initial: IncidentState = {
        "incident_id": incident_id,
        "service_name": service_name,
        "trace": trace,
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
    return {
        "incident_id": state.get("incident_id"),
        "status": state.get("status"),
        "reason": state.get("reason", ""),
        "attempts": state.get("attempt", 0),
        "function": state.get("function_name", ""),
        "file": state.get("rel_path", ""),
        "patch": state.get("patch", ""),
        "runbooks": [item.get("title", "") for item in state.get("runbooks", [])],
        "tokens_used": state.get("tokens_used", 0),
        "model": state.get("model", ""),
        "history": [
            {key: value for key, value in entry.items() if key != "reply"}
            for entry in state.get("history", [])
        ],
        "verification": verification,
    }
