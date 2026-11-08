"""Build set C (bench/tasks/C): five tasks that need library APIs newer than typical model training data.

    uv run python scripts/build_c_tasks.py

These tasks are WRITTEN BY US (authored_by = pinch-team). They are for the Tavily on/off comparison: each needs a feature that
was added in 2026 to a library whose exact version is pinned in the task's environment. Each task has a hidden check script that
tests behaviour (not the source text), and a reference solution that was run against it (`python -m pinch.bench validate --sets C`).

  click  8.5.1  (8.5.0 released 2026-08-24): Argument(help=...) and click.custom_version_option
  pytest 9.2.0  (9.1.0 released 2026-06-13, 9.2.0 2026-10-27): max_warnings, parametrize_long_str_id_strategy
  anyio  4.16.0 (released 2026-11-01): BlockingPortal.call_async
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "bench" / "tasks" / "C"
PIP = ["pytest==9.2.0", "click==8.5.1", "anyio==4.16.0"]
NOTE = "Package versions in this environment are fixed: click 8.5.1, pytest 9.2.0, anyio 4.16.0."

TASKS = {}

TASKS["click-argument-help"] = dict(
    title="click 8.5: help text for arguments",
    prompt="""cli.py is a small click command with two positional arguments, NAME and COUNT. Give them help text, 'Who to greet.' for NAME and 'How many times to greet.' for COUNT, so that `python cli.py --help` lists them in click's own dedicated section for positional arguments. Use click's built-in support for argument help; do not put the text in the docstring or in an epilog. The command's behaviour must not change.
""" + NOTE,
    repo={"cli.py": '''import click


@click.command()
@click.argument("name")
@click.argument("count", type=int, default=1)
@click.option("--shout", is_flag=True, help="Uppercase the greeting.")
def greet(name, count, shout):
    """Greet NAME, COUNT times."""
    for _ in range(count):
        text = f"Hello, {name}!"
        click.echo(text.upper() if shout else text)


if __name__ == "__main__":
    greet()
'''},
    gold={"cli.py": '''import click


@click.command()
@click.argument("name", help="Who to greet.")
@click.argument("count", type=int, default=1, help="How many times to greet.")
@click.option("--shout", is_flag=True, help="Uppercase the greeting.")
def greet(name, count, shout):
    """Greet NAME, COUNT times."""
    for _ in range(count):
        text = f"Hello, {name}!"
        click.echo(text.upper() if shout else text)


if __name__ == "__main__":
    greet()
'''},
    check='''import sys
from click.testing import CliRunner
import cli

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

res = CliRunner().invoke(cli.greet, ["--help"])
out = res.output
if res.exit_code != 0:
    fail(f"--help exited {res.exit_code}")
if "Positional arguments" not in out:
    fail("--help has no 'Positional arguments' section:\\n" + out)
tail = out.split("Positional arguments", 1)[1]
for text in ("Who to greet.", "How many times to greet."):
    if text not in tail:
        fail(f"{text!r} is not listed under 'Positional arguments':\\n" + out)
helps = {p.name: getattr(p, "help", None) for p in cli.greet.params if p.param_type_name == "argument"}
if helps != {"name": "Who to greet.", "count": "How many times to greet."}:
    fail(f"argument help attributes are {helps}")
if "Greet NAME, COUNT times." not in out or "--shout" not in out:
    fail("the rest of the help output changed")
run = CliRunner().invoke(cli.greet, ["Ann", "2", "--shout"])
if run.output != "HELLO, ANN!\\nHELLO, ANN!\\n":
    fail(f"behaviour changed: {run.output!r}")
print("PASS")
''',
)

TASKS["click-custom-version-option"] = dict(
    title="click 8.5: --version whose output comes from a callback",
    prompt="""tool.py is a click command group. Add a `--version` option to the `cli` group. It prints exactly one line, `<command path> 1.4.2 (config: <value>)`, and exits. `<command path>` is the click context's command path (the name the program was invoked as), and `<value>` is the value of the environment variable TOOL_CONFIG at the moment the option is processed, or `default` when it is not set. For example, with TOOL_CONFIG=/etc/tool.toml and the program invoked as `tool`, the output is `tool 1.4.2 (config: /etc/tool.toml)`. Use click's built-in decorator for a --version option whose output is produced by a callback; do not write the eager option by hand. The `hello` command must keep working.
