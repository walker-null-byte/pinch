"""Static SVG charts from a results file, in a light and a dark variant each.

    uv run python -m pinch.bench charts bench/results/run1.jsonl [--out docs/charts]

Every number comes from the same code as the markdown report (`summary.load`, `summary.stats`, `summary.paired`, `summary.rel`),
with the same exclusions: infra_error rows, the cache-twice demonstration and phase 2 are left out.

Charts written (each as `<name>-light.svg` and `<name>-dark.svg`), plus `charts.md` with GitHub <picture> markup:
- core-<set>      tokens per solved task per mode (baseline, none, all): emphasis bars, `all` in the accent colour
- cost-<set>      the same with cost per solved task, when prices are known for every run
- ablation-<set>  % change in tokens per solved task when one technique is switched off, relative to `all`, on the tasks every
                  mode finished (diverging bars; `all` is the set name when several sets contribute)

The SVGs are hand-written (stdlib only). Text is always in an ink colour, never in a series colour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .modes import ABLATABLE
from .summary import group_by_mode, load, main_rows, mode_order, paired, rel, stats

FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

PALETTES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
              "blue": "#2a78d6", "red": "#e34948", "gray": "#b4b2a9"},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
             "blue": "#3987e5", "red": "#e66767", "gray": "#5f5e5a"},
}

CATEGORY = {"baseline": "mini-swe-agent (baseline)", "none": "pinch, no techniques", "all": "pinch, all techniques"}
MINUS = "−"

WIDTH = 720
PAD = 24            # outer left/right padding
BAR_H = 20
RADIUS = 4
TITLE_PX, LABEL_PX, TICK_PX = 16, 13, 11
GAP = 8             # space between the parts of a tip label
CHAR_W = 0.6        # conservative average glyph width, as a fraction of the font size


# -- small helpers --------------------------------------------------------------------------------

def text_w(s: str, px: float) -> float:
    return len(s) * px * CHAR_W


def signed(s: str) -> str:
    return s.replace("-", MINUS)


def compact(x: float) -> str:
    """87600 -> 87.6K, 123456 -> 123K, 1.2e6 -> 1.2M, 950 -> 950."""
    a = abs(x)
    if a >= 1e6:
        out = f"{x / 1e6:.1f}M"
    elif a >= 1e5:
        out = f"{x / 1e3:.0f}K"
    elif a >= 1e3:
        out = f"{x / 1e3:.1f}K"
    else:
        out = f"{x:.0f}"
    return out.replace(".0M", "M").replace(".0K", "K")


def usd(x: float) -> str:
    return f"${x:.3f}" if x < 1 else f"${x:.2f}"


def nice_step(span: float, target: int = 4) -> float:
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    """Round tick values covering [lo, hi]; always includes 0 when the range does."""
    if hi <= lo:
        hi = lo + 1
    step = nice_step(hi - lo, target)
    first = math.floor(lo / step + 1e-9) * step
    last = math.ceil(hi / step - 1e-9) * step
    n = int(round((last - first) / step))
    return [round(first + i * step, 10) for i in range(n + 1)]


def wrap(text: str, px: float, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if cur and text_w(trial, px) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + [cur] if cur else lines


def bar_path(x0: float, x1: float, y: float, h: float = BAR_H, r: float = RADIUS) -> str:
    """A horizontal bar from the baseline x0 to the data end x1: square at x0, rounded corners at x1 only."""
    length = abs(x1 - x0)
    if length < 0.5:
        return ""
    r = min(r, length / 2, h / 2)
    d = 1 if x1 >= x0 else -1
    return (f"M{x0:.2f},{y:.2f} L{x1 - d * r:.2f},{y:.2f} A{r:.2f},{r:.2f} 0 0 {1 if d > 0 else 0} {x1:.2f},{y + r:.2f} "
            f"L{x1:.2f},{y + h - r:.2f} A{r:.2f},{r:.2f} 0 0 {1 if d > 0 else 0} {x1 - d * r:.2f},{y + h:.2f} L{x0:.2f},{y + h:.2f} Z")


# -- chart model ----------------------------------------------------------------------------------

@dataclass
class Row:
    key: str                         # mode / technique id
    label: str                       # category label
    sub: str                         # secondary line under the label ("20/22 solved"), may be empty
    value: float | None              # the measure; None = nothing solved
    text: list[tuple[str, str]]      # tip label as (text, ink) parts: ink is "ink" or "ink2"
    color: str                       # palette key for the bar: gray / blue / red


@dataclass
class Chart:
    name: str
    title: str
    subtitle: str
    desc: str
    rows: list[Row]
    kind: str                        # "emphasis" or "diverging"
    tick_fmt: callable
    values: dict                     # raw numbers, for tests and the description


def render(chart: Chart, theme: str) -> str:
    c = PALETTES[theme]
    diverging = chart.kind == "diverging"
    two_line = any(r.sub for r in chart.rows)
    pitch = 48 if two_line else 38

    width = max(WIDTH, int(text_w(chart.title, TITLE_PX) + 2 * PAD) + 1)
    title_lines = [chart.title]
    sub_lines = wrap(chart.subtitle, LABEL_PX, width - 2 * PAD)
    y = 24 + TITLE_PX
    title_y = y
    sub_y0 = title_y + 22
    top = sub_y0 + 20 * (len(sub_lines) - 1) + 28

    label_w = max(max(text_w(r.label, LABEL_PX), text_w(r.sub, TICK_PX + 1)) for r in chart.rows)
    x_plot0 = PAD + label_w + 20
    x_plot1 = width - PAD

    vals = [r.value for r in chart.rows if r.value is not None]
    lo = min([0.0] + vals)
    hi = max([0.0] + vals)
    ticks = nice_ticks(lo, hi)
    lo, hi = ticks[0], ticks[-1]

    # room for tip labels beyond the bar ends, so labels never leave the plot area or touch the category column
    def tip_w(r: Row) -> float:
        return sum(text_w(t.strip(), LABEL_PX) for t, _ in r.text) + GAP * (len(r.text) - 1) + 10
    pos_pad = max([tip_w(r) for r in chart.rows if r.value is None or r.value >= 0] + [0])
    neg_pad = max([tip_w(r) for r in chart.rows if r.value is not None and r.value < 0] + [0])
    avail = x_plot1 - x_plot0 - pos_pad - neg_pad
    scale = avail / (hi - lo)
    zero_x = x_plot0 + neg_pad + (0 - lo) * scale

    def X(v: float) -> float:
        return zero_x + v * scale

    plot_top = top
    plot_bottom = top + pitch * len(chart.rows)
    tick_y = plot_bottom + 18
    height = tick_y + 22

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
         f'aria-labelledby="t d" font-family="{FONT}">',
         f"<title id=\"t\">{escape(chart.title)}</title>", f"<desc id=\"d\">{escape(chart.desc)}</desc>",
         f'<rect width="{width}" height="{height}" fill="{c["surface"]}"/>',
         f'<text x="{PAD}" y="{title_y}" font-size="{TITLE_PX}" font-weight="600" fill="{c["ink"]}">{escape(title_lines[0])}</text>']
    for i, line in enumerate(sub_lines):
        o.append(f'<text x="{PAD}" y="{sub_y0 + 20 * i}" font-size="{LABEL_PX}" fill="{c["ink2"]}">{escape(line)}</text>')

    # gridlines and tick labels (hairline, solid, recessive); the zero line is the baseline
    for t in ticks:
        x = X(t)
        is_zero = abs(t) < 1e-12
        o.append(f'<line x1="{x:.2f}" y1="{plot_top - 6}" x2="{x:.2f}" y2="{plot_bottom + 2}" stroke="{c["axis"] if is_zero else c["grid"]}" stroke-width="1"/>')
        o.append(f'<text x="{x:.2f}" y="{tick_y}" font-size="{TICK_PX}" text-anchor="middle" fill="{c["muted"]}" '
                 f'style="font-variant-numeric: tabular-nums" font-feature-settings="\'tnum\'">{escape(chart.tick_fmt(t))}</text>')

    for i, r in enumerate(chart.rows):
        cy = plot_top + pitch * i + pitch / 2
        by = cy - BAR_H / 2
        if r.sub:
            o.append(f'<text x="{PAD}" y="{cy - 2:.2f}" font-size="{LABEL_PX}" fill="{c["ink"]}">{escape(r.label)}</text>')
            o.append(f'<text x="{PAD}" y="{cy + 14:.2f}" font-size="{TICK_PX + 1}" fill="{c["ink2"]}">{escape(r.sub)}</text>')
        else:
            o.append(f'<text x="{PAD}" y="{cy + 4.5:.2f}" font-size="{LABEL_PX}" fill="{c["ink"]}">{escape(r.label)}</text>')
        if r.value is None:
            tip_x, anchor = zero_x + 8, "start"
        else:
            end = X(r.value)
            d = bar_path(zero_x, end, by)
            if d:
                o.append(f'<path d="{d}" fill="{c[r.color]}" data-series="{escape(r.key)}" data-value="{r.value:.6g}"/>')
            if r.value < 0:
                tip_x, anchor = end - 8, "end"
            else:
                tip_x, anchor = max(end, zero_x) + 8, "start"
        # a surface-coloured halo keeps a gridline that crosses the label from striking through the text
        spans = "".join(f'<tspan fill="{c[ink]}"{f" dx={chr(34)}{GAP}{chr(34)}" if j else ""}>{escape(t.strip())}</tspan>' for j, (t, ink) in enumerate(r.text))
        o.append(f'<text x="{tip_x:.2f}" y="{cy + 4.5:.2f}" font-size="{LABEL_PX}" text-anchor="{anchor}" data-label="{escape(r.key)}" '
                 f'stroke="{c["surface"]}" stroke-width="4" stroke-linejoin="round" paint-order="stroke">{spans}</text>')
    o.append("</svg>")
    return "\n".join(o) + "\n"


# -- building the charts from rows ----------------------------------------------------------------

def _models(rows: list[dict]) -> str:
    return ", ".join(sorted({r["model"] for r in rows}))


def _runs_each(rows: list[dict]) -> str:
    n = len({r["repeat"] for r in rows})
    return f"{n} run{'s' if n != 1 else ''} each"


def emphasis_chart(set_: str, rows_s: list[dict], measure: str) -> Chart | None:
    """measure: 'tokens' or 'cost'. None when the set has no baseline, no pinch mode, or no value for the baseline."""
    by_mode = group_by_mode(rows_s)
    modes = [m for m in mode_order(set(by_mode)) if m in CATEGORY]
    if "baseline" not in modes or len(modes) < 2:
        return None
    key = "tokens_per_solved" if measure == "tokens" else "cost_per_solved"
    st = {m: stats(by_mode[m]) for m in modes}
    base = st["baseline"][key]
    if base is None:
        return None
    fmt = compact if measure == "tokens" else usd
    rows, values = [], {}
    for m in modes:
        v = st[m][key]
        values[m] = {"value": v, "solved": st[m]["solved"], "n": st[m]["n"], "pct": None if m == "baseline" or v is None else 100 * (v - base) / base}
        sub = f"{st[m]['solved']}/{st[m]['n']} solved"
        if v is None:
            text = [("none solved", "ink2")]
        else:
            text = [(fmt(v), "ink")]
            if m != "baseline":
                d = signed(rel(v, base)).replace("+0%", "0%")
                text.append((d, "ink2"))
        rows.append(Row(m, CATEGORY[m], sub, v, text, "blue" if m == "all" else "gray"))
    what, unit = ("Tokens", "tokens") if measure == "tokens" else ("Cost", "cost")
    title = f"{what} per solved task, set {set_} (lower is better)" if measure == "tokens" else f"Cost per solved task, set {set_} (lower is better)"
    ntasks = len({r["task"] for r in rows_s})
    subtitle = f"{_models(rows_s)} · {ntasks} tasks, {_runs_each(rows_s)}"
    parts = [f"{CATEGORY[m]} {fmt(values[m]['value']) if values[m]['value'] is not None else 'no solves'} ({values[m]['solved']}/{values[m]['n']} solved)" for m in modes]
    desc = f"{title}. {'; '.join(parts)}. {subtitle}."
    return Chart(f"{'core' if measure == 'tokens' else 'cost'}-{set_}", title, subtitle, desc, rows, "emphasis",
                 (lambda t: compact(t)) if measure == "tokens" else _usd_tick(rows), values)


def _usd_tick(rows):
    vals = [r.value for r in rows if r.value is not None]
    step = nice_step(max(vals) or 1)
    dec = 0 if step >= 1 else min(4, max(0, -math.floor(math.log10(step))))
    return lambda t: f"${t:.{dec}f}"


def ablation_chart(counted: list[dict]) -> Chart | None:
    present = {r["mode"] for r in counted if r.get("phase", 1) == 1}
    abl = [f"no-{t}" for t in ABLATABLE if f"no-{t}" in present]
    if "all" not in present or not abl:
        return None
    groups = paired(counted, ["all"] + abl)
    if not groups.get("all"):
        return None
    st = {m: stats(g) for m, g in groups.items()}
    ref = st["all"]["tokens_per_solved"]
    if ref is None:
        return None
    rows, values = [], {}
    for m in abl:
        if m not in st:
            continue
        v = st[m]["tokens_per_solved"]
        tech = m[3:]
        diff = st[m]["solved"] - st["all"]["solved"]
        if v is None:
            rows.append(Row(tech, tech, "", None, [("none solved", "ink2")], "blue"))
            values[tech] = {"pct": None, "solved_diff": diff}
            continue
        pct = 100 * (v - ref) / ref
        text = [(signed(rel(v, ref)).replace("+0%", "0%"), "ink")]
        if diff:
            text.append((f" · {abs(diff)} {'more' if diff > 0 else 'fewer'} solved", "ink2"))
        rows.append(Row(tech, tech, "", pct, text, "blue" if pct >= 0 else "red"))
        values[tech] = {"pct": pct, "solved_diff": diff, "tokens_per_solved": v}
    if not rows:
        return None
    rows.sort(key=lambda r: (r.value is None, -(r.value or 0)))
    tasks = {r["task"] for r in groups["all"]}
    sets = sorted({t.split("/")[0] for t in tasks})
    where = f"set {sets[0]}" if len(sets) == 1 else f"sets {', '.join(sets)} combined"
    all_rows = [r for g in groups.values() for r in g]
    each = _runs_each(groups["all"])
    tail = f"{each} — small differences are noise" if each == "1 run each" else each
    subtitle = f"{_models(all_rows)} · {len(tasks)} paired tasks, {where} · {tail}"
    title = "What each technique saves: tokens per solved task with it switched off"
    desc = (f"{title}, relative to all techniques on ({compact(ref)} tokens per solved task). "
            + "; ".join(f"{r.key} {signed(f'{r.value:+.0f}%') if r.value is not None else 'no solves'}" for r in rows) + f". {subtitle}.")
    name = f"ablation-{sets[0] if len(sets) == 1 else 'all'}"
    return Chart(name, title, subtitle, desc, rows, "diverging", lambda t: signed(f"{t:+.0f}%").replace("+0%", "0%"),
                 {"all_tokens_per_solved": ref, "techniques": values, "n_tasks": len(tasks)})


def build_charts(path: Path) -> list[Chart]:
    _, counted, _ = load(path)
    charts: list[Chart] = []
    for s in sorted({r["set"] for r in counted}):
        rows_s = main_rows(counted, s)[0]
        for measure in ("tokens", "cost"):
            ch = emphasis_chart(s, rows_s, measure) if rows_s else None
            if ch:
                charts.append(ch)
    abl = ablation_chart(counted)
    if abl:
        charts.append(abl)
    return charts


def picture(name: str, alt: str, prefix: str = "docs/charts") -> str:
    return (f'<picture><source media="(prefers-color-scheme: dark)" srcset="{prefix}/{name}-dark.svg">'
            f'<img alt="{escape(alt, {chr(34): "&quot;"})}" src="{prefix}/{name}-light.svg" width="720"></picture>')


def write_charts(src: Path, out: Path) -> list[Path]:
    charts = build_charts(src)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    snippets = []
    for ch in charts:
        for theme in ("light", "dark"):
            p = out / f"{ch.name}-{theme}.svg"
            p.write_text(render(ch, theme), encoding="utf-8")
            written.append(p)
        snippets.append(picture(ch.name, ch.desc))
    md = out / "charts.md"
    md.write_text("\n\n".join(snippets) + "\n" if snippets else "", encoding="utf-8")
    written.append(md)
    return written


def cmd_charts(args) -> int:
    if not args.file.exists():
        raise SystemExit(f"bench: {args.file} not found")
    written = write_charts(args.file, args.out)
    if len(written) == 1:
        print("no charts: the file has no set with a baseline plus a pinch mode, and no ablation modes")
    for p in written:
        print(f"wrote {p}")
    return 0


def add_charts_parser(sub) -> None:
    s = sub.add_parser("charts", help="write light and dark SVG charts and a charts.md snippet from a results file")
    s.add_argument("file", type=Path)
    s.add_argument("--out", type=Path, default=Path("docs/charts"), help="output directory (default: docs/charts)")
    s.set_defaults(fn=cmd_charts)
