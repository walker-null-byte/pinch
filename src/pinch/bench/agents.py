"""The two agents, as seen from the harness: how each is started in the container, and how its numbers are read back.

Token counts come ONLY from the `usage` block of the provider's reply as relayed by Bifrost:
  pinch: the ledger lines pinch writes from each reply's `usage` (client.py).
  mini-swe-agent: the full response objects it stores in its trajectory (`extra.response.usage`).
Nothing is estimated from text length. A call whose reply has no usage block marks the whole run `usage_complete = false`,
and such a run is left out of the token averages.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..ledger import estimate_cost, load_prices
from ..ratelimit import DAILY_MARKERS, classify_429
from ..session import Session

MINI_STOPS = {"Submitted": "done", "LimitsExceeded": "step_limit", "TimeExceeded": "timeout", "RepeatedFormatError": "format_errors"}
CONTEXT_WORDS = ("context length", "context_length", "maximum context", "too long", "too many tokens", "reduce the length",
                 "context window", "contextwindow")
# Only a per-day limit (or a hard "insufficient_quota") stops a benchmark. The word "quota" alone does not: Gemini's per-minute
# error says "You exceeded your current quota ... requests per minute ... retry in 44s" (RESOURCE_EXHAUSTED), which clears by waiting.
QUOTA_WORDS = (*DAILY_MARKERS, "insufficient_quota")
BUDGET_WORDS = ("budget_exceeded", "budget exceeded", "budget limit", "budget has been exceeded")


@dataclass
class Outcome:
    agent: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = 0.0
    usage_complete: bool = True
    cache_hits: int = 0
    cache_saved_tokens: int = 0
    stop: str = ""  # done | step_limit | timeout | format_errors | error
    infra: str | None = None  # rate_limit | provider_unavailable | quota | budget | config | bad_request | harness
    infra_detail: str = ""
    auto_approvals: int | None = None  # pinch only: how many writes/shell/git calls the auto-approve flag let through
    tavily_calls: int = 0
    tavily_credits: int = 0
    escalations: int = 0
    fallbacks: int = 0
    techniques_on: list[str] = field(default_factory=list)
    served_models: list[str] = field(default_factory=list)
    wait_seconds: float = 0.0  # seconds spent in pacing and rate-limit waits (inside the agent's wall time)
    rate_limit_waits: int = 0
    pretest: str = ""  # pinch: red | green | skipped:<reason> | "" (technique off)
    final_text: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def classify_error(kind: str = "", status: int | None = None, text: str = "") -> str | None:
    """Is this model error the infrastructure's fault (None means: the agent's own failure, which counts)?"""
    low = (text or "").lower()
    if any(w in low for w in CONTEXT_WORDS) and (status in (400, 413, None) or kind in ("other", "")):
        return None  # the agent's history grew past the model's window: that is a result, not an outage
    if any(w in low for w in BUDGET_WORDS) or status == 402 or kind == "budget":
        return "budget"
    if status == 429 or kind in ("rate_limit", "RateLimitError", "quota"):
        return classify_429(low)  # per day -> "quota" (stop); any other 429, known or not, -> "rate_limit" (wait and retry)
    if any(w in low for w in QUOTA_WORDS):
        return "quota"
    if status in (401, 403, 404) or kind in ("auth", "AuthenticationError", "NotFoundError", "PermissionDeniedError"):
        return "config"
    if kind in ("unreachable", "server", "APIConnectionError", "Timeout", "APITimeoutError", "InternalServerError",
                "ServiceUnavailableError", "BadGatewayError") or (status is not None and status >= 500):
        return "provider_unavailable"
    return "bad_request"


# -- pinch ----------------------------------------------------------------------------------------

def pinch_command(prompt: str, *, model: str, base_url: str, savings: str, style: str, max_steps: int, tool_mode: str) -> list[str]:
    return ["/opt/tools/pinch/bin/python", "-I", "/opt/bench/pinch_run.py", prompt, "--repo", "/work", "--model", model,
            "--base-url", base_url, "--savings", savings, "--style", style, "--max-steps", str(max_steps), "--mode", tool_mode,
            "--session-dir", "/out/sessions", "--auto-approve"]


def parse_pinch(out: Path, exit_code: int, stdout: str) -> Outcome:
    o = Outcome("pinch")
    files = sorted((out / "sessions").glob("*.jsonl")) if (out / "sessions").is_dir() else []
    if not files:
        o.stop, o.infra, o.infra_detail = "error", "harness", "pinch wrote no session file: " + stdout[-300:].replace("\n", " ")
        return o
    records = Session(files[-1]).records()
    ledger = [r for r in records if r["type"] == "ledger"]
    events = [r for r in records if r["type"] == "event"]
    costs = []
    for r in ledger:
        o.calls += 1
        o.input_tokens += int(r.get("input_tokens") or 0)
        o.output_tokens += int(r.get("output_tokens") or 0)
        o.cached_tokens += int(r.get("cached_tokens") or 0)
        if r.get("cache_hit"):
            o.cache_hits += 1
            o.cache_saved_tokens += int(r.get("cache_saved_tokens") or 0)
        elif not r.get("usage_reported", True):
            o.usage_complete = False
        costs.append(r.get("cost_usd"))
        o.wait_seconds += float(r.get("wait_seconds") or 0)
        o.rate_limit_waits += int(r.get("rate_limit_waits") or 0)
        if r.get("is_fallback"):
            o.fallbacks += 1
        if (r.get("served_model") or r.get("model")) not in o.served_models:
            o.served_models.append(r.get("served_model") or r.get("model"))
    o.cost_usd = None if any(c is None for c in costs) else round(sum(costs), 8)
    names = [e["event"] for e in events]
    pre = [e for e in events if e["event"] == "pretest"]
    if pre:
        o.pretest = pre[-1]["status"] if pre[-1]["status"] != "skipped" else "skipped:" + pre[-1].get("reason", "")
    o.auto_approvals = names.count("auto_approved")
    o.escalations = names.count("escalated")
    saved = [e for e in events if e["event"] == "savings"]
    o.techniques_on = list(saved[-1]["techniques"]) if saved else []
    tav = [r for r in records if r["type"] == "tavily"]
    o.tavily_calls, o.tavily_credits = len(tav), sum(int(r.get("credits") or 0) for r in tav)
    errors = [e for e in events if e["event"] == "model_error"]
    last_text = [r["message"].get("content") or "" for r in records if r["type"] == "message" and r["message"].get("role") == "assistant"]
    o.final_text = (last_text[-1] if last_text else "")[:300]
    if "budget_stop" in names:
        o.stop, o.infra, o.infra_detail = "error", "budget", "pinch's own pre-call budget check stopped the task"
    elif exit_code == 0:
        o.stop = "done"
    elif "step_limit" in names:
        o.stop = "step_limit"
    elif errors:
        e = errors[-1]
        o.stop = "error"
        o.infra = classify_error(e.get("kind", ""), e.get("status"), e.get("error", ""))
        o.infra_detail = f"{e.get('kind')} {e.get('status')}: {str(e.get('error', ''))[:200]}"
        if o.infra is None:
            o.stop = "context_overflow"
    else:
        o.stop = "error"
        if "confidential_violation" in names:
            o.infra, o.infra_detail = "config", "confidential violation"
    return o


# -- mini-swe-agent -------------------------------------------------------------------------------

def mini_spec(prompt: str, *, model: str, base_url: str, max_steps: int, max_tokens: int, wall_time: int,
              min_call_interval: float = 0.0) -> dict:
    return {"min_call_interval": min_call_interval, "task": prompt, "model": model, "api_base": base_url, "max_steps": max_steps, "max_tokens": max_tokens,
            "wall_time": wall_time, "command_timeout": 180, "trajectory": "/out/traj.json", "result": "/out/mini_result.json"}


def mini_command() -> list[str]:
    return ["/opt/tools/mini/bin/python", "-I", "/opt/bench/mini_run.py", "/out/mini_spec.json"]


def parse_mini(out: Path, exit_code: int, stdout: str) -> Outcome:
    o = Outcome("mini")
    result_file, traj_file = out / "mini_result.json", out / "traj.json"
    try:
        result = json.loads(result_file.read_text())
    except (OSError, json.JSONDecodeError):
        result = {}
    try:
        traj = json.loads(traj_file.read_text())
    except (OSError, json.JSONDecodeError):
        traj = {}
    price_model = None
    for m in traj.get("messages", []):
        resp = (m.get("extra") or {}).get("response")
        if not isinstance(resp, dict):
            continue
        o.calls += 1
        usage = resp.get("usage")
        if not isinstance(usage, dict) or usage.get("prompt_tokens") is None:
            o.usage_complete = False
            continue
        o.input_tokens += int(usage.get("prompt_tokens") or 0)
        o.output_tokens += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = (details.get("cached_tokens") if isinstance(details, dict) else 0) or usage.get("cache_read_input_tokens") or 0
        o.cached_tokens += int(cached)
        routing = ((resp.get("extra_fields") or {}).get("routing_info") or {}) if isinstance(resp.get("extra_fields"), dict) else {}
        if routing.get("is_fallback"):
            o.fallbacks += 1  # only visible when the client library keeps Bifrost's extra_fields
        if resp.get("model") and resp["model"] not in o.served_models:
            o.served_models.append(resp["model"])
        price_model = price_model or resp.get("model")
    o.wait_seconds = float(result.get("wait_seconds") or 0)
    o.rate_limit_waits = int(result.get("rate_limit_waits") or 0)
    status = result.get("exit_status", "")
    if not result and not traj:
        o.stop, o.infra, o.infra_detail = "error", "harness", "mini-swe-agent left no result: " + stdout[-300:].replace("\n", " ")
    elif status in MINI_STOPS:
        o.stop = MINI_STOPS[status]
    else:
        o.stop = "error"
        err = result.get("error", "") or result.get("error_type", "")
        o.infra = classify_error(result.get("error_type", ""), _status_in(err), err)
        o.infra_detail = f"{result.get('error_type')}: {err[:200]}"
        if o.infra is None:
            o.stop = "context_overflow"
    o.final_text = (traj.get("info", {}).get("submission") or "")[:300]
    return o


def _status_in(text: str) -> int | None:
    m = re.search(r"\b(?:status|code)\W{0,3}(\d{3})\b", text or "", re.I) or re.search(r"\b(4\d\d|5\d\d)\b", text or "")
    return int(m.group(1)) if m else None


def fill_cost(o: Outcome, model: str) -> None:
    """mini-swe-agent has no cost of its own that we trust (it uses litellm's price map). Price both agents the same way:
    provider-reported tokens times our prices.toml, so the dollar figures are comparable."""
    if o.agent == "mini":
        prices = load_prices()
        served = o.served_models[0] if o.served_models else model
        o.cost_usd = estimate_cost(served, o.input_tokens, o.output_tokens, o.cached_tokens, prices) \
            if o.usage_complete else None
        if o.cost_usd is None and served != model:
            o.cost_usd = estimate_cost(model, o.input_tokens, o.output_tokens, o.cached_tokens, prices)


def keep_artifacts(out: Path, dest: Path, diff_text: str, log: str) -> str:
    """Keep the run's session / trajectory (compressed), the diff and the log tail. Returns the folder relative to results/."""
    dest.mkdir(parents=True, exist_ok=True)
    for src in list(out.glob("sessions/*.jsonl")) + [out / "traj.json", out / "mini_result.json"]:
        if src.is_file():
            with src.open("rb") as f, gzip.open(dest / (src.name + ".gz"), "wb") as g:
                shutil.copyfileobj(f, g)
    (dest / "changes.diff").write_text(diff_text[:60000])
    (dest / "agent.log").write_text(log[-20000:])
    return dest.name
