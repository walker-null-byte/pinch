"""The committed gateway files say what the README says they do."""

import json
from pathlib import Path

from pinch import prove

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "gateway"
CONFIG = json.loads((GATEWAY / "config.json").read_text())


def test_image_tag_is_pinned_everywhere_and_never_latest():
    compose = (GATEWAY / "docker-compose.yml").read_text()
    assert f"image: {prove.IMAGE}" in compose
    images = [line for line in compose.splitlines() if line.strip().startswith("image:")]
    assert len(images) == 1 and not images[0].endswith(("latest", ":")) and ":" in images[0]
    assert prove.IMAGE.split(":")[1].startswith("v") and "latest" not in prove.IMAGE


def test_demo_env_matches_the_values_prove_uses():
    values = dict(line.split("=", 1) for line in (GATEWAY / "demo.env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    assert values == prove.DEMO_ENV


def test_three_mock_providers_on_the_ports_prove_starts():
    providers = CONFIG["providers"]
    assert set(providers) == {"cloud-a", "cloud-b", "local"}
    for name, port in prove.PORTS.items():
        assert providers[name]["network_config"]["base_url"] == f"http://127.0.0.1:{port}"


def test_every_user_has_a_budget_and_the_chain_is_cloud_a_cloud_b_local():
    governance = CONFIG["governance"]
    budgets = {b["virtual_key_id"]: b["max_limit"] for b in governance["budgets"]}
    assert budgets == {"vk-alice": 1.00, "vk-bob": 0.25, "vk-penny": 0.01}
    rules = {r["scope_id"]: r for r in governance["routing_rules"]}
    assert set(rules) == set(budgets)
    for rule in rules.values():
        assert rule["scope"] == "virtual_key" and rule["targets"][0]["provider"] == "cloud-a"
        assert rule["fallbacks"] == ["cloud-b/", "local/"] and "model" not in rule["targets"][0]   # the caller's model (small or big tier) is kept


def test_confidential_key_can_reach_only_local_and_has_no_cloud_route():
    governance = CONFIG["governance"]
    vk = next(v for v in governance["virtual_keys"] if v["id"] == "vk-confidential")
    assert [pc["provider"] for pc in vk["provider_configs"]] == ["local"] and not vk.get("allow_all_providers")
    assert not any(r.get("scope_id") == "vk-confidential" for r in governance["routing_rules"])
    # The response cache exists (exact-match, in-process store) but only answers requests that carry a cache key, and no default key is set
    plugin = next(p for p in CONFIG["plugins"] if p["name"] == "semantic_cache")
    assert plugin["config"]["default_cache_key"] == "" and plugin["config"]["dimension"] == 1 and "provider" not in plugin["config"]
    assert CONFIG["vector_store"]["type"] == "chromem"


def test_real_provider_templates_are_not_loaded_and_their_key_names_are_documented():
    templates = json.loads((GATEWAY / "providers.real.example.json").read_text())
    assert {"gemini", "groq", "openrouter", "anthropic", "local"} <= set(templates["providers"])
    assert not set(templates["providers"]) & {"gemini", "groq", "openrouter", "anthropic"} & set(CONFIG["providers"])
    example = (ROOT / ".env.example").read_text()
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "PINCH_CONFIDENTIAL_KEY"):
        assert f"\n{name}=" in example
    # Only these names may carry a value in .env.example, and they are plain defaults, not secrets.
    defaults = {"PINCH_BASE_URL", "PINCH_KEY_HEADER", "PINCH_CONFIDENTIAL_VK_ID", "PINCH_BUDGET_VK_ID", "PINCH_GATEWAY_ADMIN_USER",
                "PINCH_MODEL", "PINCH_LOCAL_MODEL", "PINCH_SMALL_MODEL", "PINCH_BIG_MODEL", "LOCAL_PROVIDER_KEY"}
    assert all(line.split("=", 1)[1] == "" or line.split("=", 1)[0] in defaults
               for line in example.splitlines() if "=" in line and not line.startswith("#")), "no real values in .env.example"
