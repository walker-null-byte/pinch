"""Technique `cascade`: start on the small model tier; move to the big tier on clear failure signals.

Signals (both configurable): the tests still fail after N edit-and-test rounds, or M tool errors in a row.
Escalation is one routing decision, made once; there is no going back down in the same session.
"""

from __future__ import annotations

import json
import re

TEST_WORDS = re.compile(r"\b(pytest|unittest|npm test|yarn test|cargo test|go test|make test|tox)\b")


def is_test_call(tool: str, args: dict) -> bool:
    return tool == "run_test" or (tool in ("run_command", "run_python_file") and bool(TEST_WORDS.search(str(args.get("command") or args.get("file_path") or ""))))


def is_tool_error(result: str) -> bool:
    if result.startswith("Error"):
        return True
    try:
        data = json.loads(result)
    except ValueError:
        return False
    return isinstance(data, dict) and "error" in data and "returncode" not in data


class Cascade:
    def __init__(self, small: str, big: str, failed_test_rounds: int = 2, tool_errors: int = 3):
        self.small, self.big = small, big
        self.limit_rounds, self.limit_errors = failed_test_rounds, tool_errors
        self.escalated = False
        self.failed_rounds = 0
        self.error_streak = 0
        self.edited_since_test = False

    @property
    def active(self) -> bool:
        return bool(self.small and self.big)

    def note_edit(self) -> None:
        self.edited_since_test = True

    def note_test(self, returncode: int | None) -> None:
        if returncode is None:
            return
        if returncode != 0 and self.edited_since_test:
            self.failed_rounds += 1
        if returncode == 0:
            self.failed_rounds = 0
        self.edited_since_test = False

    def note_result(self, result: str) -> None:
        self.error_streak = self.error_streak + 1 if is_tool_error(result) else 0

    def reason(self) -> str | None:
        """Why to escalate now, or None. Never fires twice."""
        if not self.active or self.escalated:
            return None
        if self.failed_rounds >= self.limit_rounds:
            return f"tests still failing after {self.failed_rounds} edit-and-test rounds"
        if self.error_streak >= self.limit_errors:
            return f"{self.error_streak} tool errors in a row"
        return None
