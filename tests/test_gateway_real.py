"""The real-provider gateway files (config.real.json, docker-compose.real.yml, .env.example) agree with each other. Offline: nothing is started."""

import json
import re
import tomllib
from pathlib import Path

from pinch import prove
from pinch.ledger import load_prices

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "gateway"
TEXT = (GATEWAY / "config.real.json").read_text()
CONFIG = json.loads(TEXT)
COMPOSE = (GATEWAY / "docker-compose.real.yml").read_text()
EXAMPLE = (ROOT / ".env.example").read_text()
GOV = CONFIG["governance"]
VKS = {v["id"]: v for v in GOV["virtual_keys"]}


def example_values() -> dict[str, str]:
    return {k.strip(): v.strip() for k, _, v in (line.partition("=") for line in EXAMPLE.splitlines() if line and not line.startswith("#") and "=" in line)}


def secret_fields(node, path=""):
    """Every (path, value) that holds a key, password or token."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and k in ("value", "admin_password", "admin_username", "setup_token", "api_key", "token", "password"):
                yield f"{path}.{k}", v
            else:
                yield from secret_fields(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from secret_fields(v, f"{path}[{i}]")


def test_no_literal_secrets_every_key_value_is_an_env_reference():
    fields = list(secret_fields(CONFIG))
    assert len(fields) >= 12
    assert all(v.startswith("env.") for _, v in fields), [p for p, v in fields if not v.startswith("env.")]
    assert not re.search(r"sk-bf-|AIza|gsk_|tvly-", TEXT)


def test_providers():
    assert {"gemini", "groq", "local", "gemini-down"} == set(CONFIG["providers"])
    local = CONFIG["providers"]["local"]
    assert local["network_config"]["base_url"] == "http://127.0.0.1:1234" and local["network_config"]["max_retries"] == 0
    assert local["custom_provider_config"]["request_path_overrides"]["chat_completion"] == "/v1/chat/completions"
    assert CONFIG["providers"]["gemini"]["keys"][0]["value"] == "env.GEMINI_API_KEY"
    assert CONFIG["providers"]["groq"]["keys"][0]["value"] == "env.GROQ_API_KEY"
    assert CONFIG["providers"]["gemini-down"]["network_config"]["base_url"].startswith("http://127.0.0.1:9")  # a closed port: always "down"


def test_confidential_key_reaches_only_local():
    vk = VKS["vk-confidential"]
    assert vk["allow_all_providers"] is False and [pc["provider"] for pc in vk["provider_configs"]] == ["local"]
    assert not any(r["scope_id"] == "vk-confidential" for r in GOV["routing_rules"])
    assert vk["value"] == "env.PINCH_CONFIDENTIAL_KEY"


def test_bench_key_is_gemini_only_with_no_fallback():
    vk = VKS["vk-bench"]
    assert [pc["provider"] for pc in vk["provider_configs"]] == ["gemini"]
    assert not any(r["scope_id"] == "vk-bench" for r in GOV["routing_rules"]), "a rule would be the place a fallback comes from"
    assert {b["max_limit"] for b in GOV["budgets"] if b["virtual_key_id"] == "vk-bench"} == {15.0}


def test_budgets_and_the_fallback_chains():
    limits = {b["virtual_key_id"]: b["max_limit"] for b in GOV["budgets"]}
    assert limits["vk-me"] == 2.0 and limits["vk-demo-low"] == 0.01
    rules = {r["scope_id"]: r for r in GOV["routing_rules"]}
    assert rules["vk-me"]["targets"] == [{"provider": "gemini", "weight": 1.0}]  # no model: the caller's model (small or big) is kept
    assert rules["vk-me"]["fallbacks"] == ["groq/qwen/qwen3.8-27b", "local/gemma4"]
    assert rules["vk-failover-demo"]["targets"][0]["provider"] == "gemini-down" and rules["vk-failover-demo"]["fallbacks"] == ["groq/qwen/qwen3.8-27b"]
    assert "vk-demo-low" not in rules, "the budget demo must stop at the limit, not slide onto another provider"


def test_key_env_names_match_the_documented_ones():
    names = {vk["id"]: vk["value"] for vk in GOV["virtual_keys"]}
    assert names == {"vk-me": "env.PINCH_GATEWAY_KEY", "vk-bench": "env.PINCH_BENCH_KEY", "vk-confidential": "env.PINCH_CONFIDENTIAL_KEY",
                     "vk-demo-low": "env.PINCH_DEMO_LOW_KEY", "vk-failover-demo": "env.PINCH_FAILOVER_DEMO_KEY"}
    assert GOV["auth_config"]["admin_username"] == "env.PINCH_GATEWAY_ADMIN_USER" and CONFIG["setup_token"] == "env.PINCH_GATEWAY_SETUP_TOKEN"
    assert example_values()["PINCH_BUDGET_VK_ID"] == "vk-me" and example_values()["PINCH_CONFIDENTIAL_VK_ID"] == "vk-confidential"


def test_semantic_cache_is_the_same_as_the_mock_config():
    mock = json.loads((GATEWAY / "config.json").read_text())
    assert CONFIG["plugins"] == mock["plugins"] and CONFIG["vector_store"] == mock["vector_store"] and CONFIG["client"] == mock["client"]


def default_models() -> set[str]:
    values = example_values()
    return {values[n] for n in ("PINCH_MODEL", "PINCH_SMALL_MODEL", "PINCH_BIG_MODEL", "PINCH_LOCAL_MODEL")}


def bare(model: str) -> str:
    """Bifrost's model name for an 'provider/model' name as pinch sends it."""
    return model.split("/", 1)[1]


