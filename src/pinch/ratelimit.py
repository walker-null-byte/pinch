"""Rate limits: tell a per-minute limit (wait and retry) from a per-day limit (stop), and work out how long to wait.

Standard library only, because mini-swe-agent's sandbox script (bench/container/mini_run.py) imports this file too, so that both agents
use exactly the same policy.

Rules:
  - Only a per-day marker means "quota" ("per day", "PerDay", "requests per day", "daily"): waiting a minute will not help.
  - Any other 429 is a rate limit, including Gemini's free-tier "Quota exceeded ... requests per minute ... Please retry in 44.2s"
    (the word "quota" and "RESOURCE_EXHAUSTED" appear in per-minute errors too, so they decide nothing).
  - How long: the provider's own "retry in Ns" / "try again in Ns" / retryDelay, or a Retry-After header, else exponential backoff;
    plus up to 10% jitter and a 1 s cushion, never more than MAX_WAIT.
"""

from __future__ import annotations

import random
import re

MAX_WAIT = 120.0
RATE_LIMIT_RETRIES = 8
DAILY_MARKERS = ("per day", "perday", "per_day", "per-day", "requests per day", "tokens per day", "daily")
_RETRY_IN = re.compile(r"(?:retry|try\s+again)\s*(?:in|after)\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|sec|secs|seconds|m|min|minutes)?\b", re.I)
_RETRY_COMPOUND = re.compile(r"(?:retry|try\s+again)\s*(?:in|after)\s*:?\s*(?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s\b", re.I)  # Groq: "7m30.5s"
_RETRY_DELAY = re.compile(r"retry_?delay\W{0,20}?(?:seconds\W{0,4})?([0-9]+(?:\.[0-9]+)?)\s*(ms|s)?", re.I)
_UNITS = {"ms": 0.001, "m": 60.0, "min": 60.0, "minutes": 60.0}


def is_daily_limit(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in DAILY_MARKERS)


def classify_429(text: str) -> str:
    """'quota' (stop: a per-day limit) or 'rate_limit' (wait and retry). Unknown 429s are rate limits."""
    return "quota" if is_daily_limit(text) else "rate_limit"


def retry_delay(text: str = "", retry_after_header: str | None = None) -> float | None:
    """Seconds the provider asked us to wait, or None."""
    if retry_after_header:
        try:
            return max(float(retry_after_header), 0.0)
        except ValueError:
            pass
    m = _RETRY_COMPOUND.search(text or "")
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))
    for pattern in (_RETRY_IN, _RETRY_DELAY):
        m = pattern.search(text or "")
        if m:
            return float(m.group(1)) * _UNITS.get((m.group(2) or "s").lower(), 1.0)
    return None


def wait_seconds(text: str = "", retry_after_header: str | None = None, attempt: int = 1, backoff: float = 1.0,
                 rng=random.random) -> float:
    """How long to wait before retry number `attempt` (1, 2, ...). Capped at MAX_WAIT."""
    hint = retry_delay(text, retry_after_header)
    base = hint if hint is not None else backoff * 2 ** (attempt - 1)
    return min(base * (1 + 0.1 * rng()) + (1.0 if hint is not None else 0.0), MAX_WAIT)
