import re

# Order matters: specific patterns first, generic numbers last.
_PATTERNS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<UUID>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<ADDR>"),
    (re.compile(r"\d+"), "<N>"),
]


def normalize_trace(text: str) -> str:
    """Remove the parts of an error that change every time (times, ids, line numbers)
    so that two crashes of the same bug look identical."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return " ".join(text.split())  # collapse extra whitespace