"""The modes a benchmark can run, and how a mode turns into pinch flags.

    baseline        mini-swe-agent (the open-source baseline)
    none            pinch --savings none
    all             pinch --savings all (default style: brief)
    no-<technique>  all minus one technique, for the ablation (no-context, no-filememory, ..., no-cache)
    style-<name>    all, with another response style (normal, tiny, caveman; brief is `all`)
    docs-on         set C only: all, with the Tavily docs tool on
    docs-off        set C only: all minus docs
    cache-twice     the same task run twice in one cache namespace, to show cache hits (clearly separate from the real runs)

Macros that expand to several modes: ablate (none, all and every no-<technique> except no-cache, which cannot show an effect with per-run namespaces), styles (all and the three other styles),
docs (docs-on, docs-off), core (baseline, none, all), everything.

Notes that the report repeats:
- On sets A and B the Tavily docs tool is never offered (it is only measured on set C), so `all` there means every other technique (pretest included).
- `cascade` needs --small-model and --big-model. Without both it is not part of `all`, and `no-cascade` is skipped.
- `none` keeps pinch's default style (brief), so `all` versus `none` isolates the techniques; the style table measures style by itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import STYLES
from ..savings import TECHNIQUES

ABLATABLE = [t for t in TECHNIQUES if t != "docs"]  # docs is measured on set C by docs-on / docs-off
# `no-cache` is left out of the macros: runs use fresh cache namespaces, so cache cannot show an effect; `cache-twice` is the cache evidence.
# It is still a valid mode if you ask for it by name.
MACRO_ABLATE = [t for t in ABLATABLE if t != "cache"]
MACROS = {
    "core": ["baseline", "none", "all"],
    "ablate": ["none", "all"] + [f"no-{t}" for t in MACRO_ABLATE],
    "styles": ["all"] + [f"style-{s}" for s in STYLES if s != "brief"],
    "docs": ["docs-on", "docs-off"],
}
MACROS["everything"] = ["baseline", "none", "all"] + [m for m in MACROS["ablate"] if m not in ("none", "all")] \
    + [m for m in MACROS["styles"] if m != "all"] + MACROS["docs"] + ["cache-twice"]


class ModeError(ValueError):
    pass


@dataclass(frozen=True)
class Mode:
    name: str
    agent: str  # "pinch" or "mini"
    style: str = "brief"
    sets: tuple[str, ...] = ("A", "B", "C")
    removed: str = ""  # the technique an ablation switches off
    twice: bool = False  # cache-twice

    def savings(self, set_: str, have_cascade: bool) -> str:
        """The value for pinch's --savings flag, for a task of `set_`."""
        if self.agent != "pinch":
            return ""
        if self.name == "none":
            return "none"
        on = [t for t in TECHNIQUES]
        if set_ != "C" or self.name == "docs-off":
            on.remove("docs")
        if not have_cascade:
            on.remove("cascade")
        if self.removed in on:
            on.remove(self.removed)
        if self.twice and "cache" not in on:
            on.append("cache")
        return ",".join(on)


def parse_mode(name: str) -> Mode:
    if name == "baseline":
        return Mode(name, "mini")
    if name in ("none", "all"):
        return Mode(name, "pinch")
    if name.startswith("no-"):
        tech = name[3:]
        if tech not in ABLATABLE:
            raise ModeError(f"unknown technique in {name!r}; the ablation covers: {', '.join(ABLATABLE)} (docs has docs-off for set C)")
        return Mode(name, "pinch", removed=tech)
    if name.startswith("style-"):
        style = name[6:]
        if style not in STYLES:
            raise ModeError(f"unknown style in {name!r}; choose from {', '.join(STYLES)}")
        return Mode(name, "pinch", style=style)
    if name in ("docs-on", "docs-off"):
        return Mode(name, "pinch", sets=("C",), removed="docs" if name == "docs-off" else "")
    if name == "cache-twice":
        return Mode(name, "pinch", twice=True)
    raise ModeError(f"unknown mode {name!r}. Modes: baseline, none, all, no-<technique>, style-<name>, docs-on, docs-off, cache-twice; "
                    f"macros: {', '.join(MACROS)}")


def resolve_modes(spec: list[str]) -> list[Mode]:
    names: list[str] = []
    for item in spec:
        for n in MACROS.get(item, [item]):
            if n not in names:
                names.append(n)
    return [parse_mode(n) for n in names]
