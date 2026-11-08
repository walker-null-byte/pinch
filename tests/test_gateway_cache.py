"""Against the real Bifrost: the exact-match response cache (per-run namespaces) and the model tiers. Skipped without Docker."""

import pytest

from pinch import prove
from pinch.client import ModelClient
from pinch.mock_provider import MockProvider
from test_prove import image_available

MSG = [{"role": "user", "content": "the same question twice"}]


@pytest.mark.skipif(not image_available(), reason=f"needs Docker and {prove.IMAGE}")
def test_bifrost_exact_match_cache_namespaces_and_tier_routing():
    mocks = {name: MockProvider([{"content": f"from {name}", "usage": {"prompt_tokens": 500, "completion_tokens": 20}, "times": 30}], port=port).start()
             for name, port in prove.PORTS.items()}
    try:
        prove.start_gateway(prove.GATEWAY_DIR / "config.json")
        vk = prove.DEMO_ENV["PINCH_VK_ALICE"]

        def ask(namespace, model="mock-small"):
            headers = {"x-bf-cache-key": namespace, "x-bf-cache-type": "direct"} if namespace else {}
            return ModelClient(prove.GATEWAY_URL + "/v1", model, vk, "x-bf-vk", 50, retries=1, extra_headers=headers).chat(MSG)

        import time
        first = ask("run-1")
        time.sleep(1.5)                                   # Bifrost stores the answer a moment after replying
        second, other_run, no_key = ask("run-1"), ask("run-2"), ask("")
        assert not first.cache_hit and second.cache_hit                       # same namespace, same request: served by Bifrost
        assert not other_run.cache_hit and not no_key.cache_hit              # another run's namespace, or no key: never shared
        assert len(mocks["cloud-a"].recorded) == 3                           # the hit never reached the provider
        assert second.embedding_tokens == 0                                  # exact-match needs no embedding provider

        before = len(mocks["cloud-a"].recorded)
        ask("", model="mock-big")                                            # the big tier goes through the same routing rule
        assert mocks["cloud-a"].recorded[before]["body"]["model"] == "mock-big"
    finally:
        prove.stop_gateway()
        for m in mocks.values():
            m.stop()