def test_every_default_model_has_a_pricing_override_and_a_prices_toml_entry():
    overrides = {o["pattern"]: json.loads(o["pricing_patch"]) for o in GOV["pricing_overrides"]}
    prices = load_prices()
    models = default_models()
    assert {"gemini/gemini-3.5-flash-lite", "gemini/gemini-3.6-flash", "local/gemma4"} <= models
    for model in models:
        assert bare(model) in overrides, f"no pricing override for {model}"
        assert bare(model) in prices, f"no prices.toml entry for {model}"
    # the failover target too
    for model in ("qwen/qwen3.8-27b", "openai/gpt-oss-120b"):
        assert model in overrides and model in prices


def test_pinch_and_the_gateway_use_the_same_prices():
    prices = tomllib.loads((ROOT / "src/pinch/prices.toml").read_text())["models"]
    for o in GOV["pricing_overrides"]:
        patch, entry = json.loads(o["pricing_patch"]), prices[o["pattern"]]
        assert abs(patch["input_cost_per_token"] * 1e6 - entry["input"]) < 1e-9, o["pattern"]
        assert abs(patch["output_cost_per_token"] * 1e6 - entry["output"]) < 1e-9, o["pattern"]
        if "cache_read_input_token_cost" in patch:
            assert abs(patch["cache_read_input_token_cost"] * 1e6 - entry["cached"]) < 1e-9, o["pattern"]


def test_compose_pins_the_image_and_keeps_tavily_out():
    body = "\n".join(line for line in COMPOSE.splitlines() if not line.lstrip().startswith("#"))
    images = [line.strip() for line in body.splitlines() if line.strip().startswith("image:")]
    assert images == [f"image: {prove.IMAGE}"] and "latest" not in body
    assert "TAVILY" not in body and "OPENROUTER" not in body and "ANTHROPIC" not in body
    assert "env_file" not in body, "only named variables may reach the container"
    assert "container_name: pinch-bifrost-real" in body and "network_mode: host" in body
    assert 'APP_PORT: "8180"' in body and "APP_HOST: 127.0.0.1" in body
    assert "./config.real.json:/app/data/config.json:ro" in body


def test_every_env_reference_in_the_config_is_in_the_compose_environment_and_in_env_example():
    referenced = set(re.findall(r'"env\.([A-Z0-9_]+)"', TEXT))
    in_compose = set(re.findall(r"^      ([A-Z0-9_]+): \$\{\1:\?", COMPOSE, re.M))
    assert referenced and referenced <= in_compose, sorted(referenced - in_compose)
    assert in_compose <= referenced, f"passed to the container but never used: {sorted(in_compose - referenced)}"
    names = {line.split("=", 1)[0] for line in EXAMPLE.splitlines() if "=" in line and not line.startswith("#")}
    assert referenced <= names, sorted(referenced - names)


def test_env_example_base_url_matches_the_compose_port():
    assert example_values()["PINCH_BASE_URL"] == "http://127.0.0.1:8180/v1"


def test_mock_stack_does_not_use_the_real_gateway_port():
    assert prove.GATEWAY_URL.endswith(":8190")
    assert 'APP_PORT: "8190"' in (GATEWAY / "docker-compose.yml").read_text()


def test_real_gateway_script_reads_the_same_compose_file():
    import importlib.util
    spec = importlib.util.spec_from_file_location("real_gateway", ROOT / "scripts" / "real_gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    c = module.read_compose()
    assert c["image"] == prove.IMAGE and c["name"] == "pinch-bifrost-real" and c["volume"] == "pinch-bifrost-real-data"
    from_env = {k for k, v in c["env"].items() if v is None}
    assert from_env == set(re.findall(r"^      ([A-Z0-9_]+): \$\{\1:\?", COMPOSE, re.M))
    assert c["env"]["APP_PORT"] == "8180" and c["env"]["APP_HOST"] == "127.0.0.1"
