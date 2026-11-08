"""Pass or fail, decided outside the agent.

After an agent stops, the harness lays the task's check/ files over the work dir (restoring the exercise's tests, or adding the
upstream fix's tests the agent never saw), removes cached bytecode, and runs the check command in a FRESH container. Pass means
the command exits 0. What the agent said about its own work is never consulted.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .sandbox import container_run, task_env, task_mounts
from .tasks import Task, overlay


@dataclass
class CheckResult:
    passed: bool
    returncode: int
    seconds: float
    summary: str  # the last pytest summary line, or the last output line
    tail: str  # last ~1500 characters of output, for the row
    timed_out: bool = False


def purge_caches(work: Path) -> None:
    """Stale bytecode must never decide a result: delete every cache before the check."""
    for pattern in ("__pycache__", ".pytest_cache"):
        for p in work.rglob(pattern):
            shutil.rmtree(p, ignore_errors=True)
    for p in work.rglob("*.pyc"):
        p.unlink(missing_ok=True)


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def summary_line(text: str) -> str:
    text = ANSI.sub("", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if re.search(r"\b(passed|failed|error|errors|no tests ran|skipped)\b", ln):
            return ln.strip("= ")[:200]
    return lines[-1][:200] if lines else ""


def run_check(task: Task, work: Path, env_folder: str) -> CheckResult:
    purge_caches(work)
    overlay(task.check_dir, work)
    r = container_run(["sh", "-c", task.check_command], mounts=task_mounts(work), env=task_env(env_folder, task.pythonpath),
                      timeout=task.check_timeout, name=None)
    text = ANSI.sub("", (r.stdout or "") + (("\n" + r.stderr) if r.stderr else ""))
    return CheckResult(passed=(r.returncode == 0 and not r.timed_out), returncode=r.returncode, seconds=round(r.seconds, 2),
                       summary="timed out" if r.timed_out else summary_line(text), tail=text[-1500:], timed_out=r.timed_out)


def validate_task(task: Task, env_folder: str, scratch: Path) -> dict:
    """Does the check do its job? The starting repo must FAIL it and the reference solution must PASS it."""
    from .tasks import prepare_workdir
    out = {"task": task.id}
    work = prepare_workdir(task, scratch / "unsolved")
    t0 = time.time()
    before = run_check(task, work, env_folder)
    out["unsolved_fails"] = not before.passed
    out["unsolved_summary"] = before.summary
    work = prepare_workdir(task, scratch / "solved")
    overlay(task.gold_dir, work)
    after = run_check(task, work, env_folder)
    out["gold_passes"] = after.passed
    out["gold_summary"] = after.summary
    out["ok"] = out["unsolved_fails"] and out["gold_passes"]
    out["seconds"] = round(time.time() - t0, 1)
    if not out["ok"]:
        out["gold_tail" if not after.passed else "unsolved_tail"] = (after if not after.passed else before).tail[-600:]
    shutil.rmtree(scratch, ignore_errors=True)
    return out
