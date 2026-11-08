"""Task loader. A task is a folder under bench/tasks/<SET>/<name>/ with:

    task.json   id, set, title, prompt, env, check, source, authored_by
    repo/       what the agent starts with (copied to a fresh work dir for every run)
    check/      files laid over the work dir AFTER the agent has finished, then the check command runs
                (A: the exercise's own tests. B: the upstream fix's tests, which the agent never sees. C: our check script)
    gold/       a reference solution laid over the work dir. Used only by `validate` and by the MOCK model, never shown to an agent

Set A  Aider polyglot benchmark, Python exercises (Exercism, MIT).
Set B  real upstream bug fixes: the repo as it was just before the fix commit; the fix's test is the check.
Set C  tasks about library APIs newer than typical model training data. Written by us.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import TASKS_DIR

SETS = ("A", "B", "C")

# Named subsets, so a partial run is reproducible. `ablate15` is for the ablation and style runs: every set B task plus 7 set A
# exercises picked to spread over difficulty. Difficulty proxy: size of the reference solution (11 to 102 lines), which tracks how much
# an agent has to write; the three smallest-to-largest tiers are represented: proverb (easy), dominoes and list-ops (medium-easy),
# wordy (medium), react, forth and paasio (the exercises Aider's own harness finds hardest).
SUBSETS: dict[str, dict[str, object]] = {
    "ablate15": {"A": ("proverb", "dominoes", "list-ops", "wordy", "react", "forth", "paasio"), "B": "all"},
}
REQUIRED = ("id", "set", "title", "prompt", "env", "check", "source", "authored_by")


class TaskError(Exception):
    pass


@dataclass(frozen=True)
class Task:
    id: str  # "A/two-bucket"
    set: str
    title: str
    prompt: str
    root: Path
    pip: tuple[str, ...]  # packages the task's venv needs (exact pins for set C)
    pythonpath: tuple[str, ...]  # relative to the work dir
    check_command: str
    check_timeout: int
    source: dict
    authored_by: str
    solution_files: tuple[str, ...] = ()  # the files an agent is expected to change (for the mock and for reporting)
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def repo_dir(self) -> Path:
        return self.root / "repo"

    @property
    def check_dir(self) -> Path:
        return self.root / "check"

    @property
    def gold_dir(self) -> Path:
        return self.root / "gold"

    @property
    def slug(self) -> str:
        return self.id.split("/", 1)[1]


def load_task(root: Path) -> Task:
    path = root / "task.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise TaskError(f"{path}: cannot read ({e})")
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise TaskError(f"{path}: missing {', '.join(missing)}")
    if data["set"] not in SETS:
        raise TaskError(f"{path}: set must be one of {SETS}, not {data['set']!r}")
    if data["id"] != f"{data['set']}/{root.name}":
        raise TaskError(f"{path}: id {data['id']!r} does not match its folder ({data['set']}/{root.name})")
    for sub in ("repo", "check", "gold"):
        if not (root / sub).is_dir():
            raise TaskError(f"{root}: missing the {sub}/ folder")
    env, check = data["env"], data["check"]
    if "command" not in check:
        raise TaskError(f"{path}: check needs a command")
    if data["set"] == "C" and data["authored_by"] != "pinch-team":
        raise TaskError(f"{path}: set C tasks are written by us and must say authored_by = pinch-team")
    if data["set"] == "B" and not {"repo", "fix_commit", "base_commit", "license"} <= set(data["source"]):
        raise TaskError(f"{path}: a set B task records repo, base_commit, fix_commit and license in source")
    return Task(
        id=data["id"], set=data["set"], title=data["title"], prompt=data["prompt"], root=root,
        pip=tuple(env.get("pip", ())), pythonpath=tuple(env.get("pythonpath", ())),
        check_command=check["command"], check_timeout=int(check.get("timeout", 300)),
        source=data["source"], authored_by=data["authored_by"], solution_files=tuple(data.get("solution_files", ())),
        extra={k: v for k, v in data.items() if k not in REQUIRED},
    )


def load_tasks(sets=SETS, root: Path | None = None, only: list[str] | None = None, limit: int | None = None,
               subset: str | None = None) -> list[Task]:
    """All tasks of the given sets, in a stable order. `only` keeps tasks whose id or slug is listed.
    `limit` keeps the first N of each set. `subset` keeps a named subset (see SUBSETS); sets it does not list are left out."""
    if subset is not None and subset not in SUBSETS:
        raise TaskError(f"unknown subset {subset!r}; choose from {', '.join(SUBSETS)}")
    root = root or TASKS_DIR
    tasks: list[Task] = []
    for s in sets:
        if s not in SETS:
            raise TaskError(f"unknown set {s!r}; choose from {', '.join(SETS)}")
        folder = root / s
        found = [load_task(d) for d in sorted(folder.iterdir()) if (d / "task.json").is_file()] if folder.is_dir() else []
        if subset is not None:
            wanted = SUBSETS[subset].get(s)
            if wanted is None:
                found = []
            elif wanted != "all":
                missing = set(wanted) - {t.slug for t in found}
                if missing:
                    raise TaskError(f"subset {subset} names tasks that do not exist: {sorted(missing)}")
                found = [t for t in found if t.slug in wanted]
        if only:
            found = [t for t in found if t.id in only or t.slug in only]
        tasks.extend(found[:limit] if limit else found)
    return tasks


def prepare_workdir(task: Task, dest: Path) -> Path:
    """A fresh copy of the starting repo. Nothing from check/ or gold/ is in it."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(task.repo_dir, dest, symlinks=True)
    return dest


def overlay(src: Path, dest: Path) -> list[str]:
    """Copy every file under src over dest (creating folders). Returns the relative paths written."""
    written = []
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        written.append(str(rel))
    return written
