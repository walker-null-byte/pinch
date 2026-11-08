"""Technique `testcompress`: cut test and command output down to what the model needs.

Keeps failing test names, the first error of each, and the summary line, under a hard cap. The full output is
saved to a file inside the repo (.pinch/outputs/, excluded from git) that the model can read if it really needs it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

LIMIT = 1200  # output at or under this many characters is returned unchanged
HARD_CAP = 1500
SHELL_TOOLS = {"run_test", "run_command", "run_python", "run_python_file"}


def parse_run(result: str) -> tuple[int, str] | None:
    """(returncode, stdout+stderr) from a coding-tools command result, or None if it is not one."""
    try:
        data = json.loads(result)
        return int(data["returncode"]), (data.get("stdout") or "") + (("\n" + data["stderr"]) if data.get("stderr") else "")
    except (ValueError, KeyError, TypeError):
        return None


def summarize(returncode: int, text: str) -> str:
    lines = text.splitlines()
    out = []
    failed = [m.group(0) for line in lines if (m := re.match(r"^(FAILED|ERROR) \S+.*$", line))]
    unit = [line for line in lines if re.match(r"^(FAIL|ERROR): ", line)]
    errors = _first_errors(lines)
    for line in (failed or unit)[:8]:
        out.append(line[:200])
        name = re.sub(r"^(FAILED|ERROR|FAIL:) ", "", line).split(" - ")[0].split("::")[-1].split(" ")[0]
        if name in errors:
            out.append(f"  first error: {errors[name][:200]}")
    if not (failed or unit):
        # no recognisable failures: the start (usually the cause) and the end (usually the summary)
        out.extend(lines[:6])
        if len(lines) > 18:
            out.append("[...]")
        out.extend(lines[-12:] if len(lines) > 6 else [])
    summary = _summary_line(lines)
    if summary and summary not in out:
        out.append(summary)
    return "\n".join(out)


def _first_errors(lines: list[str]) -> dict[str, str]:
    """pytest: under '____ test_name ____' the first 'E   message' line."""
    errors, current = {}, None
    for line in lines:
        header = re.match(r"^_{3,} (.+?) _{3,}$", line)
        if header:
            current = header.group(1).split(".")[-1].split(" ")[0]
        elif current and current not in errors and line.startswith("E  "):
            errors[current] = line[1:].strip()
    return errors


def _summary_line(lines: list[str]) -> str:
    for line in reversed(lines):
        if re.search(r"\b(passed|failed|error|errors)\b.* in [\d.]+s", line) or re.match(r"^(Ran \d+ tests?|OK|FAILED \()", line):
            return line.strip()
    return ""


def compress_run(result: str, save_dir: Path, name: str, repo: Path) -> str:
    """Return the shorter text for a command result, or the result unchanged if it is already short."""
    parsed = parse_run(result)
    if parsed is None:
        return result
    returncode, text = parsed
    if len(text) <= LIMIT:
        return result
    save_dir.mkdir(parents=True, exist_ok=True)
    _exclude_from_git(repo)
    path = save_dir / f"{name}.txt"
    path.write_text(text, encoding="utf-8")
    rel = path.relative_to(repo).as_posix()
    head = f"returncode {returncode}. Output cut from {len(text)} to about {HARD_CAP} characters; the full output is in {rel} (read it only if this is not enough)."
    body = summarize(returncode, text)
    out = head + "\n" + body
    return out if len(out) <= HARD_CAP + len(head) else out[: HARD_CAP + len(head)] + "\n[cut]"


def _exclude_from_git(repo: Path) -> None:
    """Keep .pinch/ out of `git status` without touching any tracked file."""
    info = repo / ".git" / "info"
    if not info.is_dir():
        return
    exclude = info / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".pinch/" not in current:
            with exclude.open("a", encoding="utf-8") as f:
                f.write("\n.pinch/\n")
    except OSError:
        pass
