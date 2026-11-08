"""The offline check: the whole harness end to end with NO real model and NO keys.

`python -m pinch.bench smoke` starts the three mock providers behind the real Bifrost container (the gateway of `pinch prove`),
then runs pinch AND mini-swe-agent, each in its sandbox container, through that gateway. A scripted "model" (the Oracle below)
answers in the format each agent expects: pinch gets its own tool calls, mini-swe-agent gets `bash` tool calls. The Oracle knows
the reference solution of each task, so the check can pass; for tasks listed as unsolvable it gives up, so the check fails.

Everything this produces says MOCK: a banner on the console, `"mock": true` in every row, and a MOCK title in the reports.
The numbers are not model results. They only prove that the pipeline records, scores, resumes and summarises correctly.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import shutil
import time
from pathlib import Path

from . import RESULTS_DIR
from .modes import resolve_modes
from .tasks import Task, load_tasks

SMOKE_TASKS = ["A/two-bucket", "A/pig-latin", "B/toolz-interpose-empty"]
UNSOLVABLE = {"A/pig-latin"}  # the mock model gives up on this one, to show a failed check


def edit_blocks(old: str, new: str) -> list[tuple[str, str]]:
    """Minimal (old_string, new_string) edits that turn `old` into `new`; each old_string occurs exactly once in `old`."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    for context in (3, 6, 12, 40):
        blocks = []
        ok = True
        for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
            i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
            o, n = "".join(a[i1:i2]), "".join(b[j1:j2])
            if old.count(o) != 1 or not o:
                ok = False
                break
            blocks.append((o, n))
        if ok:
            return blocks
    return [(old, new)]


