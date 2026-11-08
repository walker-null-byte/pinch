"""Modes, the run loop, --resume, rate-limit handling and clean stops. The agents are replaced by a fake: no Docker needed."""

import json
from types import SimpleNamespace

import pytest

from pinch.bench import runner, sandbox
from pinch.bench.modes import ABLATABLE, ModeError, parse_mode, resolve_modes
from pinch.bench.runner import Options, Run, Writer, build_units, done_keys, read_rows
from pinch.bench.tasks import load_tasks
from pinch.savings import TECHNIQUES


def test_mode_names_and_macros():
    assert [m.name for m in resolve_modes(["core"])] == ["baseline", "none", "all"]
    names = [m.name for m in resolve_modes(["ablate"])]
    assert names[:2] == ["none", "all"] and names[2:] == [f"no-{t}" for t in ABLATABLE if t != "cache"]
    assert "no-docs" not in names and "no-cache" not in names and "no-cache" not in [m.name for m in resolve_modes(["everything"])]
    assert "no-pretest" in names and parse_mode("no-pretest").removed == "pretest"
    assert "pretest" not in parse_mode("no-pretest").savings("A", False).split(",") and "pretest" in parse_mode("all").savings("A", False).split(",")
    assert parse_mode("none").savings("A", False) == "none"
    assert parse_mode("no-cache").removed == "cache"                                  # still available by name
    assert [m.name for m in resolve_modes(["styles"])] == ["all", "style-normal", "style-tiny", "style-caveman"]
    assert [m.name for m in resolve_modes(["all", "core"])] == ["all", "baseline", "none"]                    # no duplicates
    assert resolve_modes(["baseline"])[0].agent == "mini" and resolve_modes(["all"])[0].agent == "pinch"
    for bad in ("no-bogus", "style-loud", "nonsense", "no-docs"):
        with pytest.raises(ModeError):
            parse_mode(bad)


def test_savings_flags_per_mode_and_set():
    all_, none = parse_mode("all"), parse_mode("none")
    assert none.savings("A", False) == "none"
    on_a = set(all_.savings("A", False).split(","))
    assert on_a == set(TECHNIQUES) - {"docs", "cascade"}                      # docs only on set C; cascade only with two models
    assert set(all_.savings("C", False).split(",")) == set(TECHNIQUES) - {"cascade"}
    assert "cascade" in all_.savings("A", True)
    assert set(parse_mode("no-trim").savings("A", False).split(",")) == on_a - {"trim"}
    assert "docs" not in parse_mode("docs-off").savings("C", False) and "docs" in parse_mode("docs-on").savings("C", False)
    assert parse_mode("docs-on").sets == ("C",)
    assert parse_mode("style-tiny").style == "tiny" and parse_mode("all").style == "brief"
    assert "cache" in parse_mode("cache-twice").savings("A", False)
    assert parse_mode("baseline").savings("A", False) == ""


def test_units_cover_repeat_task_mode_and_skip_inapplicable_pairs():
    tasks = load_tasks(["A", "C"], limit=2)
    modes = resolve_modes(["baseline", "docs-on", "cache-twice"])
    units = build_units(tasks, modes, 2)
    assert len(units) == 2 * (4 * 2 + 2)                                       # docs-on only for the 2 C tasks; baseline and cache-twice for all 4
    assert [u.repeat for u in units][:1] == [0] and {u.phases for u in units if u.mode.twice} == {(1, 2)}


class FakeRuns:
    """Stands in for run_once: scripted rows by call order."""

    def __init__(self, script=None):
        self.script, self.calls = script or {}, []

    def __call__(self, task, mode, repeat, phase, attempt, opts, cache_key=""):
        self.calls.append((task.id, mode.name, repeat, phase, attempt))
        kind = self.script.get((task.slug, mode.name, attempt), "ok")
        row = {"type": "run", "ts": 0, "run_id": f"r{len(self.calls)}", "mock": False, "set": task.set, "task": task.id, "mode": mode.name,
               "agent": mode.agent, "repeat": repeat, "phase": phase, "attempt": attempt, "model": "m", "status": "ok", "infra_reason": None,
               "passed": True, "total_tokens": 100, "input_tokens": 90, "output_tokens": 10, "cached_tokens": 0, "calls": 3,
               "usage_complete": True, "cost_usd": 0.5, "agent_s": 1.0, "stop": "done", "cache_cache_key": cache_key}
        if kind != "ok":
            row.update(status="infra_error", infra_reason=kind, passed=None, infra_detail="scripted", cost_usd=0.0)
        return row


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    def install(script=None):
        f = FakeRuns(script)
        monkeypatch.setattr(runner, "run_once", f)
        return f
    return install


