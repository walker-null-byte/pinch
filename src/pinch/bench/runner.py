"""The benchmark runner: tasks x modes x repeats, one JSONL row per run.

    uv run python -m pinch.bench run --set A,B,C --modes core,ablate --model <name> --repeat 3 --out bench/results/run1.jsonl
    ... --resume        continue the same file: rows already done are skipped

Per run: fresh work dir -> agent in a throwaway container -> check in another fresh container -> row.
Rows whose run failed on infrastructure (rate limit that did not clear, outage, quota, budget, bad key) are written and marked
`status = "infra_error"`; the summary leaves them out of every average. Rate limits are waited out and retried (the wait grows each
time); quota or budget exhaustion and bad credentials stop the run cleanly so it can be resumed later.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import CACHE_DIR, RESULTS_DIR
from .agents import (Outcome, fill_cost, keep_artifacts, mini_command, mini_spec, parse_mini, parse_pinch, pinch_command)
from .modes import Mode, ModeError, resolve_modes
from .sandbox import (SECRET_ENV, SandboxError, container_run, docker_ok, ensure_env, ensure_image, task_env, task_mounts,
                      tool_versions, IMAGE, PINNED)
from .score import run_check
from .tasks import SETS, SUBSETS, Task, load_tasks, overlay, prepare_workdir

FATAL = {"quota", "budget", "config"}  # stop the whole run; resume later
RETRYABLE = {"provider_unavailable"}  # rate limits are handled separately in Run.attempts


@dataclass
class Options:
    model: str
    base_url: str = "http://127.0.0.1:8180/v1"
    max_steps: int = 30
    max_tokens: int = 4096
    agent_timeout: int = 900  # seconds of wall time one agent run may take
    tool_mode: str = "lazy"
    jobs: int = 1
    delay: float = 0.0  # seconds between starting runs (grows after rate limits, shrinks after successes)
    infra_retries: int = 3
    rate_limit_sleep: float = 30.0  # first wait after a rate limit; doubles each time (up to 15 minutes)
    rate_limit_window: float = 900.0  # a rate limit that still has not cleared after this many seconds is treated as the daily quota: stop
    min_call_interval: float = 0.0  # seconds between model calls inside one agent process (both agents); 0 = no pacing
    max_consecutive_infra: int = 8
    budget_usd: float | None = None
    small_model: str = ""
    big_model: str = ""
    mock: bool = False
    tavily_url: str = ""
    artifacts: bool = True
    keep_workdirs: bool = False
    tag: str = ""  # cache namespace prefix for cache-twice; stored in the file so --resume reuses it


class Stop(Exception):
    pass


class Throttle:
    """Spaces out run starts. A rate limit doubles the gap, each clean run shortens it again."""

    def __init__(self, base: float):
        self.base, self.gap, self.lock, self.next_at = base, base, threading.Lock(), 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.time()
            start = max(now, self.next_at)
            self.next_at = start + self.gap
        if start > now:
            time.sleep(start - now)

    def slower(self) -> None:
        with self.lock:
            self.gap = min(max(self.gap * 2, 5.0), 300.0)

    def faster(self) -> None:
        with self.lock:
            self.gap = self.base if self.gap * 0.8 < self.base + 0.5 else self.gap * 0.8


class Writer:
    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, row: dict) -> None:
        with self.lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())


def read_rows(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a half-written last line after a crash
    return rows


def row_key(row: dict) -> tuple:
    return (row["set"], row["task"], row["mode"], row["repeat"], row.get("phase", 1))


def done_keys(rows: list[dict]) -> set[tuple]:
    """A run is done unless it failed on infrastructure; those are run again on --resume."""
    return {row_key(r) for r in rows if r.get("type") == "run" and r.get("status") != "infra_error"}


# -- one run --------------------------------------------------------------------------------------

_env_lock = threading.Lock()


def git_diff(base: Path) -> tuple[str, list[str]]:
    """Diff of base/pristine against base/work, with the folder names stripped from the paths."""
    r = subprocess.run(["git", "diff", "--no-index", "--no-color", "--", "pristine", "work"], capture_output=True, text=True, cwd=base)
    text = r.stdout.replace("a/pristine/", "a/").replace("b/work/", "b/")
    files = sorted({ln.split(" b/", 1)[1] for ln in text.splitlines() if ln.startswith("diff --git") and " b/" in ln})
    return text, files


def settle_timeout(outcome: Outcome, timed_out: bool) -> None:
    """An agent killed at the outer wall-time limit, before it wrote its own result, ran out of time: the same rule for both
    agents. It counts as a `timeout` stop (not an infrastructure error) and the work it left behind is still checked.
    A clear outage it reported before that (rate limit, quota, budget, credentials, provider down) stays an outage."""
    if timed_out and outcome.infra in (None, "bad_request", "harness"):
        outcome.infra, outcome.infra_detail, outcome.stop = None, "", "timeout"


def run_once(task: Task, mode: Mode, repeat: int, phase: int, attempt: int, opts: Options, cache_key: str = "") -> dict:
    """One attempt at one run. Always returns a row; never raises for an infrastructure problem."""
    run_id = uuid.uuid4().hex[:12]
    base = CACHE_DIR / "runs" / run_id
    work, out, pristine = base / "work", base / "out", base / "pristine"
    out.mkdir(parents=True)
    row = {"type": "run", "ts": round(time.time(), 1), "run_id": run_id, "mock": opts.mock, "set": task.set, "task": task.id,
           "mode": mode.name, "agent": mode.agent, "repeat": repeat, "phase": phase, "attempt": attempt, "model": opts.model,
           "style": mode.style if mode.agent == "pinch" else "", "status": "ok", "infra_reason": None, "passed": None}
    t_start = time.time()
    try:
        prepare_workdir(task, work)
        shutil.copytree(work, pristine, symlinks=True)
        with _env_lock:
            env_folder = ensure_env(task.pip)
        env = task_env(env_folder, task.pythonpath)
        env.update({"LC_ALL": "C.UTF-8"})
        secrets = SECRET_ENV
        if mode.agent == "pinch":
            savings = mode.savings(task.set, bool(opts.small_model and opts.big_model))
            row["savings_requested"] = savings
            env.update({"PINCH_MAX_TOKENS": str(opts.max_tokens), "PINCH_KEY_HEADER": "x-bf-vk"})
            if opts.min_call_interval > 0:
                env["PINCH_MIN_CALL_INTERVAL"] = str(opts.min_call_interval)
            if opts.small_model and opts.big_model:
                env.update({"PINCH_SMALL_MODEL": opts.small_model, "PINCH_BIG_MODEL": opts.big_model})
            if opts.tavily_url:
                env["PINCH_TAVILY_URL"] = opts.tavily_url
            if task.extra.get("test_command"):
                env["PINCH_TEST_COMMAND"] = task.extra["test_command"]  # what a user would put in config; stop-on-green runs it
            if cache_key:
                env["PINCH_CACHE_KEY"] = cache_key
            command = pinch_command(task.prompt, model=opts.model, base_url=opts.base_url, savings=savings, style=mode.style,
                                    max_steps=opts.max_steps, tool_mode=opts.tool_mode)
        else:
            spec = mini_spec(task.prompt, model=opts.model, base_url=opts.base_url, max_steps=opts.max_steps,
                             max_tokens=opts.max_tokens, wall_time=opts.agent_timeout, min_call_interval=opts.min_call_interval)
            (out / "mini_spec.json").write_text(json.dumps(spec))
            command = mini_command()
        run = container_run(command, mounts=task_mounts(work, out), env=env, secrets=secrets, timeout=opts.agent_timeout + 120)
        row["agent_s"] = round(run.seconds, 1)
        outcome = parse_pinch(out, run.returncode, run.stdout) if mode.agent == "pinch" else parse_mini(out, run.returncode, run.stdout)
        settle_timeout(outcome, run.timed_out)
        fill_cost(outcome, opts.model)
        apply_outcome(row, outcome)
        if outcome.infra:
            row["status"], row["infra_reason"], row["infra_detail"] = "infra_error", outcome.infra, outcome.infra_detail
        else:
            check = run_check(task, work, env_folder)
            row.update(passed=check.passed, check_summary=check.summary, check_s=check.seconds)
        diff, files = git_diff(base)
        row["files_changed"], row["diff_lines"] = files, sum(1 for ln in diff.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))
        if opts.artifacts:
            folder = RESULTS_DIR / "artifacts" / run_id
            keep_artifacts(out, folder, diff, (run.stdout or "") + "\n--- stderr ---\n" + (run.stderr or ""))
            row["artifact"] = f"artifacts/{run_id}"
    except SandboxError as e:
        row.update(status="infra_error", infra_reason="harness", infra_detail=str(e)[:300])
    except Exception as e:  # a bug in the harness must not lose the other runs
        row.update(status="infra_error", infra_reason="harness", infra_detail=f"{type(e).__name__}: {e}"[:300])
    row["wall_s"] = round(time.time() - t_start, 1)
    if not opts.keep_workdirs:
        shutil.rmtree(base, ignore_errors=True)
    return row


def apply_outcome(row: dict, o: Outcome) -> None:
    row.update(
        stop=o.stop, calls=o.calls, input_tokens=o.input_tokens, output_tokens=o.output_tokens, cached_tokens=o.cached_tokens,
        total_tokens=o.total_tokens, usage_complete=o.usage_complete, cost_usd=o.cost_usd, cache_hits=o.cache_hits,
        cache_saved_tokens=o.cache_saved_tokens, auto_approvals=o.auto_approvals, tavily_calls=o.tavily_calls,
        tavily_credits=o.tavily_credits, escalations=o.escalations, fallbacks=o.fallbacks, techniques_on=o.techniques_on,
        served_models=o.served_models, pretest=o.pretest, final_text=o.final_text,
        wait_seconds=round(o.wait_seconds, 1), rate_limit_waits=o.rate_limit_waits,
    )


# -- the loop -------------------------------------------------------------------------------------

@dataclass
class Unit:
    task: Task
    mode: Mode
    repeat: int
    phases: tuple[int, ...] = (1,)


class Run:
    def __init__(self, units: list[Unit], opts: Options, writer: Writer, existing: list[dict], say=None):
        self.units, self.opts, self.writer = units, opts, writer
        self.done = done_keys(existing)
        self.spent = sum((r.get("cost_usd") or 0.0) for r in existing if r.get("type") == "run")
        self.throttle = Throttle(opts.delay)
        self.stop = threading.Event()
        self.stop_reason = ""
        self.consecutive_infra = 0
        self.rate_limited_since: float | None = None  # when the current unbroken streak of rate-limited runs began
        self.lock = threading.Lock()
        self.count = 0
        self.total = sum(len(u.phases) for u in units)
        self.say = say or (lambda s: print(s, file=sys.stderr, flush=True))
        self.new_rows: list[dict] = []

    def request_stop(self, reason: str) -> None:
        with self.lock:
            if not self.stop.is_set():
                self.stop_reason = reason
                self.stop.set()
                self.say(f"\nStopping cleanly: {reason}. Rows so far are saved; continue later with --resume.")

    def over_budget(self) -> bool:
        return self.opts.budget_usd is not None and self.spent >= self.opts.budget_usd

    def unit_worker(self, unit: Unit) -> None:
        cache_key = f"bench-{self.opts.tag}-{unit.task.slug}-r{unit.repeat}" if unit.mode.twice else ""
        for phase in unit.phases:
            key = (unit.task.set, unit.task.id, unit.mode.name, unit.repeat, phase)
            if key in self.done:
                continue
            if self.stop.is_set():
                return
            if self.over_budget():
                self.request_stop(f"the --budget-usd limit of ${self.opts.budget_usd:.2f} is used up (${self.spent:.2f} spent)")
                return
            self.attempts(unit, phase, cache_key)

    def attempts(self, unit: Unit, phase: int, cache_key: str) -> None:
        opts = self.opts
        attempt, other_failures = 0, 0
        while True:
            attempt += 1
            self.throttle.wait()
            row = run_once(unit.task, unit.mode, unit.repeat, phase, attempt, opts, cache_key)
            self.writer.write(row)
            with self.lock:
                self.spent += row.get("cost_usd") or 0.0
                self.new_rows.append(row)
                self.count += 1
                n = self.count
            self.say(describe(row, n, self.total))
            reason = row.get("infra_reason")
            if row["status"] != "infra_error":
                self.consecutive_infra = 0
                self.rate_limited_since = None
                self.throttle.faster()
                return
            if reason in FATAL:
                self.consecutive_infra += 1
                self.request_stop({"quota": "the provider's daily quota is used up", "budget": "the gateway budget is used up",
                                   "config": "the gateway rejected the credentials or the model name"}[reason])
                return
            if reason == "rate_limit":
                # A per-minute limit clears by waiting; keep waiting (the wait grows) until it has not cleared for rate_limit_window
                # seconds, which means it is really the daily quota. These runs do not count towards the infrastructure streak.
                now = time.time()
                self.rate_limited_since = self.rate_limited_since or now
                if now - self.rate_limited_since > opts.rate_limit_window:
                    self.request_stop(f"the rate limit has not cleared for {opts.rate_limit_window / 60:.0f} minutes (probably the daily quota)")
                    return
                self.throttle.slower()
                wait = min(900.0, opts.rate_limit_sleep * 2 ** min(attempt - 1, 5)) * random.uniform(0.9, 1.1)
                self.say(f"   rate_limit: waiting {wait:.0f}s, then trying this run again (attempt {attempt + 1})")
                self.sleep(wait)
                if self.stop.is_set():
                    return
                continue
            self.consecutive_infra += 1
            if self.consecutive_infra >= opts.max_consecutive_infra:
                self.request_stop(f"{self.consecutive_infra} runs in a row failed on infrastructure ({reason}: {row.get('infra_detail', '')[:120]})")
                return
            other_failures += 1
            if reason in RETRYABLE and other_failures <= opts.infra_retries:
                self.throttle.slower()
                wait = min(900.0, opts.rate_limit_sleep * 2 ** (other_failures - 1)) * random.uniform(0.9, 1.1)
                self.say(f"   {reason}: waiting {wait:.0f}s, then trying this run again (attempt {attempt + 1})")
                self.sleep(wait)
                if self.stop.is_set():
                    return
                continue
            return  # harness / bad request: kept as an infra row, move on

    def sleep(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def go(self) -> int:
        todo = [u for u in self.units if any((u.task.set, u.task.id, u.mode.name, u.repeat, p) not in self.done for p in u.phases)]
        skipped = len(self.units) - len(todo)
        runs = sum(1 for u in todo for p in u.phases if (u.task.set, u.task.id, u.mode.name, u.repeat, p) not in self.done)
        self.say(f"{runs} runs to do ({skipped} units already done)" if skipped else f"{runs} runs to do")
        with ThreadPoolExecutor(max_workers=self.opts.jobs) as pool:
            list(pool.map(self.unit_worker, todo))
        return 3 if self.stop_reason else 0


def describe(row: dict, n: int, total: int) -> str:
    head = f"[{n}/{total}] {row['task']:<46} {row['agent']}:{row['mode']:<18} r{row['repeat']}" + (f".{row['phase']}" if row.get("phase", 1) > 1 else "")
    if row["status"] == "infra_error":
        return f"{head} INFRA ERROR ({row['infra_reason']}): {row.get('infra_detail', '')[:100]}"
    verdict = "PASS" if row["passed"] else "fail"
    return (f"{head} {verdict}  tokens={row.get('total_tokens', 0)} calls={row.get('calls', 0)} "
            f"{row.get('agent_s', 0):.0f}s" + (f" (waited {row['wait_seconds']:.0f}s, {row.get('rate_limit_waits', 0)} rate-limit waits)" if row.get("wait_seconds") else "") + f"  {row.get('stop', '')}" + ("  [MOCK]" if row.get("mock") else ""))


# -- command line ---------------------------------------------------------------------------------

def build_units(tasks: list[Task], modes: list[Mode], repeat: int) -> list[Unit]:
    """repeat -> task -> mode, so a run cut short is balanced between modes."""
    units = []
    for r in range(repeat):
        for t in tasks:
            for m in modes:
                if t.set in m.sets:
                    units.append(Unit(t, m, r, (1, 2) if m.twice else (1,)))
    return units


def add_run_parser(sub) -> None:
    from .cli import csv
    s = sub.add_parser("run", help="run tasks x modes x repeats and write one JSONL row per run")
    s.add_argument("--set", dest="sets", type=csv, default=list(SETS), help="task sets, e.g. A,B,C (default: all)")
    s.add_argument("--modes", type=csv, default=["core"], help="modes and macros, comma separated (see `pinch.bench.modes`); default: core")
    s.add_argument("--model", required=True, help="model name as the gateway routes it")
    s.add_argument("--repeat", type=int, default=1)
    s.add_argument("--out", type=Path, default=RESULTS_DIR / "results.jsonl")
    s.add_argument("--resume", action="store_true", help="continue --out: skip rows already done (infrastructure failures are retried)")
    s.add_argument("--only", type=csv, default=[], help="only these task ids or names")
    s.add_argument("--limit", type=int, help="only the first N tasks of each set")
    s.add_argument("--subset", choices=sorted(SUBSETS), help="a named, reproducible subset, e.g. ablate15 (all 8 set B tasks + 7 set A tasks) for ablations and styles")
    s.add_argument("--base-url", default=os.environ.get("PINCH_BASE_URL", "http://127.0.0.1:8180/v1"))
    s.add_argument("--max-steps", type=int, default=30, help="most model calls per run, the same for both agents")
    s.add_argument("--max-tokens", type=int, default=4096, help="most output tokens per call, the same for both agents")
    s.add_argument("--timeout", type=int, default=900, help="seconds of wall time one agent run may take")
    s.add_argument("--tool-mode", choices=("lazy", "full"), default="lazy", help="pinch tool schemas (its default is lazy)")
    s.add_argument("--jobs", type=int, default=1, help="runs in parallel (free tiers rate-limit: keep it at 1 or 2)")
    s.add_argument("--delay", type=float, default=0.0, help="seconds between starting runs")
    s.add_argument("--min-call-interval", type=float, default=4.5,
                   help="seconds between model calls inside each agent process, the same for both agents (default 4.5: Gemini's free tier allows 15 a minute); 0 turns pacing off")
    s.add_argument("--infra-retries", type=int, default=3, help="times to wait out a rate limit or outage and try a run again")
    s.add_argument("--rate-limit-sleep", type=float, default=30.0, help="first wait after a rate limit (doubles each time, up to 15 min)")
    s.add_argument("--budget-usd", type=float, help="stop cleanly when the rows in --out add up to this much (needs a price for --model)")
    s.add_argument("--small-model", default="", help="cascade: the small tier (with --big-model; the gateway must route both)")
    s.add_argument("--big-model", default="")
    s.add_argument("--no-artifacts", action="store_true", help="do not keep sessions, trajectories and diffs")
    s.add_argument("--keep-workdirs", action="store_true")
    s.set_defaults(fn=cmd_run)

    p = sub.add_parser("plan", help="count the runs and estimate the model calls for a configuration (no model is called)")
    p.add_argument("--set", dest="sets", type=csv, default=list(SETS))
    p.add_argument("--modes", type=csv, default=["core"])
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--only", type=csv, default=[])
    p.add_argument("--limit", type=int)
    p.add_argument("--subset", choices=sorted(SUBSETS))
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--small-model", default="")
    p.add_argument("--big-model", default="")
    p.add_argument("--pinch-calls", type=float, default=8.0, help="assumed average model calls per pinch run")
    p.add_argument("--mini-calls", type=float, default=14.0, help="assumed average model calls per mini-swe-agent run")
    p.add_argument("--tokens-per-call", type=int, default=6000, help="assumed average tokens (input + output) per call")
    p.set_defaults(fn=cmd_plan)


def cmd_plan(args) -> int:
    """How many runs and model calls a configuration would make. The per-run averages are assumptions you can change; the upper
    bound is --max-steps calls for every run."""
    modes = resolve_modes(args.modes)
    if not (args.small_model and args.big_model):
        modes = [m for m in modes if m.name != "no-cascade"]
    tasks = load_tasks(args.sets, only=args.only or None, limit=args.limit, subset=getattr(args, "subset", None))
    units = build_units(tasks, modes, args.repeat)
    by_mode: dict[str, int] = {}
    for u in units:
        by_mode[u.mode.name] = by_mode.get(u.mode.name, 0) + len(u.phases)
    runs = sum(by_mode.values())
    est = sum(n * (args.mini_calls if m == "baseline" else args.pinch_calls) for m, n in by_mode.items())
    print(f"tasks: " + ", ".join(f"{s}={sum(1 for t in tasks if t.set == s)}" for s in args.sets) + f"   repeat: {args.repeat}")
    for m, n in by_mode.items():
        print(f"  {m:<18} {n:>5} runs")
    print(f"total: {runs} runs")
    print(f"model calls, estimated at {args.pinch_calls} per pinch run and {args.mini_calls} per mini-swe-agent run (assumptions, not measurements): about {est:,.0f}")
    print(f"model calls, upper bound at --max-steps {args.max_steps}: {runs * args.max_steps:,}")
    print(f"tokens, if an average call carries {args.tokens_per_call:,} tokens (input + output): about {est * args.tokens_per_call / 1e6:,.1f} million")
    return 0


def preflight(args, modes: list[Mode], tasks: list[Task], opts: Options) -> None:
    from ..config import load_trusted_env_files
    if not docker_ok():
        raise SystemExit("bench: Docker is not running or not reachable.")
    if not opts.mock:
        load_trusted_env_files()  # a .env in the pinch checkout, as for the CLI
        # Benchmark rows must never switch model silently, so a real run uses PINCH_BENCH_KEY (a key with no fallback chain) when
        # it is set. The containers get it under the name PINCH_GATEWAY_KEY, which is what both agents read.
        if os.environ.get("PINCH_BENCH_KEY"):
            os.environ["PINCH_GATEWAY_KEY"] = os.environ["PINCH_BENCH_KEY"]
        if not os.environ.get("PINCH_GATEWAY_KEY"):
            raise SystemExit("bench: neither PINCH_BENCH_KEY nor PINCH_GATEWAY_KEY is set (a Bifrost virtual key; see .env.example). "
                             "To check the harness without keys, run: python -m pinch.bench smoke")
    ensure_image()
    if not (Path(__file__).parent / "container").is_dir() or not tool_versions():
        raise SystemExit("bench: the tool venvs are missing. Run: python -m pinch.bench setup")
    if any(m.name in ("docs-on",) for m in modes) and not opts.mock and not os.environ.get("TAVILY_API_KEY"):
        raise SystemExit("bench: docs-on needs TAVILY_API_KEY (docs-off does not).")
    if args.budget_usd is not None:
        from ..ledger import estimate_cost
        if estimate_cost(opts.model, 1000, 1000, 0) is None:
            raise SystemExit(f"bench: --budget-usd needs a price for {opts.model!r} in src/pinch/prices.toml (or ~/.config/pinch/prices.toml).")
    if (opts.small_model or opts.big_model) and not (opts.small_model and opts.big_model):
        raise SystemExit("bench: --small-model and --big-model go together.")
    if any(m.name == "no-cascade" for m in modes) and not (opts.small_model and opts.big_model):
        print("bench: no-cascade is skipped: the cascade is off without --small-model and --big-model.", file=sys.stderr)


def cmd_run(args) -> int:
    try:
        modes = resolve_modes(args.modes)
    except ModeError as e:
        raise SystemExit(f"bench: {e}")
    if not (args.small_model and args.big_model):
        modes = [m for m in modes if m.name != "no-cascade"]
    opts = Options(model=args.model, base_url=args.base_url, max_steps=args.max_steps, max_tokens=args.max_tokens,
                   agent_timeout=args.timeout, tool_mode=args.tool_mode, jobs=args.jobs, delay=args.delay,
                   infra_retries=args.infra_retries, rate_limit_sleep=args.rate_limit_sleep, budget_usd=args.budget_usd,
                   small_model=args.small_model, big_model=args.big_model, artifacts=not args.no_artifacts,
                   min_call_interval=args.min_call_interval,
                   keep_workdirs=args.keep_workdirs, mock=getattr(args, "mock", False), tavily_url=getattr(args, "tavily_url", ""))
    return execute(args, modes, opts)


def execute(args, modes: list[Mode], opts: Options) -> int:
    tasks = load_tasks(args.sets, only=args.only or None, limit=args.limit, subset=getattr(args, "subset", None))
    if not tasks:
        raise SystemExit("bench: no tasks match.")
    preflight(args, modes, tasks, opts)
    out: Path = args.out
    existing = read_rows(out)
    if existing and not args.resume:
        raise SystemExit(f"bench: {out} already has {len(existing)} rows. Use --resume to continue it, or choose another --out.")
    prior_tag = next((r["cache_tag"] for r in existing if r.get("type") == "meta" and r.get("cache_tag")), "")
    opts.tag = prior_tag or uuid.uuid4().hex[:8]
    units = build_units(tasks, modes, args.repeat)
    writer = Writer(out)
    writer.write({"type": "meta", "ts": round(time.time(), 1), "mock": opts.mock, "model": opts.model, "base_url": opts.base_url,
                  "sets": args.sets, "modes": [m.name for m in modes], "repeat": args.repeat, "max_steps": opts.max_steps,
                  "max_tokens": opts.max_tokens, "agent_timeout": opts.agent_timeout, "min_call_interval": opts.min_call_interval, "tool_mode": opts.tool_mode,
                  "image": IMAGE, "tools": tool_versions(), "pinned": PINNED, "cache_tag": opts.tag,
                  "small_model": opts.small_model, "big_model": opts.big_model,
                  "task_counts": {s: sum(1 for t in tasks if t.set == s) for s in args.sets},
                  "auto_approve": "pinch runs use --auto-approve; each approval is logged in the session (auto_approved events)",
                  "command": " ".join(sys.argv[1:])[:400]})
    if opts.mock:
        print("*** MOCK RUN: scripted model responses, not real model results ***", file=sys.stderr)
    code = Run(units, opts, writer, existing).go()
    print(f"\nRows are in {out}. Summarise with: python -m pinch.bench summarize {out}", file=sys.stderr)
    return code
