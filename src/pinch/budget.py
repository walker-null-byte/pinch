"""Budget check before every model call (requirement 2).

Bifrost's virtual-key budget is the source of truth. Before each call pinch asks Bifrost for the
tightest budget on the user's key. If Bifrost cannot say (no admin access, unreachable), pinch
uses its own ledger against `budget_usd`. It then compares what is left with a conservative
estimate of the call's cost:

    input tokens  = characters of the request / 3        (real text averages 3.5-4, so this over-counts)
    output tokens = max_tokens                           (the most the model may write)
    cost          = prices.toml (a model with no price entry is charged at the [default] entry)

If the estimate is more than what is left, pinch does not make the call. Bifrost rejects calls
over budget too (HTTP 402); that is the second layer and is handled the same way.

Bifrost adds a call's cost to a key's usage a moment after the call. So pinch also adds up
the calls it made itself since it first looked, and uses whichever spend figure is larger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .gateway import Gateway, GatewayError
from .ledger import price_entry


@dataclass
class BudgetVerdict:
    ok: bool
    limit: float | None  # None when no limit is known
    spent: float
    estimate: float
    source: str  # "bifrost", "pinch ledger" or "none"

    @property
    def remaining(self) -> float | None:
        return None if self.limit is None else self.limit - self.spent


def estimate_call_cost(messages: list[dict], tools: list[dict] | None, model: str, max_tokens: int, prices: dict) -> float:
    entry = price_entry(model, prices)
    chars = len(json.dumps(messages, default=str)) + len(json.dumps(tools or [], default=str))
    return (chars / 3 * entry["input"] + max_tokens * entry["output"]) / 1_000_000


class BudgetGuard:
    def __init__(self, gateway: Gateway | None, vk_id: str, fallback_limit: float | None, prices: dict,
                 session_spend: Callable[[], float], user_spend: Callable[[], float] | None = None):
        self.gateway, self.vk_id, self.fallback_limit, self.prices = gateway, vk_id, fallback_limit, prices
        self.session_spend = session_spend  # dollars pinch has recorded for this session, from its ledger
        self.user_spend = user_spend  # dollars this user has spent this month across sessions (the spend file)
        self._baseline: tuple[float, float] | None = None  # (gateway usage, session spend) when first read

    def check(self, messages: list[dict], tools: list[dict] | None, model: str, max_tokens: int) -> BudgetVerdict:
        estimate = estimate_call_cost(messages, tools, model, max_tokens, self.prices)
        limit, spent, source = self._limit_and_spend()
        if limit is None:
            return BudgetVerdict(True, None, spent, estimate, "none")
        return BudgetVerdict(estimate <= limit - spent, limit, spent, estimate, source)

    def _limit_and_spend(self) -> tuple[float | None, float, str]:
        own = self.session_spend()
        if self.gateway and self.vk_id:
            try:
                budget = self.gateway.tightest_budget(self.vk_id)
            except GatewayError:
                budget = None
            if budget:
                if self._baseline is None:
                    self._baseline = (budget.used, own)
                base_used, base_own = self._baseline
                return budget.limit, max(budget.used, base_used + (own - base_own)), "bifrost"
        if self.fallback_limit is not None:
            return self.fallback_limit, self.user_spend() if self.user_spend else own, "pinch ledger"
        return None, own, "none"
