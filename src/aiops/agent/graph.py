from pathlib import Path
from typing import Dict, List, Literal, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph

from aiops.agent.diff_validator import validate_diff
from aiops.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from aiops.context.extract import extract_function, resolve_repo_path
from aiops.context.stacktrace import parse_python_traceback


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
    status: Literal["running", "patch_ready", "no_patch", "escalated"]
    reason: str


def build_graph(llm, store):
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
            return {
                "status": "patch_ready",
                "patch": result.clean_diff,
                "reason": "",
            }
        error = result.error or "Diff validation failed."
        history = list(state.get("history", []))
        history.append({
            "attempt": state.get("attempt", 0),
            "ok": False,
            "error": error,
            "reply": reply,
        })
        updated = {"history": history, "last_error": error}
        if state.get("attempt", 0) >= state.get("max_retries", 3):
            updated.update({
                "status": "escalated",
                "reason": "No valid patch after {} attempts. Last problem: {}".format(
                    state.get("attempt", 0), error
                ),
            })
        return updated

    def after_analyze(state: IncidentState) -> str:
        return "end" if state.get("status") == "escalated" else "retrieve_runbook"

    def after_generate(state: IncidentState) -> str:
        return "end" if state.get("status") == "escalated" else "validate_patch"

    def after_validation(state: IncidentState) -> str:
        if state.get("status") in ("patch_ready", "no_patch", "escalated"):
            return "end"
        return "generate_patch"

    workflow = StateGraph(IncidentState)
    workflow.add_node("analyze_error", analyze_error)
    workflow.add_node("retrieve_runbook", retrieve_runbook)
    workflow.add_node("generate_patch", generate_patch)
    workflow.add_node("validate_patch", validate_patch)
    workflow.add_edge(START, "analyze_error")
    workflow.add_conditional_edges(
        "analyze_error", after_analyze,
        {"end": END, "retrieve_runbook": "retrieve_runbook"},
    )
    workflow.add_edge("retrieve_runbook", "generate_patch")
    workflow.add_conditional_edges(
        "generate_patch", after_generate,
        {"end": END, "validate_patch": "validate_patch"},
    )
    workflow.add_conditional_edges(
        "validate_patch", after_validation,
        {"end": END, "generate_patch": "generate_patch"},
    )
    return workflow.compile()


def run_agent(llm, store, trace: str, service_name: str,
              repo_root: str, incident_id: int = 0,
              path_prefix: str = "/app/", max_retries: int = 3) -> Dict:
    graph = build_graph(llm, store)
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
    }
