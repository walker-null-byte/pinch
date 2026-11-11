"""`python -m pinch.bench <command>`: setup, list, validate, run, summarize, charts, smoke, clean."""

from __future__ import annotations

import argparse
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import CACHE_DIR, RESULTS_DIR, TASKS_DIR
from .tasks import SETS, TaskError, load_tasks


def csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_setup(args) -> int:
    from . import sandbox
    if not sandbox.docker_ok():
        print("bench: Docker is not running or not reachable.", file=sys.stderr)
        return 2
    sandbox.ensure_image()
    built = sandbox.setup_tools(force=args.force)
    print(f"image {sandbox.IMAGE}: ready")
    print("tool venvs built: " + (", ".join(built) if built else "none (already there; use --force to rebuild)"))
    print("versions:", ", ".join(f"{k}=={v}" for k, v in sandbox.tool_versions().items()))
    return 0


def cmd_list(args) -> int:
    tasks = load_tasks(args.sets)
    for s in args.sets:
        mine = [t for t in tasks if t.set == s]
        print(f"set {s}: {len(mine)} tasks")
        if args.verbose:
            for t in mine:
                print(f"  {t.id}  [{t.authored_by}]  {t.title}")
    return 0


def cmd_validate(args) -> int:
    from . import sandbox
    from .score import validate_task
    if not sandbox.docker_ok():
        print("bench: Docker is not running or not reachable.", file=sys.stderr)
        return 2
    sandbox.ensure_image()
    tasks = load_tasks(args.sets, only=args.only or None, limit=args.limit)
    work_root = Path(tempfile.mkdtemp(prefix="validate-", dir=CACHE_DIR if CACHE_DIR.exists() else None))
    # build the environments first, one at a time (they are shared between tasks)
    envs = {t.id: sandbox.ensure_env(t.pip) for t in tasks}
    bad = 0

    def one(task):
        return validate_task(task, envs[task.id], work_root / task.id.replace("/", "_"))

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for result in pool.map(one, tasks):
            flag = "ok  " if result["ok"] else "FAIL"
            print(f"{flag} {result['task']:<48} unsolved: {result['unsolved_summary'][:45]:<45} | reference: {result['gold_summary'][:45]}")
            if not result["ok"]:
                bad += 1
                for k in ("gold_tail", "unsolved_tail"):
                    if k in result:
                        print("     " + result[k][-400:].replace("\n", "\n     "))
    print(f"\n{len(tasks) - bad}/{len(tasks)} tasks validated (unsolved fails, reference solution passes)")
    import shutil
    shutil.rmtree(work_root, ignore_errors=True)
    return 1 if bad else 0


def cmd_clean(args) -> int:
    from . import sandbox
    n = sandbox.clean_containers()
    print(f"removed {n} leftover bench container(s)")
    runs = CACHE_DIR / "runs"
    if runs.exists():
        import shutil
        shutil.rmtree(runs, ignore_errors=True)
        print(f"removed {runs}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pinch.bench", description="pinch benchmark harness (Phase 4a).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="pull the pinned image and build the tool venvs (pinch, mini-swe-agent)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("list", help="show the task sets")
    s.add_argument("--sets", type=csv, default=list(SETS))
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("validate", help="check every task: unsolved fails, reference solution passes (no model involved)")
    s.add_argument("--sets", type=csv, default=list(SETS))
    s.add_argument("--only", type=csv, default=[])
    s.add_argument("--limit", type=int)
    s.add_argument("--jobs", type=int, default=3)
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("clean", help="remove leftover bench containers and work dirs")
    s.set_defaults(fn=cmd_clean)

    from .runner import add_run_parser
    add_run_parser(sub)
    from .summary import add_summarize_parser
    add_summarize_parser(sub)
    from .charts import add_charts_parser
    add_charts_parser(sub)
    from .mock import add_smoke_parser
    add_smoke_parser(sub)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except TaskError as e:
        print(f"bench: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nbench: interrupted. Run `python -m pinch.bench clean` to remove leftover containers.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
