"""Read-only use of the Bifrost admin API: remaining budget, and the local-only check.

Endpoints used (docs.getbifrost.ai/features/governance/virtual-keys, /budget-and-limits):
  GET /api/governance/virtual-keys/{id}   a key's provider_configs, allow_all_providers, budgets
The admin API needs the gateway's admin credentials (HTTP Basic). They are separate from the user's
virtual key and come from the environment only. Without them pinch cannot read the budget from
Bifrost and falls back to its own ledger; for a confidential repo it cannot verify the key, so it refuses to start.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class GatewayError(Exception):
    """The admin API could not be reached or did not answer as expected."""


@dataclass
class Budget:
    limit: float
    used: float

    @property
    def remaining(self) -> float:
        return self.limit - self.used


class Gateway:
    def __init__(self, admin_url: str, user: str = "", password: str = "", transport: httpx.BaseTransport | None = None):
        self.admin_url = admin_url.rstrip("/")
        auth = (user, password) if user else None
        self._http = httpx.Client(timeout=10.0, auth=auth, transport=transport)

    def virtual_key(self, vk_id: str) -> dict:
        try:
            response = self._http.get(f"{self.admin_url}/api/governance/virtual-keys/{vk_id}")
        except httpx.HTTPError as e:
            raise GatewayError(f"cannot reach the gateway admin API at {self.admin_url} ({type(e).__name__})") from e
        if response.status_code != 200:
            raise GatewayError(f"the gateway admin API answered HTTP {response.status_code} for key '{vk_id}'")
        try:
            data = response.json()
        except ValueError as e:
            raise GatewayError("the gateway admin API did not return JSON") from e
        return data.get("virtual_key", data)

    def tightest_budget(self, vk_id: str) -> Budget | None:
        """The budget on this key with the least money left, or None if the key has no budget."""
        budgets = [Budget(float(b["max_limit"]), float(b.get("current_usage") or 0)) for b in self.virtual_key(vk_id).get("budgets") or []]
        return min(budgets, key=lambda b: b.remaining, default=None)

    def check_local_only(self, vk_id: str, local_providers: tuple[str, ...], expected_key: str = "") -> list[str]:
        """Confirm this virtual key can reach only providers marked local. Returns the providers it can reach.

        Raises GatewayError with a plain reason if it cannot be confirmed. Fails closed.
        """
        vk = self.virtual_key(vk_id)
        if expected_key and vk.get("value") and vk["value"] != expected_key:
            raise GatewayError(f"the key configured for confidential repos is not the gateway's '{vk_id}'")
        if not vk.get("is_active", True):
            raise GatewayError(f"virtual key '{vk_id}' is switched off in the gateway")
        if vk.get("allow_all_providers"):
            raise GatewayError(f"virtual key '{vk_id}' is allowed to use every provider, including cloud ones")
        reachable = [pc["provider"] for pc in vk.get("provider_configs") or []]
        if not reachable:
            raise GatewayError(f"virtual key '{vk_id}' has no providers, so it cannot reach a local model")
        outside = [p for p in reachable if p not in local_providers]
        if outside:
            raise GatewayError(f"virtual key '{vk_id}' can reach non-local providers: {', '.join(outside)}")
        return reachable

    def close(self) -> None:
        self._http.close()