def opts(**kw):
    return Options(model="m", rate_limit_sleep=0.0, **kw)


def run_all(tmp_path, units, o, existing=None):
    writer = Writer(tmp_path / "out.jsonl")
    r = Run(units, o, writer, existing or [], say=lambda s: None)
    r.sleep = lambda s: None
    code = r.go()
    return r, code, read_rows(tmp_path / "out.jsonl")


def test_every_run_becomes_a_row_and_resume_skips_what_is_done(tmp_path, fake):
    f = fake()
    tasks = load_tasks(["A"], limit=2)
    units = build_units(tasks, resolve_modes(["baseline", "all"]), 1)
    _, code, rows = run_all(tmp_path, units, opts())
    assert code == 0 and len(rows) == 4 and len(f.calls) == 4
    # a second invocation with --resume finds everything done
    f2 = fake()
    _, code2, rows2 = run_all(tmp_path, build_units(tasks, resolve_modes(["baseline", "all"]), 1), opts(), existing=rows)
    assert code2 == 0 and f2.calls == [] and len(rows2) == 4
    # a bigger request only runs the new cells
    f3 = fake()
    run_all(tmp_path, build_units(tasks, resolve_modes(["baseline", "all", "none"]), 1), opts(), existing=rows)
    assert sorted({c[1] for c in f3.calls}) == ["none"] and len(f3.calls) == 2


def test_resume_retries_infra_rows_but_keeps_them_in_the_file(tmp_path, fake):
    tasks = load_tasks(["A"], limit=1)
    units = build_units(tasks, resolve_modes(["all"]), 1)
    fake({(tasks[0].slug, "all", a): "provider_unavailable" for a in (1, 2, 3, 4)})
    _, _, rows = run_all(tmp_path, units, opts(infra_retries=1))          # gives up after 2 attempts
    assert [r["status"] for r in rows] == ["infra_error", "infra_error"] and done_keys(rows) == set()
    f = fake()
    _, _, rows2 = run_all(tmp_path, units, opts(), existing=rows)
    assert len(f.calls) == 1 and [r["status"] for r in rows2] == ["infra_error", "infra_error", "ok"]


def test_rate_limit_is_waited_out_and_the_run_continues(tmp_path, fake):
    f = fake({("two-bucket", "all", 1): "rate_limit", ("two-bucket", "all", 2): "rate_limit"})
    tasks = load_tasks(["A"], only=["two-bucket", "zipper"])
    r, code, rows = run_all(tmp_path, build_units(tasks, resolve_modes(["all"]), 1), opts(infra_retries=3))
    assert code == 0 and [(x["task"], x["attempt"], x["status"]) for x in rows] == [
        ("A/two-bucket", 1, "infra_error"), ("A/two-bucket", 2, "infra_error"), ("A/two-bucket", 3, "ok"), ("A/zipper", 1, "ok")]
    assert r.throttle.gap > 0                                                    # it slowed down after the 429s


@pytest.mark.parametrize("reason", ["quota", "budget", "config"])
def test_quota_budget_and_bad_credentials_stop_the_run_cleanly(tmp_path, fake, reason):
    tasks = load_tasks(["A"], only=["two-bucket", "zipper", "react"])
    fake({(tasks[0].slug, "all", 1): reason})
    r, code, rows = run_all(tmp_path, build_units(tasks, resolve_modes(["all"]), 1), opts())
    assert code == 3 and r.stop_reason and len(rows) == 1 and rows[0]["infra_reason"] == reason     # nothing runs after the failure
    assert done_keys(rows) == set()                                                                   # so --resume runs it again


def test_spending_cap_stops_before_the_next_run(tmp_path, fake):
    f = fake()
    tasks = load_tasks(["A"], limit=4)
    r, code, rows = run_all(tmp_path, build_units(tasks, resolve_modes(["all"]), 1), opts(budget_usd=1.0))   # each fake run costs $0.50
    assert code == 3 and len(rows) == 2 and "budget-usd" in r.stop_reason
    # money already spent in the file counts toward the cap on resume
    f2 = fake()
    r2, code2, rows2 = run_all(tmp_path, build_units(tasks, resolve_modes(["all"]), 1), opts(budget_usd=1.0), existing=rows)
    assert code2 == 3 and f2.calls == [] and len(rows2) == 2