""" + NOTE,
    repo={"tool.py": '''import click


@click.group()
def cli():
    """Example tool."""


@cli.command()
def hello():
    """Say hello."""
    click.echo("hello")


if __name__ == "__main__":
    cli()
'''},
    gold={"tool.py": '''import os

import click


def show(ctx):
    return f"{ctx.command_path} 1.4.2 (config: {os.environ.get('TOOL_CONFIG', 'default')})"


@click.group()
@click.custom_version_option(show)
def cli():
    """Example tool."""


@cli.command()
def hello():
    """Say hello."""
    click.echo("hello")


if __name__ == "__main__":
    cli()
'''},
    check='''import sys
from click.testing import CliRunner
import tool

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

runner = CliRunner()
for value, expected in (("/etc/tool.toml", "tool 1.4.2 (config: /etc/tool.toml)\\n"), ("/other.toml", "tool 1.4.2 (config: /other.toml)\\n")):
    res = runner.invoke(tool.cli, ["--version"], prog_name="tool", env={"TOOL_CONFIG": value})
    if res.output != expected:
        fail(f"with TOOL_CONFIG={value}: got {res.output!r}, expected {expected!r} (exit {res.exit_code})")
res = runner.invoke(tool.cli, ["--version"], prog_name="tool", env={"TOOL_CONFIG": None})
if res.output != "tool 1.4.2 (config: default)\\n":
    fail(f"without TOOL_CONFIG: got {res.output!r}")
res = runner.invoke(tool.cli, ["hello"], prog_name="tool")
if res.output != "hello\\n":
    fail(f"hello changed: {res.output!r}")
used = [p for p in tool.cli.params if p.callback is not None and "custom_version_option" in p.callback.__qualname__]
if not used:
    fail("the --version option was not made with click.custom_version_option")
print("PASS")
''',
)

TASKS["pytest-max-warnings"] = dict(
    title="pytest 9.1: fail the run when there are too many warnings",
    prompt="""Our CI should fail the test run when the suite emits more than 3 warnings, but the tests themselves must still pass: do not turn warnings into errors and do not edit the tests. The suite in tests/test_legacy.py currently emits 4 warnings. Add a pytest.ini that sets pytest's built-in limit for this (configuration only, no plugin and no conftest.py code).
""" + NOTE,
    repo={"tests/test_legacy.py": '''import warnings


def old_api():
    warnings.warn("old_api is deprecated", DeprecationWarning)
    return 1


def test_a():
    assert old_api() == 1


def test_b():
    assert old_api() == 1


def test_c():
    assert old_api() == 1
    assert old_api() == 1


def test_d():
    assert 1 + 1 == 2
'''},
    gold={"pytest.ini": "[pytest]\nmax_warnings = 3\n"},
    check='''import subprocess
import sys

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

def run(*extra):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra],
                          capture_output=True, text=True, timeout=120)

full = run()
text = full.stdout + full.stderr
if full.returncode == 0:
    fail("the run with 4 warnings passed; it should fail:\\n" + text)
if "4 passed" not in text:
    fail("the tests must all still pass (4 passed), got:\\n" + text)
if "4 warnings" not in text:
    fail("expected pytest to report the 4 warnings:\\n" + text)
if "warning" not in text.split("warnings summary")[-1].lower() or "max" not in text.lower():
    fail("the failure should come from the max-warnings limit:\\n" + text)
fewer = run("-k", "not test_c")  # 2 warnings, under the limit
if fewer.returncode != 0:
    fail("with 2 warnings the run should pass:\\n" + fewer.stdout + fewer.stderr)
exactly = run("-k", "not test_b")  # 3 warnings, exactly the limit: allowed
if exactly.returncode != 0:
    fail("with exactly 3 warnings the run should pass:\\n" + exactly.stdout + exactly.stderr)
print("PASS")
''',
)

TASKS["pytest-long-string-ids"] = dict(
    title="pytest 9.2: test ids for long string parameters",
    prompt="""The parametrized tests in tests/test_slugs.py have long string parameters, and pytest's automatic test ids for them are unhelpful (they fall back to `text0`, `text1`). Configure pytest in a pytest.ini so that a long string parameter gets the SHA-256 hex digest of its value as its test id, while short strings keep their own value as their id. Configuration only: do not edit the tests and do not pass ids yourself.
""" + NOTE,
    repo={"tests/test_slugs.py": '''import pytest

LONG_A = "alpha-" * 40
LONG_B = "beta-" * 50


@pytest.mark.parametrize("text", [LONG_A, LONG_B, "short"])
def test_nonempty(text):
    assert text
'''},
    gold={"pytest.ini": "[pytest]\nparametrize_long_str_id_strategy = sha256\n"},
    check='''import hashlib
import subprocess
import sys

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests/test_slugs.py"],
                     capture_output=True, text=True, timeout=120)
lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip().startswith("tests/")]
if len(lines) != 3:
    fail("expected 3 collected tests:\\n" + out.stdout + out.stderr)
expected = ["tests/test_slugs.py::test_nonempty[" + hashlib.sha256(("alpha-" * 40).encode()).hexdigest() + "]",
            "tests/test_slugs.py::test_nonempty[" + hashlib.sha256(("beta-" * 50).encode()).hexdigest() + "]",
            "tests/test_slugs.py::test_nonempty[short]"]
if lines != expected:
    fail("test ids are\\n  " + "\\n  ".join(lines) + "\\nexpected\\n  " + "\\n  ".join(expected))
print("PASS")
''',
)

TASKS["anyio-portal-call-async"] = dict(
    title="anyio 4.16: await a function in another event loop through a BlockingPortal",
    prompt="""bridge.py connects request handlers to a worker that runs its own event loop in a separate thread (reached through an anyio BlockingPortal). Implement `lookup_in_worker(portal, name)` so that it runs `lookup(name)` inside the worker's event loop and returns its result, with these two properties: (1) it must not block the caller's event loop (other tasks of the caller keep running while it waits), and (2) if the calling task is cancelled, the work in the worker loop is cancelled as well. Use the BlockingPortal API of the installed anyio.
