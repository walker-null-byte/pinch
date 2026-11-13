"""Summary: tokens per solved task, ablation shares, pairing, exclusions, the MOCK label, the CSV."""

import csv
import json

import pytest

from pinch.bench.summary import build, summarize_file


def row(mode, task, passed, tokens, repeat=0, set_="A", status="ok", mock=False, **kw):
    r = {"type": "run", "set": set_, "task": f"{set_}/{task}", "mode": mode, "agent": "mini" if mode == "baseline" else "pinch", "repeat": repeat,
         "phase": 1, "model": "m", "status": status, "passed": passed, "input_tokens": int(tokens * 0.9), "output_tokens": tokens - int(tokens * 0.9),
         "cached_tokens": 0, "total_tokens": tokens, "calls": 4, "usage_complete": True, "cost_usd": tokens * 1e-6, "agent_s": 10.0,
         "mock": mock, "auto_approvals": 3, "tavily_calls": 0, "tavily_credits": 0, "cache_hits": 0, "cache_saved_tokens": 0}
    r.update(kw)
    return r


def write(path, rows, **meta):
    lines = [{"type": "meta", "mock": False, "task_counts": {"A": 2}, "tools": {"mini-swe-agent": "2.4.6", "litellm": "x"}, **meta}] + rows
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")


def test_tokens_per_solved_counts_the_tokens_of_failed_runs(tmp_path):
    rows = [row("baseline", "a", True, 1000), row("baseline", "b", False, 3000),
            row("all", "a", True, 400), row("all", "b", True, 600)]
    f = tmp_path / "r.jsonl"
    write(f, rows)
    text, csv_rows = build(f)
    base = next(r for r in csv_rows if r["table"] == "main" and r["mode"] == "baseline")
    allm = next(r for r in csv_rows if r["table"] == "main" and r["mode"] == "all")
    assert base["tokens_per_solved"] == 4000 and base["pass_rate"] == 0.5 and base["tokens_mean"] == 2000 and base["tokens_median"] == 2000
    assert allm["tokens_per_solved"] == 500 and allm["cost_per_solved"] == pytest.approx(0.0005)
    assert "-88%" in text                                            # 500 vs 4000 per solved task


def test_infra_rows_are_kept_but_left_out_and_a_later_retry_wins(tmp_path):
    rows = [row("all", "a", None, 0, status="infra_error", infra_reason="rate_limit"),
            row("all", "a", True, 500),                                  # the retry of the same run finished
            row("all", "b", None, 0, status="infra_error", infra_reason="quota")]
    f = tmp_path / "r.jsonl"
    write(f, rows)
    text, csv_rows = build(f)
    main = next(r for r in csv_rows if r["table"] == "main")
    assert main["n"] == 1 and main["solved"] == 1
    assert "| rate_limit | 1 |" in text and "| quota | 1 |" in text and "left out of every number: 2" in text


def test_the_main_table_compares_modes_on_the_same_tasks_only(tmp_path):
    """A partial run: the baseline finished tasks a, b and c, pinch only a and b. Task c must not count for the baseline."""
    rows = [row("baseline", "a", True, 1000), row("baseline", "b", True, 1000), row("baseline", "c", False, 90000),
            row("all", "a", True, 500), row("all", "b", True, 500)]
    f = tmp_path / "r.jsonl"
    write(f, rows)
    text, csv_rows = build(f)
    base = next(r for r in csv_rows if r["table"] == "main" and r["mode"] == "baseline")
    assert base["n"] == 2 and base["tokens_per_solved"] == 1000
    assert "-50%" in text and "1 other run of set A is left out of this table" in text


def test_ablation_share_of_savings_uses_paired_tasks(tmp_path):
    rows = []
    for t in "ab":
        rows += [row("none", t, True, 1000), row("all", t, True, 400), row("no-trim", t, True, 700), row("no-context", t, True, 450)]
    rows.append(row("no-trim", "c", True, 99999))                       # not run in the other modes: must not distort the pairs
    f = tmp_path / "r.jsonl"
    write(f, rows)
    text, csv_rows = build(f)
    abl = {r["mode"]: r for r in csv_rows if r["table"] == "ablation"}
    assert abl["no-trim"]["extra_tokens_without"] == 300 and abl["no-trim"]["share_of_savings"] == pytest.approx(0.5)     # 300 of the 600 saved
    assert abl["no-context"]["share_of_savings"] == pytest.approx(50 / 600)
    assert abl["no-trim"]["n"] == 2


