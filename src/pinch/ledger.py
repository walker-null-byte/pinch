"""Token ledger: one record per model call, stored as `ledger` lines in the session file.

Cost is an estimate from prices.toml (USD per million tokens). Unknown models cost None,
shown as '?', so a missing price never looks like a free call.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import config_dir
from .session import Session

PRICES_FILE = Path(__file__).parent / "prices.toml"


def load_prices() -> dict[str, dict]:
    prices: dict[str, dict] = {}
    for path in (PRICES_FILE, config_dir() / "prices.toml"):
        try:
            prices.update(tomllib.loads(path.read_text(encoding="utf-8")).get("models", {}))
        except (OSError, tomllib.TOMLDecodeError):
            continue
    return prices


def price_entry(model: str, prices: dict) -> dict:
    """The price for `model`, or the [default] entry (deliberately high) for budget estimates."""
    return prices.get(model) or prices.get(model.split("/", 1)[-1]) or prices["default"]


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int, prices: dict | None = None) -> float | None:
    prices = prices if prices is not None else load_prices()
    entry = prices.get(model) or prices.get(model.split("/", 1)[-1])
    if entry is None:
        return None
    fresh = max(input_tokens - cached_tokens, 0)
    cached_price = entry.get("cached", entry["input"])
    return (fresh * entry["input"] + cached_tokens * cached_price + output_tokens * entry["output"]) / 1_000_000


@dataclass
class LedgerRecord:
    step: int
    model: str  # the model we asked for
    served_model: str | None  # the model the response says answered
    provider: str | None  # who served it, if the gateway tells us
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float | None
    finish_reason: str | None = None
    latency_ms: int | None = None
    usage_reported: bool = True  # False if the response had no usage block
    is_fallback: bool = False  # the gateway answered from a fallback provider
    primary_provider: str | None = None  # who it tried first (set when is_fallback)
    tries: int = 1  # tries needed to reach the gateway (pinch retried when > 1)
    tier: str = "primary"  # "local" once pinch is on the local-only key
    model_tier: str = ""  # cascade: "small" or "big" ("" when the cascade is off)
    escalated: str = ""  # cascade: why this call is the first on the big tier ("" otherwise)
    cache_hit: bool = False  # answered from Bifrost's cache; tokens above are then 0 and the saving is below
    cache_saved_tokens: int = 0  # input+output tokens that would have been billed without the cache hit
    embedding_tokens: int = 0  # tokens spent embedding the request for the cache
    wait_seconds: float = 0.0  # seconds spent waiting before/within this call (pacing and rate-limit waits)
    rate_limit_waits: int = 0  # rate-limit waits this call went through

    def as_fields(self) -> dict:
        return asdict(self)


def summarize(session: Session) -> str:
    records = session.records()
    rows = [r for r in records if r["type"] == "ledger"]
    events = [r for r in records if r["type"] == "event"]
    if not rows:
        return f"Session {session.id}: no model calls recorded."

    def line(step, model, provider, i, o, c, cost) -> str:
        return f"{step:>5}  {model[:24]:<24} {provider[:12]:<12} {i:>8} {o:>8} {c:>8} {cost:>12}"

    techniques = [e["techniques"] for e in events if e["event"] == "savings"]
    out = [f"Session {session.id}", "techniques on: " + (", ".join(techniques[-1]) if techniques and techniques[-1] else "none"),
           line("step", "model", "provider", "input", "output", "cached", "cost (USD)")]
    for r in rows:
        cost = r.get("cost_usd")
        provider = (r.get("provider") or "-") + ("*" if r.get("is_fallback") else "") + ("(cache)" if r.get("cache_hit") else "")
        out.append(line(r["step"], r.get("served_model") or r["model"], provider, r["input_tokens"],
                        r["output_tokens"], r["cached_tokens"], "%.6f" % cost if cost is not None else "?"))
    known = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    total = "%.6f" % sum(known) + ("" if len(known) == len(rows) else "+?")
    total_in, total_cached = sum(r["input_tokens"] for r in rows), sum(r["cached_tokens"] for r in rows)
    out.append(line("total", f"{len(rows)} calls", "", total_in, sum(r["output_tokens"] for r in rows), total_cached, total))
    if total_in:
        out.append(f"cached input tokens (provider prompt cache): {total_cached} of {total_in} = {100 * total_cached / total_in:.0f}%")
    if any(r.get("is_fallback") for r in rows):
        out.append("* a fallback provider answered (the first-choice provider did not): " + ", ".join(
            f"step {r['step']} from {r.get('primary_provider')}" for r in rows if r.get("is_fallback")))
    hits = [r for r in rows if r.get("cache_hit")]
    if hits or any(r.get("embedding_tokens") for r in rows):
        out.append(f"(cache) = answered by Bifrost's response cache: {len(hits)} calls, {sum(r.get('cache_saved_tokens', 0) for r in hits)} tokens "
                   f"not billed; embedding tokens spent on the cache: {sum(r.get('embedding_tokens', 0) for r in rows)}")
    for r in rows:
        if r.get("escalated"):
            out.append(f"escalated to the big tier at step {r['step']} ({r['escalated']}); model {r.get('served_model') or r['model']}")
    runs = [e for e in events if e["event"] == "tool_run"]
    edits = {"edit_file": 0, "apply_patch": 0, "search_and_replace": 0, "write_file (new file)": 0, "write_file (existing file)": 0}
    for e in runs:
        if e["tool"] in edits:
            edits[e["tool"]] += 1
        elif e["tool"] == "write_file":
            edits["write_file (existing file)" if e.get("overwrote_existing") else "write_file (new file)"] += 1
    if any(edits.values()):
        out.append("edits: " + ", ".join(f"{n} {k}" for k, n in edits.items() if n))
    lookups = [r for r in records if r["type"] == "tavily"]
    if lookups:
        out.append(f"docs_lookup: {len(lookups)} calls, {sum(r['credits'] for r in lookups)} Tavily credits "
                   f"({sum(1 for r in lookups if r['cached'])} answered from the session cache)")
    for e in [e for e in events if e["event"] == "pretest"]:
        out.append("pretest (tests run once before the first model call): " + (
            f"skipped ({e['reason']})" if e["status"] == "skipped" else
            "the tests failed, and the compressed failure went into the first prompt" if e["status"] == "red" else
            "the tests passed, and a one-line note went into the first prompt"))
    stops = [e for e in events if e["event"] == "stop_on_green"]
    if stops:
        out.append(f"stopped on green after step {stops[-1]['step']} (rule {stops[-1].get('rule', 'red_to_green')}): the tests had failed earlier and "
                   "passed now, so no further model call was made")
    held = [e for e in events if e["event"] == "green_not_stopped"]
    if held:
        out.append(f"{len(held)} green test run(s) did not stop the run because the tests had never failed in this session (rule never_red)")
    return "\n".join(out)
