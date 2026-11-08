"""Phase 2 behaviour that can be checked offline: gateway admin client, budget, confidential key, retries, failover notice.

The real Bifrost is exercised by `pinch prove` (and tests/test_prove.py when Docker is available).
"""

import hashlib
import json

import httpx
import pytest

from conftest import FakeGateway
from helpers import call
from pinch.budget import BudgetGuard, estimate_call_cost
from pinch.cli import build_agent
from pinch.client import ModelClient, ProviderError
from pinch.config import Config
from pinch.gateway import Budget, Gateway, GatewayError
from pinch.ledger import load_prices
from pinch.mock_provider import MockProvider, redact_headers
from pinch.policy import MARKER_NAME, load_policy
from pinch.session import Session

MSG = [{"role": "user", "content": "hi"}]


def fingerprint(value: str) -> str:
    return "redacted:" + hashlib.sha256(value.encode()).hexdigest()[:8]


# -- gateway admin client ------------------------------------------------------------------------

def gateway_returning(vk: dict | None = None, status: int = 200) -> Gateway:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/governance/virtual-keys/vk-x"
        return httpx.Response(status, json={"virtual_key": vk} if vk is not None else {})
    return Gateway("http://gw", "admin", "pw", transport=httpx.MockTransport(handler))


def vk(providers=("local",), **extra) -> dict:
    return {"id": "vk-x", "is_active": True, "allow_all_providers": False, "value": "sk-bf-x",
            "provider_configs": [{"provider": p} for p in providers], "budgets": [], **extra}


def test_tightest_budget_is_the_one_with_least_left():
    gw = gateway_returning(vk(budgets=[{"max_limit": 10, "current_usage": 1}, {"max_limit": 1, "current_usage": 0.75}]))
    assert gw.tightest_budget("vk-x") == Budget(limit=1, used=0.75) and gw.tightest_budget("vk-x").remaining == 0.25
    assert gateway_returning(vk()).tightest_budget("vk-x") is None


def test_local_only_check_accepts_a_key_limited_to_local_providers():
    assert gateway_returning(vk()).check_local_only("vk-x", ("local",), "sk-bf-x") == ["local"]


@pytest.mark.parametrize("key,reason", [
    (vk(providers=("local", "cloud-a")), "cloud-a"),
    (vk(allow_all_providers=True), "every provider"),
    (vk(providers=()), "no providers"),
    (vk(is_active=False), "switched off"),
    (vk(value="sk-bf-other"), "not the gateway's"),
])
def test_local_only_check_fails_closed(key, reason):
    with pytest.raises(GatewayError, match=reason):
        gateway_returning(key).check_local_only("vk-x", ("local",), "sk-bf-x")


def test_gateway_errors_are_plain():
    with pytest.raises(GatewayError, match="HTTP 403"):
        gateway_returning(status=403).virtual_key("vk-x")
    down = Gateway("http://127.0.0.1:9")
    with pytest.raises(GatewayError, match="cannot reach"):
        down.virtual_key("vk-x")


# -- confidential repos need a verified local-only key ----------------------------------------------

def confidential_cfg(tmp_path, **kw) -> Config:
    return Config(session_dir=tmp_path / "s", config_path=tmp_path / "none.toml", **kw)


def make_confidential(repo, tmp_path, cfg, gateway, **kw):
    (repo / MARKER_NAME).write_text("")
    policy = load_policy(repo)
    return build_agent(cfg, policy, Session.create(cfg.session_dir, {"repo": str(repo)}), out=kw.get("out", lambda t: None), gateway=gateway)


def test_confidential_repo_without_a_local_key_does_not_start(repo, tmp_path):
    with pytest.raises(SystemExit, match="none is set"):
        make_confidential(repo, tmp_path, confidential_cfg(tmp_path, gateway_key="user-key"), FakeGateway())


def test_confidential_repo_without_admin_access_does_not_start(repo, tmp_path):
    with pytest.raises(SystemExit, match="could not confirm"):
        make_confidential(repo, tmp_path, confidential_cfg(tmp_path, confidential_key="k"), None)


def test_confidential_repo_refuses_a_key_that_can_reach_the_cloud(repo, tmp_path):
    with pytest.raises(SystemExit, match="non-local providers: cloud-a"):
        make_confidential(repo, tmp_path, confidential_cfg(tmp_path, confidential_key="k"), FakeGateway(local_ok=False))


