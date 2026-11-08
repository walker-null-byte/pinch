"""The approval gate.

Every tool is tagged read, write, shell or git in TOOL_CLASS below. Reads run freely.
Everything else needs a yes from the user. A tool that is not in the table is "unknown"
and needs approval too.

The gate only ever hears from two sources: the --auto-approve flag (set once at startup)
and the `ask` callback, which reads the user's own keyboard. Model output, tool results and
file contents never reach it, so nothing the model reads or writes can change a decision.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

READ, WRITE, SHELL, GIT, UNKNOWN = "read", "write", "shell", "git", "unknown"

# Every coding-tools tool, by name. Edit here to change what needs approval.
TOOL_CLASS: dict[str, str] = {
    # read
    "read_file": READ, "read_multiple_files": READ, "list_directory": READ,
    "search_files": READ, "grep_search": READ, "analyze_code": READ, "get_ast": READ,
    "read_image_file": READ, "get_file_info": READ,
    "get_workspace_info": READ, "get_working_dir": READ, "get_current_time": READ,
    # write
    "write_file": WRITE, "edit_file": WRITE, "apply_patch": WRITE, "delete_file": WRITE,
    "delete_directory": WRITE, "move_file": WRITE, "copy_file": WRITE,
    "create_directory": WRITE, "search_and_replace": WRITE, "format_code": WRITE,
    # shell (running tests counts as shell)
    "run_command": SHELL, "run_python": SHELL, "run_python_file": SHELL, "run_test": SHELL,
    "process_manage": SHELL, "install_package": SHELL, "restart_server": SHELL,
    # lint_code runs ruff or pylint as a subprocess, and pylint can load plugin code named in the
    # repo's own config (load-plugins), so it is shell, not read.
    "lint_code": SHELL,
    # git (even read-only git needs approval unless the user allows it in config)
    "git_status": GIT, "git_log": GIT, "git_diff": GIT, "git_commit": GIT, "git_add": GIT,
    # TurboMCP lazy meta-tools that only look things up
    "search_tools": READ, "get_tool": READ,
    # Tavily web lookup: changes nothing locally. Not offered for confidential repos.
    "docs_lookup": READ,
}
# `call_tool` is deliberately not in the table: it is unwrapped and its inner tool is checked.
META_CALL = "call_tool"


def classify(tool: str) -> str:
    return TOOL_CLASS.get(tool, UNKNOWN)


@dataclass
class Decision:
    allowed: bool
    kind: str
    how: str  # "read", "user", "always", "auto-approve", "allowed-in-config", "denied"
    note: str = ""


@dataclass
class Gate:
    repo: Path
    ask: Callable[[str], str]  # shows the prompt, returns "y" | "n" | "a". Reads the user's keyboard only.
    auto_approve: bool = False
    always: set[str] = field(default_factory=set)  # scopes the user said "always" to (see always_scope)
    allowed_in_config: frozenset[str] = frozenset()
    on_event: Callable[..., None] = lambda event, **data: None

    def check(self, tool: str, args: dict, via_call_tool: bool = False) -> Decision:
        """Decide whether `tool` may run with `args`. `tool` is the real tool name, not call_tool."""
        kind = classify(tool)
        if kind == READ:
            return Decision(True, kind, "read")
        if tool in self.allowed_in_config:
            return Decision(True, kind, "allowed-in-config")
        if always_scope(tool, kind, args) in self.always:
            return Decision(True, kind, "always")
        if self.auto_approve:
            self.on_event("auto_approved", tool=tool, kind=kind, args=_short(args))
            return Decision(True, kind, "auto-approve")

        prompt = describe(tool, kind, args, self.repo, via_call_tool)
        prompt += f"\n  Allow?  [y] yes   [n] no   [a] {always_label(tool, kind, args)}"
        answer = (self.ask(prompt) or "n").strip().lower()
        if answer in ("a", "always"):
            self.always.add(always_scope(tool, kind, args))
            self.on_event("approved_always", tool=tool, kind=kind)
            return Decision(True, kind, "always")
        if answer in ("y", "yes"):
            self.on_event("approved", tool=tool, kind=kind)
            return Decision(True, kind, "user")
        self.on_event("denied", tool=tool, kind=kind)
        return Decision(False, kind, "denied", "The user denied this action. Do not retry it; try another approach or explain.")


def always_scope(tool: str, kind: str, args: dict) -> str:
    """What an "always" answer covers. Write and git: the tool. Shell and unknown tools: only this
    exact call, so "always" on `pytest -q` never covers `rm -rf`."""
    if kind in (WRITE, GIT):
        return tool
    return tool + " " + json.dumps(args, sort_keys=True, default=str)


def always_label(tool: str, kind: str, args: dict) -> str:
    if kind in (WRITE, GIT):
        return f"always allow {tool} for this session (any {kind} call to {tool})"
    exact = args.get("command") or args.get("code") or (json.dumps(args, default=str) if args else "(the repo's auto-detected test command)")
    return f"always allow only this exact {tool} call for this session: {sanitize(str(exact))[:120]}"


# -- what the user sees ------------------------------------------------------------------------

def sanitize(text: str) -> str:
    """Make control characters (such as terminal escapes) visible so they cannot hide anything."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" for c in text)


def _short(args: dict, limit: int = 300) -> str:
    text = json.dumps(args, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def describe(tool: str, kind: str, args: dict, repo: Path, via_call_tool: bool = False, max_lines: int = 40) -> str:
    """Plain-language approval prompt, written by code. Shows the exact diff or command."""
    verb = {WRITE: "change files", SHELL: "run a command", GIT: "use git", UNKNOWN: "use a tool pinch does not recognise"}.get(kind, "act")
    head = f"pinch wants to {verb} in {repo}\n  tool: {tool} ({kind}){'  [requested through call_tool]' if via_call_tool else ''}"
    body = _preview(tool, args, repo)
    lines = sanitize(body).splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines not shown)"]
    return head + "\n" + "\n".join("    " + line for line in lines)


def _preview(tool: str, args: dict, repo: Path) -> str:
    def current(path_arg: str) -> str:
        try:
            return (repo / Path(str(path_arg)).expanduser()).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    if tool == "edit_file":
        path, old_s, new_s = str(args.get("file_path")), str(args.get("old_string", "")), str(args.get("new_string", ""))
        before = current(path)
        if old_s and old_s in before:  # show the real change, with context, as it will be applied
            return _diff(path, before, before.replace(old_s, new_s, 1))
        return _diff(path, old_s, new_s)  # the edit will probably fail; show what was asked
    if tool == "write_file":
        path = str(args.get("file_path"))
        new = str(args.get("content", ""))
        old = "" if args.get("mode") == "append" else current(path)
        note = "(appending to the file)\n" if args.get("mode") == "append" else ""
        return note + _diff(path, old, new)
    if tool == "apply_patch":
        return f"patch for {args.get('file_path')}:\n{args.get('patch_text', '')}"
    if tool in ("run_command", "run_test"):
        return "$ " + str(args.get("command") or "(auto-detected test command)")
    if tool == "run_python":
        return "python code:\n" + str(args.get("code", ""))
    return json.dumps(args, indent=2, default=str)


def _diff(path: str, old: str, new: str) -> str:
    a, b = old.splitlines(), new.splitlines()
    out = list(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", lineterm="", n=2))
    return "\n".join(out) or "(no change)"
