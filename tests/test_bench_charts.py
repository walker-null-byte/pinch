"""Charts: numbers match summary.stats, accent/diverging colour mapping, exclusions, degenerate inputs, light + dark files."""

import json
import re
import xml.etree.ElementTree as ET

import pytest

from pinch.bench.charts import PALETTES, build_charts, compact, write_charts
from pinch.bench.summary import load, stats


def row(mode, task, passed, tokens, repeat=0, set_="A", status="ok", **kw):
    r = {"type": "run", "set": set_, "task": f"{set_}/{task}", "mode": mode, "agent": "mini" if mode == "baseline" else "pinch", "repeat": repeat,
         "phase": 1, "model": "m-1", "status": status, "passed": passed, "input_tokens": int(tokens * 0.9), "output_tokens": tokens - int(tokens * 0.9),
         "cached_tokens": 0, "total_tokens": tokens, "calls": 4, "usage_complete": True, "cost_usd": tokens * 1e-6, "agent_s": 10.0}
    r.update(kw)
    return r


def write(path, rows):
    lines = [{"type": "meta", "mock": False, "task_counts": {"A": 2}, "tools": {}}] + rows
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    return path


def core_rows():
    return [row("baseline", "a", True, 100_000), row("baseline", "b", True, 120_000),
            row("none", "a", True, 90_000), row("none", "b", False, 130_000),
            row("all", "a", True, 60_000), row("all", "b", True, 70_000)]


def ablation_rows():
    rows = []
    for t in "abcd":
        rows += [row("all", t, True, 100_000), row("no-trim", t, t != "d", 150_000), row("no-context", t, True, 80_000)]
    return rows


def svg(p):
    return p.read_text()


def test_core_chart_values_match_stats_and_all_is_the_accent(tmp_path):
    f = write(tmp_path / "r.jsonl", core_rows())
    out = tmp_path / "out"
    write_charts(f, out)
    _, counted, _ = load(f)
    text = svg(out / "core-A-light.svg")
    ET.fromstring(text)  # well-formed
    by_mode = {m: stats([r for r in counted if r["mode"] == m]) for m in ("baseline", "none", "all")}
    for m, expect in (("baseline", "110K"), ("none", "220K"), ("all", "65K")):
        assert compact(by_mode[m]["tokens_per_solved"]) == expect
    assert "110K" in text and "220K" in text and "65K" in text and "+100%" in text
    assert "−41%" in text                       # all vs baseline: 65K / 110K - 1
    assert "1/2 solved" in text and "2/2 solved" in text
    colour = {m: c for c, m in re.findall(r'<path d="[^"]+" fill="(#[0-9a-f]+)" data-series="(\w+)"', text)}
    assert colour["all"] == PALETTES["light"]["blue"]
    assert colour["baseline"] == colour["none"] == PALETTES["light"]["gray"]
    assert "Tokens per solved task, set A (lower is better)" in text and "m-1 · 2 tasks, 1 run each" in text
    assert "mini-swe-agent (baseline)" in text and "pinch, no techniques" in text and "pinch, all techniques" in text
    dark = svg(out / "core-A-dark.svg")
    assert PALETTES["dark"]["blue"] in dark and PALETTES["dark"]["surface"] in dark and PALETTES["light"]["blue"] not in dark


def test_text_never_uses_a_series_colour(tmp_path):
    f = write(tmp_path / "r.jsonl", core_rows() + ablation_rows())
    write_charts(f, tmp_path / "out")
    for p in (tmp_path / "out").glob("*.svg"):
        for theme in PALETTES:
            if f"-{theme}" in p.name:
                c = PALETTES[theme]
                for m in re.finditer(r"<text[^>]*>.*?</text>", p.read_text()):
                    for series in (c["blue"], c["red"], c["gray"]):
                        assert series not in m.group(0)


def test_bars_round_only_the_data_end(tmp_path):
    f = write(tmp_path / "r.jsonl", core_rows())
    write_charts(f, tmp_path / "out")
    text = svg(tmp_path / "out" / "core-A-light.svg")
    paths = re.findall(r'<path d="([^"]+)"', text)
    assert paths and all(p.count("A") == 2 and "rx" not in p for p in paths)
    assert "<rect" in text and text.count("<rect") == 1      # only the background


