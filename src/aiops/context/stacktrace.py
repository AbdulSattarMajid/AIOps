import re
from dataclasses import dataclass, field

_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<function>\S+)')
_EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception))(?::\s*(.*))?$")


@dataclass
class Frame:
    file: str
    line: int
    function: str


@dataclass
class ParsedTrace:
    exception_type: str | None
    message: str
    frames: list[Frame] = field(default_factory=list)

    @property
    def crash_frame(self) -> Frame | None:
        """The frame where the error was raised (the last one in a Python traceback)."""
        return self.frames[-1] if self.frames else None

    @property
    def signature(self) -> str:
        """Short text used to search for a matching runbook."""
        return f"{self.exception_type or ''}: {self.message}".strip(": ")


def parse_python_traceback(trace: str) -> ParsedTrace:
    frames = [
        Frame(m["file"], int(m["line"]), m["function"])
        for m in _FRAME.finditer(trace)
    ]
    exception_type, message = None, ""
    for raw in reversed(trace.splitlines()):
        match = _EXCEPTION.match(raw.strip())
        if match:
            exception_type, message = match.group(1), (match.group(2) or "")
            break
    return ParsedTrace(exception_type=exception_type, message=message, frames=frames)