def test_localhost_gateway_is_not_treated_as_local(repo, tmp_path):
    """Bifrost on localhost forwards to the cloud, so a localhost URL proves nothing."""
    cfg = confidential_cfg(tmp_path, base_url="http://127.0.0.1:8180/v1", gateway_key="user-key")
    with pytest.raises(SystemExit):
        make_confidential(repo, tmp_path, cfg, FakeGateway())


def test_confidential_repo_sends_only_the_local_key_and_never_a_cache_key(make_rig, repo):
    (repo / MARKER_NAME).write_text("")
    rig = make_rig([{"content": "ok"}], gateway_key="user-key", confidential_key="local-key", cache_key="shared",
                   key_header="x-bf-vk", savings="all")
    rig.agent.run("go")
    headers = rig.mock.recorded[0]["headers"]
    assert headers["x-bf-vk"] == fingerprint("local-key") and headers["x-bf-vk"] != fingerprint("user-key")
    assert not any(h.startswith("x-bf-cache") for h in headers)
    assert any("can reach only local providers" in line for line in rig.said)


def test_normal_repo_uses_the_user_key_and_may_opt_in_to_the_cache(make_rig):
    rig = make_rig([{"content": "ok"}], gateway_key="user-key", confidential_key="local-key", cache_key="shared", key_header="x-bf-vk", savings="cache")
    rig.agent.run("go")
    headers = rig.mock.recorded[0]["headers"]
    assert headers["x-bf-vk"] == fingerprint("user-key") and headers["x-bf-cache-key"] == fingerprint("shared")


def test_a_cloud_answer_for_a_confidential_repo_stops_the_task(make_rig, repo):
    (repo / MARKER_NAME).write_text("")
    rig = make_rig([{"tool_calls": [call("get_working_dir")], "provider": "cloud-a"}, {"content": "never reached"}])
    result = rig.agent.run("go")
    assert result.stop == "error" and "not a local provider" in result.text and len(rig.sent()) == 1
    assert rig.session_events("confidential_violation")


# -- budget -----------------------------------------------------------------------------------------

def test_estimate_is_chars_over_three_plus_max_tokens_at_the_price_table():
    prices = {"m": {"input": 3.0, "output": 6.0}, "default": {"input": 100.0, "output": 100.0}}
    messages = [{"role": "user", "content": "x" * 300}]
    chars = len(json.dumps(messages))
    assert estimate_call_cost(messages, None, "m", 1000, prices) == pytest.approx((chars / 3 * 3.0 + 1000 * 6.0) / 1e6 + (len("[]") / 3 * 3.0) / 1e6)
    assert estimate_call_cost(messages, None, "no-such-model", 10, prices) > estimate_call_cost(messages, None, "m", 10, prices)


def test_guard_uses_bifrost_first_and_corrects_for_its_delay():
    gw = FakeGateway(budget=Budget(limit=0.01, used=0.002))
    spent = [0.0]
    guard = BudgetGuard(gw, "vk", None, load_prices(), lambda: spent[0])
    first = guard.check(MSG, None, "mock-small", 10)
    assert first.source == "bifrost" and first.ok and first.spent == pytest.approx(0.002)
    spent[0] = 0.0075      # pinch made calls since; Bifrost has not booked them yet (still says 0.002)
    later = guard.check(MSG, None, "mock-small", 10)
    assert later.spent == pytest.approx(0.0095) and later.remaining == pytest.approx(0.0005)
    assert not guard.check(MSG, None, "mock-small", 1000).ok


def test_guard_falls_back_to_pinchs_ledger_then_to_no_limit():
    spent = [0.004]
    ledger_only = BudgetGuard(FakeGateway(budget=None), "vk", 0.005, load_prices(), lambda: spent[0])
    verdict = ledger_only.check(MSG, None, "mock-small", 1000)
    assert verdict.source == "pinch ledger" and verdict.limit == 0.005 and not verdict.ok
    assert BudgetGuard(None, "", None, load_prices(), lambda: 1e9).check(MSG, None, "mock-small", 1000).ok


def test_pinch_stops_before_the_call_when_the_estimate_exceeds_what_is_left(make_rig):
    rig = make_rig([{"content": "must not be reached"}], gateway=FakeGateway(budget=Budget(0.01, 0.0099)), max_tokens=1000,
                   confidential_key="local-key")
    result = rig.agent.run("go")
    assert result.stop == "budget" and rig.sent() == []                       # nothing was sent
    assert "$0.0099 of $0.0100" in rig.budget_prompts[0] and "Nothing was sent" in rig.budget_prompts[0]
    assert "pinch --resume" in result.text and rig.session_events("budget_stop")
    assert any("[l] continue on the local tier" in line and "[r]" in line and "[q]" in line for line in rig.said)


