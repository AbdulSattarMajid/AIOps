import ast
from dataclasses import dataclass
from pathlib import Path

# Files whose contents must never be read or sent to an AI.
_DENIED_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", "secrets.json"}
_DENIED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")

MAX_SOURCE_CHARS = 6000  # keeps the prompt small


@dataclass
class CodeContext:
    file: str
    function: str
    start_line: int
    end_line: int
    crash_line: int
    source: str
    truncated: bool = False


def resolve_repo_path(frame_file: str, repo_root: str, path_prefix: str = "/app/") -> Path | None:
    """Turn a path from a stack trace into a real file inside repo_root.
    Stack traces come from outside, so they are untrusted: anything that points outside
    repo_root (like ../../etc/passwd) or at a secret file is refused."""
    path = frame_file.replace("\\", "/")
    if path_prefix and path.startswith(path_prefix):
        path = path[len(path_prefix):]
    path = path.lstrip("/")
    root = Path(repo_root).resolve()
    try:
        candidate = (root / path).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None
    name = candidate.name.lower()
    if name in _DENIED_NAMES or name.endswith(_DENIED_SUFFIXES):
        return None
    if candidate.suffix != ".py" or not candidate.is_file():
        return None
    return candidate


def _functions(tree: ast.AST) -> list[tuple[str, int, int]]:
    """Every function in the file as (qualified name, first line, last line)."""
    found: list[tuple[str, int, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                name = prefix + child.name
                found.append((name, start, child.end_lineno))
                visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def extract_function(frame_file: str, line: int, repo_root: str,
                     path_prefix: str = "/app/") -> CodeContext | None:
    """Return only the function that contains `line`, not the whole file."""
    path = resolve_repo_path(frame_file, repo_root, path_prefix)
    if path is None:
        return None
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    matches = [f for f in _functions(tree) if f[1] <= line <= f[2]]
    if not matches:
        return None
    name, start, end = max(matches, key=lambda f: f[1])  # innermost function

    text = "\n".join(source.splitlines()[start - 1:end])
    truncated = len(text) > MAX_SOURCE_CHARS
    if truncated:
        text = text[:MAX_SOURCE_CHARS]
    return CodeContext(file=path.name, function=name, start_line=start, end_line=end,
                       crash_line=line, source=text, truncated=truncated)


def with_line_numbers(ctx: CodeContext) -> str:
    """Printable version of the function, with real line numbers and an arrow at the crash."""
    out = []
    for i, text in enumerate(ctx.source.splitlines(), start=ctx.start_line):
        arrow = ">>" if i == ctx.crash_line else "  "
        out.append(f"{arrow} {i:4d} | {text}")
    return "\n".join(out)
