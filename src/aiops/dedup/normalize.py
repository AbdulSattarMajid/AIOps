import re

_CHANGING_PARTS = re.compile(
    r"(?P<status>(?:\bhttp[A-Za-z]*\b\s*[:=]?\s*"
    r"|\bhttp/\d+(?:\.\d+)?\s+"
    r"|\bstatus(?:_code)?\b\s*[:=]?\s*"
    r"|\bcode\b\s*[:=]?\s*)(?P<code>[1-5]\d{2})\b)"
    r"|(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
    r"|(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})"
    r"|(?P<address>0x[0-9a-f]+)"
    r"|(?P<number>\d+)",
    re.IGNORECASE,
)


def normalize_trace(text: str) -> str:
    """Remove the parts of an error that change every time (times, ids, line numbers)
    so that two crashes of the same bug look identical."""
    def replace(match: re.Match) -> str:
        if match.group("status"):
            prefix = match.group("status")[:-len(match.group("code"))]
            return re.sub(r"\d+", "<N>", prefix) + match.group("code")
        if match.group("timestamp"):
            return "<TS>"
        if match.group("uuid"):
            return "<UUID>"
        if match.group("address"):
            return "<ADDR>"
        return "<N>"

    text = _CHANGING_PARTS.sub(replace, text)
    return " ".join(text.split())  # collapse extra whitespace