def test_budget_choice_l_continues_on_the_local_key(make_rig):
    rig = make_rig([{"content": "answered locally"}], gateway=FakeGateway(budget=Budget(0.01, 0.0099)), max_tokens=1000,
                   confidential_key="local-key", gateway_key="user-key", key_header="x-bf-vk", budget_answers=("l",))
    result = rig.agent.run("go")
    assert result.stop == "done" and result.text == "answered locally"
    assert rig.mock.recorded[0]["headers"]["x-bf-vk"] == fingerprint("local-key")
    assert rig.agent.tier == "local" and rig.session_events("tier_local")


def test_budget_choice_l_without_a_local_key_just_stops(make_rig):
    rig = make_rig([{"content": "no"}], gateway=FakeGateway(budget=Budget(0.01, 0.0099)), max_tokens=1000, budget_answers=("l",))
    assert rig.agent.run("go").stop == "budget" and rig.sent() == []
    assert any("not set up" in line for line in rig.said)


def test_budget_choice_r_explains_how_to_raise_the_limit(make_rig):
    rig = make_rig([{"content": "no"}], gateway=FakeGateway(budget=Budget(0.01, 0.0099)), max_tokens=1000, budget_answers=("r",),
                   budget_vk_id="vk-penny")
    result = rig.agent.run("go")
    assert result.stop == "budget" and "virtual key 'vk-penny'" in result.text and "budget_usd" in result.text


def test_bifrosts_own_402_gets_the_same_menu(make_rig):
    rig = make_rig([{"status": 402, "message": "Budget exceeded: 0.0116 >= 0.0100 dollars"}], budget_answers=("q",))
    result = rig.agent.run("go")
    assert result.stop == "budget" and "Budget exceeded" in rig.budget_prompts[0]
    assert len(rig.sent()) == 1 and rig.session_events("model_error")[0]["kind"] == "budget"


def test_ledger_fallback_limit_comes_from_config(make_rig):
    rig = make_rig([{"content": "ok", "usage": {"prompt_tokens": 3000, "completion_tokens": 100}}, {"content": "no"}],
                   budget_usd=0.0035, max_tokens=100)
    first = rig.agent.run("one")
    assert first.stop == "done"
    assert rig.agent.run("two").stop == "budget" and len(rig.sent()) == 1


# -- retries ----------------------------------------------------------------------------------------

def test_client_retries_the_gateway_on_429_and_5xx_then_succeeds():
    sleeps = []
    script = [{"status": 429, "headers": {"Retry-After": "2"}}, {"status": 500}, {"content": "finally"}]
    with MockProvider(script) as mock:
        reply = ModelClient(mock.url, "m", retries=3, backoff=1.0, sleep=sleeps.append).chat(MSG)
    assert reply.message["content"] == "finally" and reply.attempts == 3
    assert sleeps == [2.0, 2.0]          # Retry-After for the 429, then backoff (1s doubled) for the 500


def test_client_gives_up_after_three_tries():
    with MockProvider([{"status": 503}] * 5) as mock:
        client = ModelClient(mock.url, "m", retries=3, backoff=0, sleep=lambda s: None)
        with pytest.raises(ProviderError) as err:
            client.chat(MSG)
        assert err.value.kind == "server" and len(mock.recorded) == 3


@pytest.mark.parametrize("status,kind", [(400, "other"), (401, "auth"), (402, "budget"), (403, "auth")])
def test_client_does_not_retry_errors_a_retry_cannot_fix(status, kind):
    with MockProvider([{"status": status}] * 3) as mock:
        with pytest.raises(ProviderError) as err:
            ModelClient(mock.url, "m", retries=3, backoff=0, sleep=lambda s: None).chat(MSG)
        assert err.value.kind == kind and len(mock.recorded) == 1


def test_unreachable_gateway_is_retried_then_session_is_saved_with_resume_instructions(make_rig):
    rig = make_rig([{"tool_calls": [call("get_working_dir")]}] + [{"status": 503}] * 3, retries=3)
    result = rig.agent.run("go")
    assert result.stop == "error" and f"pinch --resume {rig.session.id}" in result.text
    assert len(rig.sent()) == 4                              # 1 good call + 3 tries
    assert [m["role"] for m in rig.session.messages()] == ["user", "assistant", "tool"]


# -- failover notice (Bifrost shapes) ----------------------------------------------------------------

