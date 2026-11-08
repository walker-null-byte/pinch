"""Stale bytecode: Python trusts a cached .pyc whose recorded mtime (whole seconds) and size match the source. An agent that edits a
file twice in one second, to the same size, then runs the tests against the OLD code. pinch runs every tool with
PYTHONDONTWRITEBYTECODE=1, so it never leaves a .pyc behind to go stale."""

import json
import os
import shlex
import subprocess
import sys

from pinch.tools import ToolBox

STAMP = 1_700_000_000  # one fixed modification time, to make "edited within the same second" deterministic


def edit(repo, value):
    path = repo / "mod.py"
    path.write_text(f"VALUE = {value}\n")
    os.utime(path, (STAMP, STAMP))


def plain_run(repo, env):
    return subprocess.run([sys.executable, "show.py"], cwd=repo, env=env, capture_output=True, text=True).stdout.strip()


def make_repo(root):
    root.mkdir()
    (root / "show.py").write_text("import mod\nprint(mod.VALUE)\n")
    edit(root, 1)
    return root


def run_in_pinch(box, command):
    parsed = box.parse_call("run_command", {"command": command})
    assert not parsed.error
    return json.loads(box.execute(parsed))


def test_without_the_variable_a_same_second_same_size_edit_runs_stale_code(tmp_path):
    """Why the protection exists: this is the failure it prevents."""
    repo = make_repo(tmp_path / "plain")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    assert plain_run(repo, env) == "1"
    edit(repo, 2)                                    # same size, same mtime second
    assert plain_run(repo, env) == "1"               # the old bytecode ran: the edit is invisible


def test_pinch_tool_runs_see_every_edit_and_leave_no_bytecode(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    repo = make_repo(tmp_path / "pinch")
    box = ToolBox(repo, "lazy")
    command = f"{shlex.quote(sys.executable)} show.py"
    assert run_in_pinch(box, command)["stdout"].strip() == "1"
    edit(repo, 2)
    assert run_in_pinch(box, command)["stdout"].strip() == "2"          # the edit is seen
    assert not list(repo.rglob("*.pyc")) and not list(repo.rglob("__pycache__"))


def test_the_variable_is_set_while_a_tool_runs_and_put_back_after(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    box = ToolBox(repo, "lazy")
    probe = f"{shlex.quote(sys.executable)} -c \"import os; print(os.environ.get('PYTHONDONTWRITEBYTECODE'))\""
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    assert run_in_pinch(box, probe)["stdout"].strip() == "1"
    assert "PYTHONDONTWRITEBYTECODE" not in os.environ                   # it does not leak into pinch's own process
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    assert run_in_pinch(box, probe)["stdout"].strip() == "1"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "0"                  # the caller's own value is restored
