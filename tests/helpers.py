"""Small helpers for writing scripted-model tests."""

import shlex
import sys


def call(name: str, lazy: bool = True, **args) -> dict:
    """A scripted tool call. In lazy mode it goes through the call_tool meta-tool."""
    if lazy:
        return {"name": "call_tool", "arguments": {"name": name, "arguments": args}}
    return {"name": name, "arguments": args}


def run_pytest_cmd() -> str:
    return f"{shlex.quote(sys.executable)} -m pytest -q"


def fix_calc_script(lazy: bool = True) -> list[dict]:
    """The scripted model: read calc.py, fix it, run the tests, finish."""
    return [
        {"tool_calls": [call("read_file", lazy, file_path="calc.py")]},
        {"tool_calls": [call("edit_file", lazy, file_path="calc.py", old_string="a - b", new_string="a + b")]},
        {"tool_calls": [call("run_command", lazy, command=run_pytest_cmd())]},
        {"content": "Fixed add(): it subtracted instead of adding. Tests pass."},
    ]


def pytest_command() -> str:
    """The repo's test command for tests: this interpreter's pytest, quiet, without a cache directory."""
    return f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