def bifrost_reply(provider, primary=None, **kw):
    info = {"provider": provider, "model": "mock-small"}
    if primary:
        info.update(is_fallback=True, primary_provider=primary, primary_model="mock-small")
    return {"extra_fields": {"routing_info": info}, **kw}


def test_notice_names_both_providers_once_and_is_recorded_in_session_and_ledger(make_rig):
    rig = make_rig([
        {"tool_calls": [call("get_working_dir")], **bifrost_reply("cloud-a")},
        {"tool_calls": [call("get_working_dir")], **bifrost_reply("cloud-b", primary="cloud-a")},
        {"tool_calls": [call("get_working_dir")], **bifrost_reply("cloud-b", primary="cloud-a")},
        {"content": "done", **bifrost_reply("cloud-b", primary="cloud-a")},
    ])
    rig.agent.run("go")
    notices = [line for line in rig.said if line.startswith("Notice")]
    assert len(notices) == 1
    assert "cloud-a did not answer" in notices[0] and "cloud-b (model mock-small) took over" in notices[0]
    event = rig.session_events("provider_changed")[0]
    assert event["step"] == 2 and event["primary_provider"] == "cloud-a" and event["is_fallback"]
    rows = [r for r in rig.session.records() if r["type"] == "ledger"]
    assert [(r["provider"], r["is_fallback"], r["primary_provider"]) for r in rows] == [
        ("cloud-a", False, None), ("cloud-b", True, "cloud-a"), ("cloud-b", True, "cloud-a"), ("cloud-b", True, "cloud-a")]


def test_notice_when_the_very_first_answer_is_from_a_fallback(make_rig):
    rig = make_rig([{"content": "done", **bifrost_reply("local", primary="cloud-a")}])
    rig.agent.run("go")
    assert any("cloud-a did not answer" in line and "local" in line for line in rig.said)


def test_client_reads_routing_headers_when_the_body_has_no_extra_fields():
    from pinch.client import _reply_from
    data = {"model": "m", "choices": [{"message": {"content": "x"}}]}
    headers = {"x-bifrost-routing-info-provider": "cloud-b", "x-bifrost-routing-info-model": "m2",
               "x-bifrost-routing-info-is-fallback": "true", "x-bifrost-routing-info-primary-provider": "cloud-a"}
    reply = _reply_from(data, data["choices"][0], data["choices"][0]["message"], "m", 1, headers)
    assert (reply.provider, reply.served_model, reply.is_fallback, reply.primary_provider) == ("cloud-b", "m2", True, "cloud-a")


def test_ledger_summary_marks_fallback_answers(make_rig, capsys):
    from pinch.cli import main
    rig = make_rig([{"content": "done", **bifrost_reply("cloud-b", primary="cloud-a")}])
    rig.agent.run("go")
    main(["ledger", rig.session.id, "--session-dir", str(rig.cfg.session_dir)])
    out = capsys.readouterr().out
    assert "cloud-b*" in out and "step 1 from cloud-a" in out


# -- a separate model name for the local tier (real providers: cloud model names are blocked on the local-only key) ----------

def test_confidential_repo_sends_the_local_model_name_and_never_escalates_to_the_cloud_model(make_rig, repo):
    (repo / MARKER_NAME).write_text("")
    rig = make_rig([{"content": "ok"}], gateway_key="user-key", confidential_key="local-key", key_header="x-bf-vk", savings="all",
                   model="gemini/small", small_model="gemini/small", big_model="gemini/big", local_model="local/gemma4")
    rig.agent.run("go")
    assert rig.mock.recorded[0]["body"]["model"] == "local/gemma4"
    assert rig.agent.cascade.active is False or "cascade" not in rig.agent.on


def test_budget_choice_l_switches_to_the_local_model_name(make_rig):
    rig = make_rig([{"content": "answered locally"}], gateway=FakeGateway(budget=Budget(0.01, 0.0099)), max_tokens=1000,
                   confidential_key="local-key", gateway_key="user-key", key_header="x-bf-vk", budget_answers=("l",),
                   model="gemini/small", local_model="local/gemma4")
    rig.agent.run("go")
    assert rig.mock.recorded[0]["body"]["model"] == "local/gemma4" and rig.agent.tier == "local"


def test_without_a_local_model_name_the_model_is_unchanged(make_rig, repo):
    (repo / MARKER_NAME).write_text("")
    rig = make_rig([{"content": "ok"}], confidential_key="local-key", key_header="x-bf-vk", model="mock-small")
    rig.agent.run("go")
    assert rig.mock.recorded[0]["body"]["model"] == "mock-small"
