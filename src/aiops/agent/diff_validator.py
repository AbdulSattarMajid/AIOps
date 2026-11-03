import ast
import difflib
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


class DiffError(ValueError):
    pass


@dataclass
class ValidationResult:
    ok: bool
    error: Optional[str]
    new_source: Optional[str]
    clean_diff: Optional[str]
    added: int
    removed: int


@dataclass
class _Hunk:
    start: int
    lines: List[Tuple[str, str]]


@dataclass
class _FileDiff:
    old_path: str
    new_path: str
    hunks: List[_Hunk]


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
_DIFF_INSTRUCTIONS = (
    "After the '+++ b/<file>' line, each section must start with a line like "
    "'@@ -17,3 +17,5 @@' and every following line must start with a space "
    "(unchanged), '+' (added) or '-' (removed)."
)


def _invalid_diff_line(line_number: int, line: str) -> DiffError:
    preview = line[:80]
    if len(line) > 80:
        preview += "..."
    return DiffError(
        f"Line {line_number} of your diff is not valid: {preview!r}. "
        f"{_DIFF_INSTRUCTIONS}"
    )


def _extract_diff_text(reply: str) -> str:
    without_fences = re.sub(r"```[^\n]*\n?", "", reply)
    lines = without_fences.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("--- "):
            return "\n".join(lines[index:])
    raise DiffError("No unified diff found in the AI reply.")


