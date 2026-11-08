"""Token-saving techniques. Each one can be switched on or off by itself.

    pinch --savings all                     every technique
    pinch --savings none                    none (the baseline for comparisons)
    pinch --savings filememory,trim         only these

The list on for a session is written to its session file, so a benchmark can tell runs apart.
"""

from __future__ import annotations

TECHNIQUES: dict[str, str] = {
    "context": "compact repo map and the most relevant files in the first prompt",
    "filememory": "re-reading an unchanged file returns a one-line stub; a changed file returns a diff",
    "smalledits": "steer the model to edit_file/apply_patch instead of rewriting whole files",
    "testcompress": "test and command output cut to failing tests, first errors and the summary",
    "trim": "old tool output becomes a one-line stub after a few steps (latest file versions kept)",
    "pretest": "run the tests once before the first model call; the compressed result goes in the first prompt and a red result counts as the first red",
    "stopongreen": "after an approved edit pinch proposes running the tests; stop only if they failed earlier and pass now",
    "cascade": "start on the small model tier, escalate to the big tier on clear failure signals",
    "prefix": "system prompt, style and tool schemas byte-identical and first on every call",
    "cache": "exact-match response cache in Bifrost, in a per-run namespace",
    "docs": "Tavily docs_lookup tool (needs TAVILY_API_KEY)",
}


def parse_savings(spec: str | list[str] | tuple[str, ...]) -> frozenset[str]:
    """'all', 'none', or a comma list of technique names -> the set that is on."""
    if isinstance(spec, str):
        spec = [part.strip() for part in spec.split(",") if part.strip()]
    names = [s.lower() for s in spec]
    if names in (["all"], []):
        return frozenset(TECHNIQUES) if names == ["all"] else frozenset()
    if names == ["none"]:
        return frozenset()
    unknown = [n for n in names if n not in TECHNIQUES]
    if unknown:
        raise ValueError(f"unknown technique(s): {', '.join(unknown)}. Choose from: all, none, {', '.join(TECHNIQUES)}")
    return frozenset(names)
