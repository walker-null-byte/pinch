"""Turn a results file into a markdown report and a CSV.

    uv run python -m pinch.bench summarize bench/results/run1.jsonl          # writes run1.md and run1.csv next to it

Rules the report follows (and prints in its methodology section):
- A run counts once. If a run was repeated (a --resume after an infrastructure failure), the latest finished row is used.
- Rows marked infra_error are kept in the file but excluded from every number; they are counted in the "Excluded" table.
- Token statistics use only runs where every model reply carried a `usage` block (usage_complete). Pass rates use every counted run.
- "Tokens per solved task" = all tokens spent by the runs in the group (solved or not) / number of runs that passed.
  Failed runs cost tokens too, and this is the number a user pays per working result.
- Ablation, style and docs tables compare modes on the SAME tasks and repeats only (paired), so a partial run stays fair.
- Cost is provider-reported tokens times the price table (src/pinch/prices.toml), the same table for both agents.
"""

from __future__ import annotations

import csv as csvlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .modes import ABLATABLE
from .runner import read_rows, row_key

MAIN_ORDER = ["baseline", "none", "all"]
STYLE_MODES = ["all", "style-normal", "style-tiny", "style-caveman"]
STYLE_NAMES = {"all": "brief (default)", "style-normal": "normal", "style-tiny": "tiny", "style-caveman": "caveman"}


# -- loading ------------------------------------------------------------------------------------