def _header_path(line: str, marker: str) -> str:
    path = line[len(marker):].split("\t", 1)[0].strip()
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def _parse_diff(diff_text: str) -> Tuple[List[_FileDiff], int, int]:
    lines = diff_text.splitlines()
    files = []
    added = 0
    removed = 0
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            raise DiffError(
                f"Line {index + 1} of your diff must start with '--- a/<file>'."
            )
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise DiffError(
                f"Line {index + 2} of your diff must start with '+++ b/<file>' "
                "after the '--- a/<file>' header."
            )
        file_diff = _FileDiff(
            _header_path(lines[index], "--- "),
            _header_path(lines[index + 1], "+++ "),
            [],
        )
        files.append(file_diff)
        index += 2
        body_start = index
        while index < len(lines) and not (
            lines[index].startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            index += 1
        body_end = index
        has_hunk_markers = any(
            line.startswith("@@") for line in lines[body_start:body_end]
        )
        index = body_start
        if not has_hunk_markers and body_start < body_end:
            hunk = _Hunk(0, [])
            while index < body_end:
                line = lines[index]
                if line.startswith("\\"):
                    index += 1
                    continue
                if line == "":
                    hunk.lines.append((" ", ""))
                elif line[0] in " +-":
                    hunk.lines.append((line[0], line[1:]))
                    if line[0] == "+":
                        added += 1
                    elif line[0] == "-":
                        removed += 1
                else:
                    raise _invalid_diff_line(index + 1, line)
                index += 1
            file_diff.hunks.append(hunk)
            continue
        while index < len(lines):
            if (
                lines[index].startswith("--- ")
                and index + 1 < len(lines)
                and lines[index + 1].startswith("+++ ")
            ):
                break
            if not lines[index].startswith("@@"):
                raise _invalid_diff_line(index + 1, lines[index])
            header = _HUNK_HEADER.match(lines[index])
            hunk = _Hunk(int(header.group(1)) if header else 0, [])
            index += 1
            while index < len(lines):
                if lines[index].startswith("@@"):
                    break
                if (
                    lines[index].startswith("--- ")
                    and index + 1 < len(lines)
                    and lines[index + 1].startswith("+++ ")
                ):
                    break
                line = lines[index]
                if line.startswith("\\"):
                    index += 1
                    continue
                if line == "":
                    hunk.lines.append((" ", ""))
                elif line[0] in " +-":
                    hunk.lines.append((line[0], line[1:]))
                    if line[0] == "+":
                        added += 1
                    elif line[0] == "-":
                        removed += 1
                else:
                    raise _invalid_diff_line(index + 1, line)
                index += 1
            file_diff.hunks.append(hunk)
        if not file_diff.hunks:
            raise DiffError("The unified diff contains a file without any hunks.")
    return files, added, removed


def _same_lines(source_lines: List[str], position: int,
                old_lines: List[str], ignore_trailing: bool) -> bool:
    actual = source_lines[position:position + len(old_lines)]
    if len(actual) != len(old_lines):
        return False
    if ignore_trailing:
        return all(a.rstrip() == b.rstrip() for a, b in zip(actual, old_lines))
    return actual == old_lines


def _find_hunk(lines: List[str], old_block: List[str], start_at: int,
               claimed_start: int) -> int:
    candidates = [
        position for position in range(start_at, len(lines) - len(old_block) + 1)
        if _same_lines(lines, position, old_block, False)
    ]
    if not candidates:
        candidates = [
            position for position in range(start_at, len(lines) - len(old_block) + 1)
            if _same_lines(lines, position, old_block, True)
        ]
    if not candidates:
        raise DiffError(
            f"No matching source context for hunk; expected {old_block[0]!r}."
        )
    return min(candidates, key=lambda position: abs(position + 1 - claimed_start))


def _apply_hunks(source: str, hunks: List[_Hunk],
                 func_range: Optional[Tuple[int, int]]) -> str:
    trailing_newline = source.endswith("\n")
    source_lines = source.splitlines()
    search_from = 0
    for hunk in hunks:
        old_block = [text for kind, text in hunk.lines if kind in (" ", "-")]
        if not old_block:
            raise DiffError(
                "A hunk must include unchanged or removed context; "
                "insertion-only hunks are not allowed."
            )
        position = _find_hunk(source_lines, old_block, search_from, hunk.start)
        output = []
        cursor = position
        for kind, text in hunk.lines:
            if kind == "+":
                if func_range is not None:
                    insertion_line = cursor + 1
                    if not (func_range[0] <= insertion_line <= func_range[1] + 1):
                        raise DiffError("changes code outside the function shown")
                output.append(text)
            elif kind == "-":
                line_number = cursor + 1
                if func_range is not None and not (
                    func_range[0] <= line_number <= func_range[1]
                ):
                    raise DiffError("changes code outside the function shown")
                cursor += 1
            else:
                output.append(source_lines[cursor])
                cursor += 1
        source_lines[position:cursor] = output
        search_from = position + len(output)
    result = "\n".join(source_lines)
    if trailing_newline:
        result += "\n"
    return result


def validate_diff(reply: str, rel_path: str, source: str,
                  func_range: Optional[Tuple[int, int]] = None
                  ) -> ValidationResult:
    try:
        diff_text = _extract_diff_text(reply)
        if len(diff_text) > 8000:
            raise DiffError("Diff is too large; maximum size is 8000 characters.")
        files, added, removed = _parse_diff(diff_text)
        if added + removed < 1:
            raise DiffError("Diff contains no changed lines.")
        if added + removed > 60:
            raise DiffError("Diff changes more than 60 lines.")
        if len(files) != 1:
            raise DiffError("Diff must change exactly one file.")
        file_diff = files[0]
        if file_diff.old_path == "/dev/null" or file_diff.new_path == "/dev/null":
            raise DiffError("Creating or deleting files is not allowed.")
        if file_diff.old_path != rel_path or file_diff.new_path != rel_path:
            raise DiffError(
                f"Diff file paths must both be {rel_path!r}; "
                f"got {file_diff.old_path!r} and {file_diff.new_path!r}."
            )
        new_source = _apply_hunks(source, file_diff.hunks, func_range)
        if new_source == source:
            raise DiffError("Diff does not change the source.")
        if rel_path.endswith(".py"):
            try:
                ast.parse(new_source)
            except SyntaxError as error:
                raise DiffError(
                    f"Resulting Python has a syntax error on line "
                    f"{error.lineno}: {error.msg}."
                )
        original_lines = source.splitlines()
        new_lines = new_source.splitlines()
        clean_diff = "\n".join(difflib.unified_diff(
            original_lines, new_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
        )) + "\n"
        return ValidationResult(True, None, new_source, clean_diff, added, removed)
    except DiffError as error:
        return ValidationResult(False, str(error), None, None, 0, 0)