def test_ablation_diverging_colours_sort_and_labels(tmp_path):
    f = write(tmp_path / "r.jsonl", ablation_rows())
    out = tmp_path / "out"
    write_charts(f, out)
    text = svg(out / "ablation-A-light.svg")
    # no-trim: 600K tokens / 3 solved = 200K vs all 400K / 4 = 100K -> +100%; no-context: 320K / 4 = 80K -> -20%
    series = re.findall(r'fill="(#[0-9a-f]+)" data-series="(\w+)" data-value="([-\d.e+]+)"', text)
    got = {s: (c, float(v)) for c, s, v in series}
    assert got["trim"] == (PALETTES["light"]["blue"], pytest.approx(100.0))
    assert got["context"] == (PALETTES["light"]["red"], pytest.approx(-20.0))
    assert text.index('data-series="trim"') < text.index('data-series="context"')      # sorted descending
    assert "+100%" in text and "−20%" in text and "1 fewer solved" in text
    assert "1 run each" in text and "small differences are noise" in text and "4 paired tasks" in text
    assert "What each technique saves: tokens per solved task with it switched off" in text
    dark = svg(out / "ablation-A-dark.svg")
    assert PALETTES["dark"]["blue"] in dark and PALETTES["dark"]["red"] in dark


def test_ablation_uses_only_tasks_every_mode_finished_and_combines_sets(tmp_path):
    rows = ablation_rows() + [row("no-trim", "extra", True, 9_999_999),
                              row("all", "z", True, 100_000, set_="B"), row("no-trim", "z", True, 100_000, set_="B"), row("no-context", "z", True, 100_000, set_="B")]
    f = write(tmp_path / "r.jsonl", rows)
    charts = {c.name: c for c in build_charts(f)}
    abl = charts["ablation-all"]
    assert abl.values["n_tasks"] == 5 and "sets A, B combined" in abl.subtitle


def test_infra_error_rows_are_excluded(tmp_path):
    rows = core_rows() + [row("all", "c", None, 9_000_000, status="infra_error"), row("baseline", "c", None, 9_000_000, status="infra_error")]
    f = write(tmp_path / "r.jsonl", rows)
    charts = {c.name: c for c in build_charts(f)}
    v = charts["core-A"].values
    assert v["all"]["n"] == 2 and v["baseline"]["n"] == 2
    assert v["all"]["value"] == 65_000
    assert "9.0M" not in "".join(c.desc for c in charts.values())


def test_a_set_without_baseline_or_ablation_does_not_crash(tmp_path):
    f = write(tmp_path / "r.jsonl", [row("all", "a", True, 1000), row("none", "a", True, 2000),
                                     row("baseline", "q", True, 1000, set_="B")])           # set B: baseline only, no pinch mode
    out = tmp_path / "out"
    written = write_charts(f, out)
    assert [p.name for p in written] == ["charts.md"] and (out / "charts.md").read_text() == ""
    f2 = write(tmp_path / "r2.jsonl", [row("baseline", "a", False, 1000), row("all", "a", False, 800)])   # nothing solved
    write_charts(f2, tmp_path / "out2")
    assert not (tmp_path / "out2" / "core-A-light.svg").exists()                        # no baseline value to compare with
    f3 = write(tmp_path / "r3.jsonl", [row("baseline", "a", True, 1000), row("all", "a", False, 800)])      # pinch solved nothing
    write_charts(f3, tmp_path / "out3")
    assert "none solved" in svg(tmp_path / "out3" / "core-A-light.svg")


def test_light_and_dark_files_and_snippet(tmp_path):
    f = write(tmp_path / "r.jsonl", core_rows() + ablation_rows()[3:])
    out = tmp_path / "out"
    written = {p.name for p in write_charts(f, out)}
    for name in ("core-A", "cost-A"):
        assert f"{name}-light.svg" in written and f"{name}-dark.svg" in written
    md = (out / "charts.md").read_text()
    assert '<picture><source media="(prefers-color-scheme: dark)" srcset="docs/charts/core-A-dark.svg">' in md
    assert 'src="docs/charts/core-A-light.svg" width="720"' in md
    text = svg(out / "core-A-light.svg")
    assert "<title" in text and "<desc" in text and f'fill="{PALETTES["light"]["surface"]}"' in text


def test_cli_registered(tmp_path):
    from pinch.bench.cli import main
    f = write(tmp_path / "r.jsonl", core_rows())
    assert main(["charts", str(f), "--out", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "core-A-dark.svg").exists()