class Oracle:
    """A stand-in model. Stateless: it works out where it is in the conversation from the request itself."""

    def __init__(self, tasks: list[Task], unsolvable: set[str] = frozenset()):
        self.tasks = tasks
        self.unsolvable = set(unsolvable)
        self.calls: list[str] = []

    def task_for(self, body: dict) -> Task | None:
        first = next((m for m in body.get("messages", []) if m.get("role") == "user"), None)
        text = first.get("content") if first else ""
        text = text if isinstance(text, str) else json.dumps(text)
        return next((t for t in self.tasks if t.prompt in text), None)

    def plan(self, task: Task) -> list[tuple[str, str, str]]:
        """(kind, path, a|b) steps for the solution: ('edit', path, (old,new)) or ('write', path, content)."""
        steps = []
        for gold in sorted(p for p in task.gold_dir.rglob("*") if p.is_file()):
            rel = str(gold.relative_to(task.gold_dir))
            start = task.repo_dir / rel
            new = gold.read_text()
            if start.exists():
                steps += [("edit", rel, blk) for blk in edit_blocks(start.read_text(), new)]
            else:
                steps.append(("write", rel, new))
        return steps

    def __call__(self, n: int, body: dict) -> dict:
        task = self.task_for(body)
        names = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        step = sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")
        if task is None:
            return {"content": "I do not recognise this task."}
        self.calls.append(f"{task.id}#{step}")
        return self.mini(task, step) if "bash" in names else self.pinch(task, step, "call_tool" in names, body.get("messages", []))

    # pinch speaks its own tools (directly with --tool-mode full, wrapped in call_tool when lazy)
    def pinch(self, task: Task, step: int, wrapped: bool, body_messages: list) -> dict:
        def call(name, **args):
            return {"name": "call_tool", "arguments": {"name": name, "arguments": args}} if wrapped else {"name": name, "arguments": args}

        main = task.solution_files[0] if task.solution_files else "."
        first = next((m["content"] for m in body_messages if m.get("role") == "user"), "")
        if step >= 1 and "[pinch ran the tests before you started" in str(first):
            step += 1  # the `pretest` technique already ran the tests, so the stand-in model does not run them again
        if task.set == "C":  # docs tasks: the stand-in model looks the API up first (refused when the docs technique is off)
            if step == 0:
                return {"content": "I need the current API, so I look it up.",
                        "tool_calls": [call("docs_lookup", query=f"{task.slug} python library new API")]}
            step -= 1
        if step == 0:
            return {"content": "Let me look at the code first.", "tool_calls": [call("read_file", file_path=main)]}
        if step == 1:  # run the tests before changing anything: red on sets A and C-style tasks with failing tests, green on set B
            return {"content": "Running the tests first.", "tool_calls": [call("run_test", command=task.extra.get("test_command") or "python -m pytest -q")]}
        if step == 2:
            if task.id in self.unsolvable:
                return {"content": "I could not find a solution for this task."}
            calls = []
            for kind, rel, data in self.plan(task):
                calls.append(call("edit_file", file_path=rel, old_string=data[0], new_string=data[1]) if kind == "edit"
                             else call("write_file", file_path=rel, content=data))
            return {"content": "Applying the fix.", "tool_calls": calls}
        if step == 3:  # reached when pinch did not stop on green (no earlier red, or stopongreen off)
            return {"content": "Running the tests again.", "tool_calls": [call("run_test", command=task.extra.get("test_command") or "python -m pytest -q")]}
        return {"content": "Done. The change is made and the tests pass."}

    # mini-swe-agent has one tool, bash
    def mini(self, task: Task, step: int) -> dict:
        def bash(cmd, text):
            return {"content": text, "tool_calls": [{"name": "bash", "arguments": {"command": cmd}}]}

        main = task.solution_files[0] if task.solution_files else "."
        if step == 0:
            return bash("ls -la", "Let me see what is here.")
        if step == 1:
            return bash(f"cat {main}", "Reading the file to change.")
        if step == 2:
            if task.id in self.unsolvable:
                return bash("echo no change", "I do not see how to solve this.")
            payload = base64.b64encode(json.dumps([[kind, rel, list(data) if kind == "edit" else data]
                                                   for kind, rel, data in self.plan(task)]).encode()).decode()
            script = ("import base64,json,pathlib\n"
                      f"for kind,rel,d in json.loads(base64.b64decode('{payload}')):\n"
                      "    p=pathlib.Path(rel); p.parent.mkdir(parents=True,exist_ok=True)\n"
                      "    p.write_text(d if kind=='write' else p.read_text().replace(d[0],d[1],1))\n")
            return bash("python - <<'PYEOF'\n" + script + "PYEOF", "Applying the fix.")
        if step == 3:
            return bash((task.extra.get("test_command") or "python -m pytest -q") + " 2>&1 | tail -5", "Running the tests.")
        return bash("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "The tests pass, so I am done.")


class MockStack:
    """The three mock providers behind the real Bifrost container. `with MockStack(oracle):` starts and stops it."""

    def __init__(self, oracle: Oracle):
        from .. import prove
        from ..mock_provider import MockProvider
        self.prove, self.oracle, self.started_gateway = prove, oracle, False
        self.mocks = {name: MockProvider(oracle, port=port) for name, port in prove.PORTS.items()}

    def __enter__(self):
        prove = self.prove
        if not prove.docker_ok():
            raise SystemExit("bench: smoke needs Docker (the real Bifrost container).")
        for m in self.mocks.values():
            m.start()
        try:
            if prove.gateway_up():
                if not prove.is_demo_gateway():
                    raise SystemExit("bench: " + prove.NOT_DEMO.format(url=prove.GATEWAY_URL))
                prove.reset_demo_budgets()
            else:
                print(f"starting the Bifrost gateway ({prove.IMAGE}) ...", flush=True)
                self.started_gateway = True  # so a failed start is also cleaned up
                prove.start_gateway(prove.GATEWAY_DIR / "config.json")
        except BaseException:
            self.__exit__()  # never leave the mock providers bound to their ports
            raise
        return self

    def __exit__(self, *exc):
        for m in self.mocks.values():
            m.stop()
        if self.started_gateway:
            self.prove.stop_gateway()


def cmd_smoke(args) -> int:
    from .. import prove
    from .runner import Options, execute
    from .summary import summarize_file
    from .sandbox import ensure_image, tool_versions
    ids = list(args.tasks) + (["C/click-argument-help"] if args.with_docs and "C/click-argument-help" not in args.tasks else [])
    tasks = [t for t in load_tasks(only=ids) if t.id in ids]
    if len(tasks) != len(ids):
        raise SystemExit(f"bench: smoke tasks not found: {sorted(set(ids) - {t.id for t in tasks})}")
    out: Path = args.out
    for path in (out, out.with_suffix(".md"), out.with_suffix(".csv")):
        path.unlink(missing_ok=True)
    ensure_image()
    if not tool_versions():
        raise SystemExit("bench: the tool venvs are missing. Run: python -m pinch.bench setup")
    oracle = Oracle(tasks, UNSOLVABLE & set(ids))
    os.environ["PINCH_GATEWAY_KEY"] = "sk-bf-demo-alice"  # the demo key of the throwaway gateway; not a secret
    modes = resolve_modes(list(args.modes) + (["docs"] if args.with_docs else []))
    tavily = None
    if args.with_docs:  # a stand-in for Tavily's search API, so the docs_lookup path is exercised too
        from ..mock_provider import MockTavily
        tavily = MockTavily().start()
        os.environ["TAVILY_API_KEY"] = "tvly-mock-not-a-secret"
    opts = Options(model="mock-small", base_url=prove.GATEWAY_URL + "/v1", max_steps=12, max_tokens=1024, agent_timeout=240,
                   jobs=args.jobs, mock=True, rate_limit_sleep=1.0, infra_retries=1,
                   tavily_url=tavily.url if tavily else "")
    ns = type("Args", (), {"sets": sorted({t.set for t in tasks}), "only": ids, "limit": None, "repeat": args.repeat, "out": out,
                           "resume": False, "budget_usd": None})()
    t0 = time.time()
    try:
        with MockStack(oracle):
            code = execute(ns, modes, opts)
    finally:
        if tavily:
            tavily.stop()
    print(f"\nsmoke run finished in {time.time() - t0:.0f}s; the mock model answered {len(oracle.calls)} calls", flush=True)
    summarize_file(out, out.with_suffix(".md"), out.with_suffix(".csv"))
    print((out.with_suffix(".md")).read_text())
    return code


def add_smoke_parser(sub) -> None:
    from .cli import csv
    s = sub.add_parser("smoke", help="offline end-to-end check with mock providers (every output is labelled MOCK)")
    s.add_argument("--tasks", type=csv, default=SMOKE_TASKS, help=f"default: {','.join(SMOKE_TASKS)}")
    s.add_argument("--modes", type=csv, default=["baseline", "none", "all", "no-pretest", "cache-twice"])
    s.add_argument("--repeat", type=int, default=1)
    s.add_argument("--jobs", type=int, default=1)
    s.add_argument("--with-docs", action="store_true", help="also run one set C task with docs-on and docs-off against a mock Tavily")
    s.add_argument("--out", type=Path, default=RESULTS_DIR / "mock-smoke.jsonl")
    s.set_defaults(fn=cmd_smoke)