def test_a_run_of_infra_failures_stops_the_whole_benchmark(tmp_path, fake):
    fake({(t.slug, "all", 1): "bad_request" for t in load_tasks(["A"])})
    r, code, rows = run_all(tmp_path, build_units(load_tasks(["A"]), resolve_modes(["all"]), 1), opts(max_consecutive_infra=3))
    assert code == 3 and len(rows) == 3 and "in a row" in r.stop_reason


def test_cache_twice_runs_both_phases_in_one_namespace(tmp_path, fake):
    f = fake()
    tasks = load_tasks(["A"], only=["two-bucket"])
    o = opts()
    o.tag = "abc"
    _, _, rows = run_all(tmp_path, build_units(tasks, resolve_modes(["cache-twice"]), 1), o)
    assert [(r["phase"]) for r in rows] == [1, 2] and len({r["cache_cache_key"] for r in rows}) == 1
    assert rows[0]["cache_cache_key"] == "bench-abc-two-bucket-r0"


def test_throttle_slows_after_limits_and_recovers():
    t = runner.Throttle(0.0)
    t.slower(); t.slower()
    assert t.gap == 10.0
    for _ in range(20):
        t.faster()
    assert t.gap == 0.0


def test_a_half_written_last_line_is_ignored(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({"type": "run", "set": "A", "task": "A/x", "mode": "all", "repeat": 0, "status": "ok"}) + '\n{"type": "ru')
    assert len(read_rows(p)) == 1


def test_run_command_refuses_to_overwrite_without_resume(tmp_path, monkeypatch):
    out = tmp_path / "x.jsonl"
    out.write_text(json.dumps({"type": "meta"}) + "\n")
    args = SimpleNamespace(sets=["A"], only=None, limit=1, out=out, resume=False, repeat=1, budget_usd=None)
    monkeypatch.setattr(runner, "preflight", lambda *a: None)
    with pytest.raises(SystemExit, match="--resume"):
        runner.execute(args, resolve_modes(["all"]), opts())


def test_task_env_puts_the_task_venv_first_and_source_folders_on_the_path():
    env = sandbox.task_env("py-123", ("src", "."))
    assert env["PATH"].startswith("/opt/tools/envs/py-123/bin:") and env["PYTHONPATH"] == "/work/src:/work"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert sandbox.env_name(("a", "b")) == sandbox.env_name(("b", "a"))


def test_secrets_are_passed_by_name_never_by_value(monkeypatch):
    seen = {}
    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setenv("PINCH_GATEWAY_KEY", "sk-bf-very-secret-value")
    sandbox.container_run(["true"], secrets=("PINCH_GATEWAY_KEY",))
    assert "PINCH_GATEWAY_KEY" in seen["cmd"] and "sk-bf-very-secret-value" not in " ".join(seen["cmd"])
    assert "--rm" in seen["cmd"] and "host" in seen["cmd"] and sandbox.IMAGE in seen["cmd"] and ":latest" not in sandbox.IMAGE


def test_a_docker_failure_is_never_taken_for_a_failed_check(monkeypatch):
    monkeypatch.setattr(sandbox.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=125, stdout="", stderr="docker: Error response from daemon: Conflict"))
    with pytest.raises(sandbox.SandboxError, match="Conflict"):
        sandbox.container_run(["true"])


def test_the_ablate15_subset_is_all_of_b_plus_seven_a_tasks():
    tasks = load_tasks(subset="ablate15")
    assert len(tasks) == 15 and sum(t.set == "B" for t in tasks) == 8 and sum(t.set == "A" for t in tasks) == 7 and not any(t.set == "C" for t in tasks)
    assert [t.slug for t in tasks if t.set == "A"] == sorted(["proverb", "dominoes", "list-ops", "wordy", "react", "forth", "paasio"])
    assert load_tasks(["A"], subset="ablate15") == [t for t in tasks if t.set == "A"]             # the set filter still applies
    with pytest.raises(Exception, match="unknown subset"):
        load_tasks(subset="nope")