""" + NOTE,
    repo={"bridge.py": '''"""Bridge between request handlers and a worker that runs its own event loop in another thread."""

import anyio
from anyio.from_thread import BlockingPortal


async def lookup(name: str) -> str:
    """Runs inside the worker's event loop. Slow on purpose."""
    await anyio.sleep(0.05)
    return name.upper()


async def lookup_in_worker(portal: BlockingPortal, name: str) -> str:
    """Called from a request handler (a different event loop than the portal's)."""
    raise NotImplementedError
'''},
    gold={"bridge.py": '''"""Bridge between request handlers and a worker that runs its own event loop in another thread."""

import anyio
from anyio.from_thread import BlockingPortal


async def lookup(name: str) -> str:
    """Runs inside the worker's event loop. Slow on purpose."""
    await anyio.sleep(0.05)
    return name.upper()


async def lookup_in_worker(portal: BlockingPortal, name: str) -> str:
    """Called from a request handler (a different event loop than the portal's)."""
    return await portal.call_async(lookup, name)
'''},
    check='''import sys
import threading
import anyio
from anyio.from_thread import start_blocking_portal
import bridge

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

cancelled = threading.Event()

async def slow(name):
    try:
        await anyio.sleep(0.4)
        return name.upper()
    except anyio.get_cancelled_exc_class():
        cancelled.set()
        raise

bridge.lookup = slow
ticks = 0

async def ticker():
    global ticks
    while True:
        await anyio.sleep(0.01)
        ticks += 1

async def main(portal):
    global ticks
    async with anyio.create_task_group() as tg:
        tg.start_soon(ticker)
        result = await bridge.lookup_in_worker(portal, "bob")
        tg.cancel_scope.cancel()
    if result != "BOB":
        fail(f"wrong result {result!r}")
    if ticks < 10:
        fail(f"the caller's event loop was blocked (only {ticks} ticks in 0.4 s)")
    with anyio.move_on_after(0.1):
        await bridge.lookup_in_worker(portal, "never")
        fail("the call should have been cancelled by the timeout")

with start_blocking_portal() as portal:
    anyio.run(main, portal)
    if not cancelled.wait(2):
        fail("cancelling the caller did not cancel the work in the worker loop")
print("PASS")
''',
)


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    for slug, t in TASKS.items():
        root = OUT / slug
        for kind in ("repo", "gold"):
            (root / kind).mkdir(parents=True, exist_ok=True)
            for rel, text in t[kind].items():
                (root / kind / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / kind / rel).write_text(text)
        (root / "check").mkdir(parents=True, exist_ok=True)
        (root / "check" / "_bench_check.py").write_text(t["check"])
        (root / "task.json").write_text(json.dumps({
            "id": f"C/{slug}", "set": "C", "title": t["title"], "prompt": t["prompt"], "authored_by": "pinch-team",
            "env": {"pip": PIP, "pythonpath": ["."]},
            "check": {"command": "python _bench_check.py", "timeout": 120},
            "solution_files": list(t["gold"]),
            "source": {"written_by": "pinch-team (this project)", "note": "Written by us for the Tavily on/off comparison; not from any public benchmark.",
                       "libraries": ["click==8.5.1", "pytest==9.2.0", "anyio==4.16.0"]},
        }, indent=2) + "\n")
    (OUT / "SOURCE.md").write_text("""# Set C: docs tasks, WRITTEN BY US

These five tasks were written by the pinch team (not taken from any public benchmark) for the Tavily docs on/off comparison.
Each needs a feature that appeared in a 2026 release of a library, with the exact version pinned in the task's environment,
so a model cannot be expected to know the API from training data. Each has a hidden behavioural check (`check/_bench_check.py`)
and a reference solution (`gold/`) that was run against it.

| Task | Library and feature |
|---|---|
| click-argument-help | click 8.5.0 (2026-08-24): `Argument(help=...)` and the "Positional arguments" help section |
| click-custom-version-option | click 8.5.0: `click.custom_version_option` |
| pytest-max-warnings | pytest 9.1.0 (2026-06-13): the `max_warnings` option |
| pytest-long-string-ids | pytest 9.2.0 (2026-10-27): `parametrize_long_str_id_strategy` |
| anyio-portal-call-async | anyio 4.16.0 (2026-11-01): `BlockingPortal.call_async` |

Caveat: the container has network access (it needs it to reach the gateway), and the installed packages carry their own docstrings,
so an agent can also find the API by reading the library's source. That is a legitimate route and is part of what the comparison measures.
""")
    print(f"wrote {len(TASKS)} tasks to {OUT}")


if __name__ == "__main__":
    main()