def load(path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """(meta rows, counted run rows, excluded run rows)."""
    rows = read_rows(path)
    metas = [r for r in rows if r.get("type") == "meta"]
    runs = [r for r in rows if r.get("type") == "run"]
    latest: dict[tuple, dict] = {}
    for r in runs:
        if r.get("status") == "infra_error":
            continue
        latest[row_key(r)] = r  # later rows win
    counted = list(latest.values())
    excluded = [r for r in runs if r.get("status") == "infra_error"]
    return metas, counted, excluded


# -- statistics ---------------------------------------------------------------------------------

def mean(xs):
    return sum(xs) / len(xs) if xs else None


def median(xs):
    return statistics.median(xs) if xs else None


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    solved = sum(1 for r in rows if r.get("passed"))
    tok_rows = [r for r in rows if r.get("usage_complete", True)]
    tokens = [r["total_tokens"] for r in tok_rows]
    cost_rows = [r for r in tok_rows if r.get("cost_usd") is not None]
    out = {
        "n": n, "solved": solved, "pass_rate": solved / n if n else None,
        "tokens_mean": mean(tokens), "tokens_median": median(tokens),
        "input_mean": mean([r["input_tokens"] for r in tok_rows]), "output_mean": mean([r["output_tokens"] for r in tok_rows]),
        "cached_mean": mean([r["cached_tokens"] for r in tok_rows]),
        "calls_mean": mean([r["calls"] for r in rows]), "time_median_s": median([active_s(r) for r in rows if "agent_s" in r]),
        "time_mean_s": mean([active_s(r) for r in rows if "agent_s" in r]),
        "token_runs": len(tok_rows),
    }
    solved_in_tok = sum(1 for r in tok_rows if r.get("passed"))
    out["tokens_per_solved"] = sum(tokens) / solved_in_tok if solved_in_tok else None
    solved_in_cost = sum(1 for r in cost_rows if r.get("passed"))
    out["cost_per_solved"] = sum(r["cost_usd"] for r in cost_rows) / solved_in_cost if solved_in_cost and len(cost_rows) == len(tok_rows) else None
    out["cost_mean"] = mean([r["cost_usd"] for r in cost_rows]) if len(cost_rows) == len(tok_rows) and cost_rows else None
    out["cached_share"] = (sum(r["cached_tokens"] for r in tok_rows) / sum(r["input_tokens"] for r in tok_rows)
                           if tok_rows and sum(r["input_tokens"] for r in tok_rows) else None)
    return out


def fmt(x, kind="int"):
    if x is None:
        return "n/a"
    if kind == "pct":
        return f"{100 * x:.0f}%"
    if kind == "usd":
        return f"${x:.4f}"
    if kind == "f1":
        return f"{x:.1f}"
    if kind == "s":
        return f"{x:.0f}s"
    return f"{x:,.0f}"


def rel(x, ref):
    if x is None or ref in (None, 0):
        return "n/a"
    return f"{100 * (x - ref) / ref:+.0f}%"


def table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def paired(rows: list[dict], modes: list[str]) -> dict[str, list[dict]]:
    """Per mode, only the (task, repeat) combinations that finished in every one of `modes`."""
    by_mode = {m: {(r["task"], r["repeat"]): r for r in rows if r["mode"] == m and r.get("phase", 1) == 1} for m in modes}
    present = [m for m in modes if by_mode[m]]
    if len(present) < 2:
        return {m: list(by_mode[m].values()) for m in present}
    common = set.intersection(*(set(by_mode[m]) for m in present))
    return {m: [by_mode[m][k] for k in sorted(common)] for m in present}


def mode_order(modes: set[str]) -> list[str]:
    fixed = [m for m in MAIN_ORDER if m in modes]
    abl = [f"no-{t}" for t in ABLATABLE if f"no-{t}" in modes]
    rest = sorted(modes - set(fixed) - set(abl))
    return fixed + abl + rest


# -- the report ---------------------------------------------------------------------------------

def build(path: Path) -> tuple[str, list[dict]]:
    metas, counted, excluded = load(path)
    mock = any(m.get("mock") for m in metas) or any(r.get("mock") for r in counted + excluded)
    tag = " (MOCK)" if mock else ""
    csv_rows: list[dict] = []
    md: list[str] = []
    title = f"Benchmark report{tag}: {path.name}"
    md.append(f"# {title}\n")
    if mock:
        md.append("> **MOCK DATA.** Every number below comes from a scripted stand-in for a model (`pinch.bench.mock`), served through the real "
                  "Bifrost gateway. It proves the harness records, scores and summarises runs correctly. It says NOTHING about how well pinch or "
                  "mini-swe-agent perform with a real model.\n")
    if not counted:
        md.append("No finished runs in this file.\n")
        return "\n".join(md), csv_rows
    sets = sorted({r["set"] for r in counted})
    models = sorted({r["model"] for r in counted})
    md.append(f"Model: {', '.join(f'`{m}`' for m in models)} · counted runs: {len(counted)} · excluded (infrastructure): {len(excluded)} · "
              f"sets: {', '.join(sets)}\n")

    # 1. main table per set
    md.append(f"## Results per set and mode{tag}\n")
    md.append("Tokens are provider-reported `usage` (input + output) through Bifrost. *Tokens per solved task* = all tokens the group spent, "
              "failed runs included, divided by the runs that passed. *vs baseline* compares that figure with mini-swe-agent's.\n")
    for s in sets:
        rows_s = [r for r in counted if r["set"] == s and r.get("phase", 1) == 1 and r["mode"] != "cache-twice"]
        if not rows_s:
            continue
        by_mode = defaultdict(list)
        for r in rows_s:
            by_mode[r["mode"]].append(r)
        base_tps = stats(by_mode["baseline"])["tokens_per_solved"] if "baseline" in by_mode else None
        lines = []
        for m in mode_order(set(by_mode)):
            st = stats(by_mode[m])
            lines.append([m, st["n"], f"{st['solved']} ({fmt(st['pass_rate'], 'pct')})", fmt(st["tokens_mean"]), fmt(st["tokens_median"]),
                          fmt(st["tokens_per_solved"]), rel(st["tokens_per_solved"], base_tps) if m != "baseline" else "-",
                          fmt(st["cost_per_solved"], "usd"), fmt(st["calls_mean"], "f1"), fmt(st["time_median_s"], "s")])
            csv_rows.append({"table": "main", "set": s, "mode": m, **{k: st[k] for k in (
                "n", "solved", "pass_rate", "tokens_mean", "tokens_median", "input_mean", "output_mean", "cached_mean",
                "tokens_per_solved", "cost_per_solved", "cost_mean", "calls_mean", "time_median_s", "token_runs")}})
        task_count = len({r["task"] for r in rows_s})
        md.append(f"### Set {s}: {task_count} task{'s' if task_count != 1 else ''}{tag}\n")
        md.append(table(["mode", "runs", "solved", "avg tokens", "median tokens", "tokens / solved", "vs baseline", "cost / solved",
                         "avg calls", "median time (no waits)"], lines) + "\n")

    # 2. ablation
    abl_modes = [m for m in ["none", "all"] + [f"no-{t}" for t in ABLATABLE] if any(r["mode"] == m for r in counted)]
    if "all" in abl_modes and len(abl_modes) > 2:
        md.append(f"## Ablation: what each technique contributes{tag}\n")
        md.append("`all` minus one technique, compared with `all` on the same tasks. *Extra tokens without it* = tokens per solved task of "
                  "`no-<technique>` minus that of `all`. *Share of savings* = that extra divided by (`none` minus `all`): how much of the total "
                  "saving the technique accounts for. Negative = the technique cost tokens here. Shares of techniques that overlap do not add up "
                  "to 100%. A technique that changes nothing in a run (for example `cache` when no request repeats) shows ~0.\n")
        for s in sets:
            rows_s = [r for r in counted if r["set"] == s]
            groups = paired(rows_s, abl_modes)
            if "all" not in groups or "none" not in groups or not groups["all"]:
                continue
            st = {m: stats(g) for m, g in groups.items()}
            span = (st["none"]["tokens_per_solved"] or 0) - (st["all"]["tokens_per_solved"] or 0) \
                if st["none"]["tokens_per_solved"] and st["all"]["tokens_per_solved"] else None
            lines = [["none (no techniques)", st["none"]["n"], fmt(st["none"]["pass_rate"], "pct"), fmt(st["none"]["tokens_per_solved"]), "", "",
                      fmt(st["none"]["cached_share"], "pct")],
                     ["all", st["all"]["n"], fmt(st["all"]["pass_rate"], "pct"), fmt(st["all"]["tokens_per_solved"]), "", "",
                      fmt(st["all"]["cached_share"], "pct")]]
            for m in abl_modes:
                if m in ("none", "all") or m not in st:
                    continue
                extra = (st[m]["tokens_per_solved"] - st["all"]["tokens_per_solved"]) \
                    if st[m]["tokens_per_solved"] is not None and st["all"]["tokens_per_solved"] is not None else None
                share = extra / span if extra is not None and span else None
                lines.append([m, st[m]["n"], fmt(st[m]["pass_rate"], "pct"), fmt(st[m]["tokens_per_solved"]),
                              fmt(extra) if extra is None else f"{extra:+,.0f}", fmt(share, "pct"), fmt(st[m]["cached_share"], "pct")])
                csv_rows.append({"table": "ablation", "set": s, "mode": m, "n": st[m]["n"], "pass_rate": st[m]["pass_rate"],
                                 "tokens_per_solved": st[m]["tokens_per_solved"], "extra_tokens_without": extra, "share_of_savings": share})
            md.append(f"### Set {s} ({st['all']['n']} paired runs per mode){tag}\n")
            md.append(table(["mode", "runs", "pass rate", "tokens / solved", "extra tokens without it", "share of savings",
                             "cached input share"], lines) + "\n")
            if "no-trim" in st and "no-prefix" in st:
                md.append(trim_vs_prefix(s, st))

    # 3. styles
    style_present = [m for m in STYLE_MODES if any(r["mode"] == m for r in counted)]
    if len(style_present) > 1:
        md.append(f"## Response styles (all techniques on){tag}\n")
        for s in sets:
            groups = paired([r for r in counted if r["set"] == s], style_present)
            if len(groups) < 2 or not any(groups.values()):
                continue
            lines = []
            for m in style_present:
                if m not in groups:
                    continue
                st = stats(groups[m])
                lines.append([STYLE_NAMES[m], st["n"], fmt(st["pass_rate"], "pct"), fmt(st["output_mean"]), fmt(st["tokens_mean"]), fmt(st["tokens_per_solved"])])
                csv_rows.append({"table": "style", "set": s, "mode": m, "n": st["n"], "pass_rate": st["pass_rate"], "output_mean": st["output_mean"],
                                 "tokens_mean": st["tokens_mean"], "tokens_per_solved": st["tokens_per_solved"]})
            md.append(f"### Set {s}{tag}\n")
            md.append(table(["style", "runs", "pass rate", "avg output tokens", "avg tokens", "tokens / solved"], lines) + "\n")

    # 4. docs on/off
    if any(r["mode"] in ("docs-on", "docs-off") for r in counted):
        md.append(f"## Tavily docs lookup on and off (set C){tag}\n")
        groups = paired([r for r in counted if r["set"] == "C"], ["docs-on", "docs-off"])
        lines = []
        for m in ("docs-on", "docs-off"):
            if m in groups and groups[m]:
                st = stats(groups[m])
                calls = sum(r.get("tavily_calls", 0) for r in groups[m])
                credits = sum(r.get("tavily_credits", 0) for r in groups[m])
                lines.append([m, st["n"], fmt(st["pass_rate"], "pct"), fmt(st["tokens_mean"]), fmt(st["tokens_per_solved"]), fmt(st["calls_mean"], "f1"),
                              f"{calls} calls / {credits} credits"])
                csv_rows.append({"table": "docs", "set": "C", "mode": m, "n": st["n"], "pass_rate": st["pass_rate"],
                                 "tokens_mean": st["tokens_mean"], "tokens_per_solved": st["tokens_per_solved"], "tavily_calls": calls, "tavily_credits": credits})
        md.append(table(["mode", "runs", "pass rate", "avg tokens", "tokens / solved", "avg model calls", "Tavily use"], lines) + "\n")
        md.append("The Tavily tool is only offered on set C. These tasks use library features released in 2026; see `bench/tasks/C/SOURCE.md`.\n")

    # 5. cache-twice
    twice = [r for r in counted if r["mode"] == "cache-twice"]
    if twice:
        md.append(f"## Same task twice, in one cache namespace{tag}\n")
        md.append("A separate demonstration, NOT part of the averages above: each task is run twice in a row with the same Bifrost cache namespace, "
                  "so the second run can be answered from the cache. Calls after a tool result that differs between the two runs (a test run prints its own timing) "
                  "miss and are billed, so the second run is cheaper, not free. The real runs above use a fresh namespace per run and never share answers.\n")
        lines = []
        for phase in (1, 2):
            rs = [r for r in twice if r.get("phase", 1) == phase]
            if rs:
                lines.append([f"run {phase}" + (" (cold)" if phase == 1 else " (same task again)"), len(rs), fmt(mean([r['total_tokens'] for r in rs])),
                              fmt(mean([r['calls'] for r in rs]), "f1"), fmt(sum(r['cache_hits'] for r in rs)), fmt(sum(r['cache_saved_tokens'] for r in rs)),
                              fmt(mean([active_s(r) for r in rs]), "s")])
                csv_rows.append({"table": "cache-twice", "set": "*", "mode": f"cache-twice#{phase}", "n": len(rs),
                                 "tokens_mean": mean([r["total_tokens"] for r in rs]), "cache_hits": sum(r["cache_hits"] for r in rs),
                                 "cache_saved_tokens": sum(r["cache_saved_tokens"] for r in rs)})
        md.append(table(["", "runs", "avg billed tokens", "avg calls", "cache hits", "tokens not billed", "avg time"], lines) + "\n")

    # 6. excluded
    md.append(f"## Excluded runs{tag}\n")
    if excluded:
        reasons = Counter((r.get("infra_reason") or "?") for r in excluded)
        md.append("These runs failed on infrastructure, not on the task. They stay in the results file, marked `infra_error`, and are in no average. "
                  "A later retry of the same run, if it finished, is counted instead.\n")
        md.append(table(["reason", "runs"], [[k, v] for k, v in reasons.most_common()]) + "\n")
    else:
        md.append("None.\n")
    unfinished_usage = [r for r in counted if not r.get("usage_complete", True)]
    if unfinished_usage:
        md.append(f"{len(unfinished_usage)} counted runs had a model reply without a `usage` block; they count for pass rate but not for token statistics.\n")

    md.append(methodology(metas, counted, excluded, mock))
    return "\n".join(md), csv_rows


def trim_vs_prefix(set_: str, st: dict) -> str:
    """Trimming old tool output shortens the history but changes the request prefix; a stable prefix lets the provider's prompt cache work.
    The two pull in opposite directions, so the data decides: this prints what each did on the same tasks, with real cached-token counts."""
    a, t, p = st["all"], st["no-trim"], st["no-prefix"]
    def move(x, ref):
        return rel(x, ref) if x is not None and ref else "n/a"
    out = [f"**Trim versus stable prefix (set {set_}).** Provider-reported cached input tokens decide this; the default is left as is.\n",
           f"- trim on, stable prefix on (`all`): {fmt(a['tokens_per_solved'])} tokens per solved task, {fmt(a['cached_share'], 'pct')} of input tokens served from the provider's prompt cache",
           f"- trim off (`no-trim`): {fmt(t['tokens_per_solved'])} ({move(t['tokens_per_solved'], a['tokens_per_solved'])}), cached share {fmt(t['cached_share'], 'pct')}",
           f"- prefix off (`no-prefix`): {fmt(p['tokens_per_solved'])} ({move(p['tokens_per_solved'], a['tokens_per_solved'])}), cached share {fmt(p['cached_share'], 'pct')}",
           "- Cached tokens are cheaper than fresh ones, so compare cost per solved task as well as tokens: see the main table. "
           "If the provider reports no cached tokens at all (cached share 0%), the data cannot favour either side.\n"]
    return "\n".join(out) + "\n"


def active_s(row: dict) -> float:
    """Agent wall time without the seconds spent in pacing and rate-limit waits (rows from before waits were recorded have none)."""
    return max(float(row.get("agent_s") or 0) - float(row.get("wait_seconds") or 0), 0.0)


def methodology(metas: list[dict], counted: list[dict], excluded: list[dict], mock: bool) -> str:
    meta = metas[0] if metas else {}
    approvals = sum(r.get("auto_approvals") or 0 for r in counted + excluded)
    pinch_runs = sum(1 for r in counted + excluded if r["agent"] == "pinch")
    counts = meta.get("task_counts", {})
    tools = meta.get("tools", {})
    lines = ["## Methodology\n"]
    L = lines.append
    L(f"- **Models.** Both agents use the same model through the same Bifrost gateway: {', '.join('`%s`' % m for m in sorted({r['model'] for r in counted}))}"
      + (" (a scripted mock model: MOCK)" if mock else "") + f". Gateway: `{meta.get('base_url', '?')}`; virtual-key header `x-bf-vk`. "
      f"Cascade tiers: {meta.get('small_model') or 'off'} / {meta.get('big_model') or 'off'}.")
    served = sorted({m for r in counted + excluded for m in (r.get("served_models") or []) if m})
    fallbacks = sum(r.get("fallbacks") or 0 for r in counted + excluded)
    L(f"- **Who answered.** Models the gateway reported as serving the calls: {', '.join('`%s`' % m for m in served) or 'not reported'}. "
      f"Calls answered by a fallback provider (recorded per call for pinch; for mini-swe-agent only when its response carries it): {fallbacks}. "
      "The benchmark key has no fallback chain, so a different model should never answer silently.")
    waits = sum(r.get("wait_seconds") or 0 for r in counted + excluded)
    rl = sum(r.get("rate_limit_waits") or 0 for r in counted + excluded)
    L(f"- **Pacing and rate limits.** Both agents are paced the same way: at least {meta.get('min_call_interval', 'n/a')} s between model requests inside each agent process "
      "(`--min-call-interval`; pinch through `PINCH_MIN_CALL_INTERVAL`, mini-swe-agent through a wrapper around its model call that changes nothing else), and both wait out a per-minute "
      "HTTP 429 (the provider's \"retry in N s\", at most 120 s, up to 8 times) instead of failing. Only a per-day limit stops a run. "
      f"This report: {rl} rate-limit waits, {waits:,.0f} s spent waiting in total. **Times in the tables exclude those waits.** "
      "Pacing is per process, so with `--jobs` above 1 the runs share one limit and the pacing no longer keeps each agent under it.")
    L(f"- **Agents.** pinch (this repository, `--auto-approve`) versus mini-swe-agent `{tools.get('mini-swe-agent', '?')}` (MIT, SWE-agent team) with litellm "
      f"`{tools.get('litellm', '?')}`, its default tool-calling config (`mini.yaml`), no changes except the connection. Each run: at most {meta.get('max_steps', '?')} model calls, "
      f"{meta.get('max_tokens', '?')} output tokens per call, {meta.get('agent_timeout', '?')} s wall time, for both agents. pinch tool mode: `{meta.get('tool_mode', '?')}`.")
    def n(s):
        return f"{counts[s]} used in this run" if s in counts else "not part of this run"
    L(f"- **Tasks.** Set A: Python exercises from Aider's polyglot benchmark ({n('A')}; Exercism, MIT; pinned commit 7e0611e, `bench/tasks/A/SOURCE.md`). "
      f"Set B: real upstream bug fixes ({n('B')}; repository, fix commit and licence per task in `bench/tasks/B/SOURCE.md`; the starting tree is the commit before the fix, the fix's tests are the hidden check). "
      f"Set C: docs tasks **written by us** ({n('C')}; `bench/tasks/C/SOURCE.md`).")
    L("- **How pass is checked.** Not by the agent. After an agent stops, the harness restores the exercise's tests (A), or adds the upstream fix's tests the agent never saw (B), "
      "or adds our check script (C), deletes cached bytecode, and runs the check in a fresh container. Pass = exit code 0.")
    L("- **Sandbox.** Every agent run and every check happens in a throwaway container of the pinned image "
      f"`{meta.get('image', 'python:3.12.15-slim-bookworm')}` with the task folder mounted and host networking (needed to reach the gateway). "
      "Model-generated commands never run on the host. Limitation: the container has internet access, so an agent could in principle fetch information from the web.")
    L(f"- **Auto-approval.** pinch ran with `--auto-approve`, so writes, shell commands and git operations were approved without a person. "
      f"Every one is logged in the run's session file (`auto_approved` events): {approvals} in the {pinch_runs} pinch runs in this report. "
      "mini-swe-agent has no approval gate in this configuration. This is a benchmark-only setting; pinch's normal mode asks first.")
    L("- **Tokens and cost.** Token counts are the `usage` the provider reported, relayed by Bifrost; nothing is estimated from text length. "
      "Cost = those tokens times `src/pinch/prices.toml`, the same table for both agents. A model without a price shows `n/a`.")
    L("- **Cache.** Real runs use a fresh Bifrost cache namespace each, so there are no cross-run cache hits. The 'same task twice' table is a separate demonstration.")
    L("- **Docs tool.** The Tavily `docs_lookup` tool is offered only on set C; on sets A and B `all` means every other technique (and `cascade` only when --small-model and --big-model are given). "
      "The techniques actually on for each run are recorded in its row (`techniques_on`).")
    L("- **Excluded.** Runs that failed on infrastructure (rate limit that did not clear, outage, exhausted quota or budget, bad credentials, harness error) are kept in the file "
      f"with `status = infra_error` and left out of every number: {len(excluded)} in this report. Rate limits are waited out and retried before a run is given up.")
    L("- **Limits.** One sample per cell unless `--repeat` was raised; models are not deterministic, so small differences are noise. "
      "`none` keeps pinch's default style (brief); the style table measures style on its own.")
    return "\n".join(lines) + "\n"


CSV_COLUMNS = ["table", "set", "mode", "n", "solved", "pass_rate", "tokens_mean", "tokens_median", "input_mean", "output_mean", "cached_mean",
               "tokens_per_solved", "cost_per_solved", "cost_mean", "calls_mean", "time_median_s", "token_runs", "extra_tokens_without",
               "share_of_savings", "tavily_calls", "tavily_credits", "cache_hits", "cache_saved_tokens"]


def summarize_file(src: Path, md_out: Path | None = None, csv_out: Path | None = None) -> tuple[Path, Path]:
    md_out = md_out or src.with_suffix(".md")
    csv_out = csv_out or src.with_suffix(".csv")
    text, rows = build(src)
    md_out.write_text(text, encoding="utf-8")
    metas, counted, excluded = load(src)
    mock = any(m.get("mock") for m in metas)
    with csv_out.open("w", newline="", encoding="utf-8") as f:
        w = csvlib.DictWriter(f, fieldnames=CSV_COLUMNS + ["mock"], extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "mock": "MOCK" if mock else ""})
    return md_out, csv_out


def cmd_summarize(args) -> int:
    if not args.file.exists():
        raise SystemExit(f"bench: {args.file} not found")
    md, csv = summarize_file(args.file, args.md, args.csv)
    print(f"wrote {md} and {csv}")
    if args.print:
        print(md.read_text())
    return 0


def add_summarize_parser(sub) -> None:
    s = sub.add_parser("summarize", help="write a markdown report and a CSV from a results file")
    s.add_argument("file", type=Path)
    s.add_argument("--md", type=Path, help="markdown output (default: next to the input)")
    s.add_argument("--csv", type=Path, help="CSV output (default: next to the input)")
    s.add_argument("--print", action="store_true", help="also print the report")
    s.set_defaults(fn=cmd_summarize)