def test_ablation_on_tasks_every_mode_solved_leaves_out_pass_fail_flips(tmp_path):
    """Task b: `all` fails after spending a lot, `no-trim` passes. Per solved task that makes no-trim look better; on the one task every
    mode solved (a), no-trim costs more."""
    rows = [row("none", "a", True, 1000), row("all", "a", True, 400), row("no-trim", "a", True, 800),
            row("none", "b", True, 1000), row("all", "b", False, 9000), row("no-trim", "b", True, 900)]
    f = tmp_path / "r.jsonl"
    write(f, rows)
    text, csv_rows = build(f)
    solved = {r["mode"]: r for r in csv_rows if r["table"] == "ablation_solved_by_all"}
    assert solved["all"]["n"] == 1 and solved["all"]["tokens_total"] == 400
    assert solved["no-trim"]["tokens_total"] == 800 and solved["none"]["tokens_total"] == 1000
    assert "only the 1 task every mode above solved" in text and "| no-trim | 800 | 4.0 | +100% |" in text


def test_style_and_docs_and_cache_twice_tables(tmp_path):
    rows = [row("all", "a", True, 500, output_tokens=100), row("style-tiny", "a", True, 450, output_tokens=40),
            row("docs-on", "x", True, 800, set_="C", tavily_calls=2, tavily_credits=2), row("docs-off", "x", False, 900, set_="C"),
            row("cache-twice", "a", True, 500), row("cache-twice", "a", True, 0, phase=2, cache_hits=4, cache_saved_tokens=500)]
    f = tmp_path / "r.jsonl"
    write(f, rows)
    text, _ = build(f)
    assert "## Response styles" in text and "tiny" in text and "brief (default)" in text
    assert "## Tavily docs lookup on and off" in text and "2 calls / 2 credits" in text
    assert "## Same task twice" in text and "NOT part of the averages" in text and "| run 2 (same task again) | 1 | 0 | 4.0 | 4 | 500 |" in text
    main_rows = text.split("## Response styles")[0]
    assert "cache-twice" not in main_rows.split("## Same task twice")[0]       # the cache demo never enters the main averages


def test_mock_is_labelled_everywhere_and_in_the_csv(tmp_path):
    f = tmp_path / "m.jsonl"
    write(f, [row("all", "a", True, 500, mock=True)], mock=True)
    md, csv_path = summarize_file(f)
    text = md.read_text()
    assert "MOCK DATA" in text and text.count("(MOCK)") >= 3 and "# Benchmark report (MOCK)" in text
    with csv_path.open() as fh:
        assert all(r["mock"] == "MOCK" for r in csv.DictReader(fh))


def test_methodology_states_the_rules(tmp_path):
    f = tmp_path / "r.jsonl"
    write(f, [row("all", "a", True, 500)])
    text, _ = build(f)
    for needle in ("--auto-approve", "auto_approved", "provider reported", "fresh container", "infra_error", "written by us", "internet access"):
        assert needle in text
    assert "9 in the 1 pinch runs" not in text and "3 in the 1 pinch runs" in text       # the approval count is real, from the rows


def test_runs_without_usage_count_for_pass_rate_but_not_tokens(tmp_path):
    rows = [row("all", "a", True, 500), row("all", "b", True, 0, usage_complete=False)]
    f = tmp_path / "r.jsonl"
    write(f, rows)
    _, csv_rows = build(f)
    m = csv_rows[0]
    assert m["n"] == 2 and m["pass_rate"] == 1.0 and m["token_runs"] == 1 and m["tokens_mean"] == 500


def test_trim_versus_prompt_cache_reports_real_cached_shares(tmp_path):
    rows = []
    for t in "ab":
        rows += [row("none", t, True, 1000), row("all", t, True, 400, cached_tokens=300), row("no-trim", t, True, 500, cached_tokens=420),
                 row("no-prefix", t, True, 800, cached_tokens=0)]  # a row from an older run: its mode is not known any more and is ignored by this section
    f = tmp_path / "r.jsonl"
    write(f, rows)
    text, _ = build(f)
    assert "Trim versus the provider's prompt cache" in text and "default is left as is" in text
    assert "cached share 93%" in text and "cached input share" in text
    assert "no-prefix" not in text.split("Trim versus the provider's prompt cache")[1].split("##")[0]


def test_trim_section_needs_no_prefix_mode_and_old_techniques_on_rows_load(tmp_path):
    rows = []
    for t in "ab":
        rows += [row("none", t, True, 1000), row("all", t, True, 400, cached_tokens=300), row("no-trim", t, True, 500, cached_tokens=420)]
    rows[0]["techniques_on"] = ["trim", "prefix"]
    f = tmp_path / "r.jsonl"
    write(f, rows)
    text, _ = build(f)
    assert "Trim versus the provider's prompt cache" in text
