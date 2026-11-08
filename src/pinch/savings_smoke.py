"""Offline savings smoke: the same scripted tasks with `--savings none` and `--savings all`, comparing what is sent.

This is an OFFLINE BYTE ESTIMATE, not a measurement of real tokens. The "model" is a small rule-based stand-in that reacts
to what is in the conversation (it lists the repo only if it has no map, reads a file only if it cannot see it, re-reads to
verify, runs the tests, and stops when they pass). Because it reads the real request, pinch's own behaviour (what is in the first
prompt, file stubs, compressed test output, trimmed history, stopping on green) decides how many calls and bytes there are.
"Tokens" are the usage numbers the mock provider reports, which count 4 characters per token. Real numbers come in Phase 4.

    uv run python -m pinch.savings_smoke              # none vs all, three tasks
    uv run python -m pinch.savings_smoke --ablate     # also each technique on its own
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .cli import build_agent
from .config import Config
from .mock_provider import MockProvider
from .policy import load_policy
from .savings import TECHNIQUES
from .session import Session

TEST_CMD = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"


def call(name: str, **args) -> dict:
    return {"name": "call_tool", "arguments": {"name": name, "arguments": args}}


def filler(i: int) -> str:
    return "".join(f'def helper_{i}_{j}(value, scale={j}):\n    """Helper {i}.{j}: scale a value and clamp it to a range."""\n'
                   f"    result = value * scale\n    return max(0, min(result, 1000))\n\n\n" for j in range(4))


@dataclass
class Task:
    name: str
    prompt: str
    files: dict[str, str]
    edits: list[tuple[str, str, str]]            # (file, old text, new text), in order
    unlock: dict[int, str] = field(default_factory=dict)   # edit index -> text the model must have seen before it knows about it
    explore: list[str] = field(default_factory=list)       # grep patterns the model tries first when the task does not name the file
    verify_read: bool = False                    # re-read the file after editing, to check it


def _base() -> dict[str, str]:
    files = {f"util/helpers_{i}.py": filler(i) for i in range(8)}
    files["README.md"] = "# Demo project\n\nSmall demo used to compare token-saving techniques.\n"
    return files


TASKS = [
    Task("one bug, named file", "add() in calc.py returns the wrong result. Fix it.",
         {**_base(), "calc.py": "def add(a, b):\n    return a - b\n\n\ndef double(a):\n    return a * 2\n",
          "test_calc.py": "from calc import add, double\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_double():\n    assert double(4) == 8\n"},
         [("calc.py", "return a - b", "return a + b")]),
    Task("two bugs, noisy tests", "shapes.py is wrong: fix it so the tests pass.",
         {**_base(), "shapes.py": "def area(w, h):\n    return w + h\n\n\ndef perimeter(w, h):\n    return w - h\n",
          "test_shapes.py": "import pytest\nfrom shapes import area, perimeter\n\n\ndef test_area():\n    assert area(3, 4) == 12\n\n\n"
                            "@pytest.mark.parametrize('w', range(1, 26))\ndef test_perimeter(w):\n    assert perimeter(w, 2) == 2 * (w + 2), f'perimeter for width {w}'\n"},
         [("shapes.py", "return w + h", "return w * h"), ("shapes.py", "return w - h", "return 2 * (w + h)")],
         unlock={1: "test_perimeter"}),
    Task("file not named, re-read to verify", "The stock count is off by one somewhere in the inventory code.",
         {**_base(), "inventory.py": "STOCK = {}\n\n\ndef stock_count(item):\n    return STOCK.get(item, 0) + 1\n\n\ndef add_stock(item, n):\n    STOCK[item] = STOCK.get(item, 0) + n\n",
          "test_inventory.py": "from inventory import add_stock, stock_count\n\n\ndef test_count():\n    add_stock('bolt', 3)\n    assert stock_count('bolt') == 3\n"},
         [("inventory.py", "STOCK.get(item, 0) + 1", "STOCK.get(item, 0)")], explore=["stock", "count"], verify_read=True),
]


def stand_in_model(task: Task):
    """A rule-based 'model': a function (call number, request body) -> mock response entry."""
    def respond(n: int, body: dict) -> dict:
        msgs = body["messages"]
        steps, results = [], {}
        for m in msgs:
            if m["role"] == "tool":
                results[m["tool_call_id"]] = m["content"]
        for m in msgs:
            for c in m.get("tool_calls") or []:
                outer = json.loads(c["function"]["arguments"])
                inner = outer.get("arguments", {})
                steps.append((outer["name"], inner if isinstance(inner, dict) else json.loads(inner), results.get(c["id"], "")))
        seen = "\n".join(m["content"] for m in msgs if m["role"] in ("user", "tool") and isinstance(m.get("content"), str))

        def edited(i):
            f, old, _ = task.edits[i]
            return any(nm == "edit_file" and a.get("old_string") == old and a.get("file_path") == f for nm, a, _r in steps)

        names = [s[0] for s in steps]
        if "[pinch: repo map" not in seen and "list_directory" not in names:
            return {"tool_calls": [call("list_directory", dir_path=".")]}
        if task.edits[0][1] not in seen:                              # the model cannot see the file yet
            greps = [s for s in steps if s[0] == "grep_search"]
            if len(greps) < len(task.explore):
                return {"tool_calls": [call("grep_search", pattern=task.explore[len(greps)])]}
        for i, (f, old, new) in enumerate(task.edits):
            if i in task.unlock and task.unlock[i] not in seen:
                continue
            if edited(i):
                continue
            if old not in seen:
                return {"tool_calls": [call("read_file", file_path=f)]}
            return {"tool_calls": [call("edit_file", file_path=f, old_string=old, new_string=new)]}
        last_edit = max((k for k, s in enumerate(steps) if s[0] == "edit_file"), default=-1)
        after = steps[last_edit + 1:]
        if task.verify_read and not any(s[0] == "read_file" for s in after) and last_edit >= 0 and "pinch ran the tests" not in steps[last_edit][2]:
            return {"tool_calls": [call("read_file", file_path=task.edits[-1][0])]}
        ran_by_pinch = last_edit >= 0 and "pinch ran the tests" in steps[last_edit][2]
        runs = [s for s in after if s[0] == "run_command"]
        if not runs and not ran_by_pinch:
            return {"tool_calls": [call("run_command", command=TEST_CMD)]}
        if runs and '"returncode":0' in runs[-1][2]:
            return {"content": "Fixed. The tests pass."}
        if any(i in task.unlock and task.unlock[i] in seen and not edited(i) for i in range(len(task.edits))):
            for i in range(len(task.edits)):
                if not edited(i):
                    f, old, new = task.edits[i]
                    return {"tool_calls": [call("read_file", file_path=f)] if old not in seen else [call("edit_file", file_path=f, old_string=old, new_string=new)]}
        return {"content": "I could not make the tests pass."}
    return respond


@dataclass
class Outcome:
    calls: int
    request_bytes: int
    input_tokens: int
    fixed: bool
    stopped_on_green: bool


def run_task(task: Task, spec: str, workdir: Path) -> Outcome:
    repo = workdir / "repo"
    for rel, text in task.files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.email=s@x", "-c", "user.name=s", "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    with MockProvider(stand_in_model(task)).start() as mock:
        cfg = Config(base_url=mock.url, model="mock-small", session_dir=workdir / "sessions", config_path=workdir / "none.toml",
                     spend_dir=workdir / "spend", max_steps=14, retries=1, auto_approve=True, savings=spec, test_command=TEST_CMD)
        policy = load_policy(repo)
        session = Session.create(cfg.session_dir, {"repo": str(policy.repo)})
        agent = build_agent(cfg, policy, session, ask=lambda p: "n", out=lambda s: None)
        agent.run(task.prompt)
        rows = [r for r in session.records() if r["type"] == "ledger"]
        outcome = Outcome(len(mock.recorded), sum(len(json.dumps(r["body"])) for r in mock.recorded), sum(r["input_tokens"] for r in rows),
                          subprocess.run(TEST_CMD, shell=True, cwd=repo, capture_output=True).returncode == 0,
                          any(r["type"] == "event" and r["event"] == "stop_on_green" for r in session.records()))
    return outcome


def measure(spec: str, task: Task) -> Outcome:
    with tempfile.TemporaryDirectory(prefix="pinch-smoke-") as tmp:
        return run_task(task, spec, Path(tmp))


def table(ablate: bool = False) -> tuple[str, list[tuple[str, Outcome, Outcome]]]:
    lines = ["Offline savings smoke: byte estimate with a rule-based stand-in model and mock-reported usage (4 chars per token). NOT real tokens.", "",
             f"{'task':<36}{'':<6}{'calls':>6}{'request KB':>12}{'input tokens':>14}{'fixed':>7}",]
    pairs = []
    for task in TASKS:
        none, everything = measure("none", task), measure("all", task)
        pairs.append((task.name, none, everything))
        for label, o in (("none", none), ("all", everything)):
            lines.append(f"{task.name if label == 'none' else '':<36}{label:<6}{o.calls:>6}{o.request_bytes / 1024:>12.1f}{o.input_tokens:>14}{'yes' if o.fixed else 'NO':>7}")
        lines.append(f"{'':<36}{'saved':<6}{100 * (1 - everything.calls / none.calls):>5.0f}%{100 * (1 - everything.request_bytes / none.request_bytes):>11.0f}%"
                     f"{100 * (1 - everything.input_tokens / none.input_tokens):>13.0f}%")
    total_none, total_all = sum(p[1].request_bytes for p in pairs), sum(p[2].request_bytes for p in pairs)
    lines.append(f"\nAll three tasks: {total_none / 1024:.1f} KB with none, {total_all / 1024:.1f} KB with all = {100 * (1 - total_all / total_none):.0f}% fewer bytes sent.")
    if ablate:
        lines += ["", "Each technique on its own (request bytes saved versus none, per task; the three tasks in order):"]
        for name in TECHNIQUES:
            saved = [100 * (1 - measure(name, t).request_bytes / base.request_bytes) for t, (_n, base, _a) in zip(TASKS, pairs)]
            lines.append(f"  {name:<13}" + "".join(f"{s:>8.0f}%" for s in saved) + f"   {TECHNIQUES[name]}")
    return "\n".join(lines), pairs


def main() -> int:
    text, _ = table(ablate="--ablate" in sys.argv)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
