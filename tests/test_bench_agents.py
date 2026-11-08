"""Reading results back from the two agents: tokens come only from reported usage, and errors are classified."""

import json

import pytest

from pinch.bench import agents
from pinch.bench.agents import classify_error, parse_mini, parse_pinch
from pinch.session import Session


def pinch_session(out, ledger, events=(), messages=()):
    s = Session.create(out / "sessions", {"repo": "/work"})
    for r in ledger:
        s.append("ledger", **{"step": 1, "model": "m", "served_model": "m", "provider": "p", "finish_reason": "stop", "usage_reported": True,
                              "cache_hit": False, "cache_saved_tokens": 0, "cost_usd": 0.001, **r})
    for name, data in events:
        s.append("event", event=name, **data)
    for m in messages:
        s.append("message", message=m)
    return s


def test_pinch_tokens_are_summed_from_the_ledger(tmp_path):
    pinch_session(tmp_path, [{"input_tokens": 1000, "output_tokens": 50, "cached_tokens": 800},
                             {"input_tokens": 1200, "output_tokens": 70, "cached_tokens": 900, "cost_usd": 0.002}],
                  events=[("savings", {"techniques": ["context", "trim"]}), ("auto_approved", {"tool": "edit_file", "kind": "write"}),
                          ("auto_approved", {"tool": "run_test", "kind": "shell"})])
    o = parse_pinch(tmp_path, 0, "")
    assert (o.input_tokens, o.output_tokens, o.cached_tokens, o.calls) == (2200, 120, 1700, 2)
    assert o.total_tokens == 2320 and o.cost_usd == pytest.approx(0.003)
    assert o.auto_approvals == 2 and o.techniques_on == ["context", "trim"] and o.stop == "done" and o.infra is None and o.usage_complete


def test_pinch_pretest_status_is_read_from_the_session(tmp_path):
    pinch_session(tmp_path, [{"input_tokens": 10, "output_tokens": 1, "cached_tokens": 0}], events=[("pretest", {"status": "red", "returncode": 1})])
    assert parse_pinch(tmp_path, 0, "").pretest == "red"
    other = tmp_path / "o"
    pinch_session(other, [{"input_tokens": 10, "output_tokens": 1, "cached_tokens": 0}], events=[("pretest", {"status": "skipped", "reason": "timeout"})])
    assert parse_pinch(other, 0, "").pretest == "skipped:timeout"


def test_pinch_cache_hits_are_not_billed_and_missing_usage_is_flagged(tmp_path):
    pinch_session(tmp_path, [{"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_hit": True, "cache_saved_tokens": 900},
                             {"input_tokens": 100, "output_tokens": 5, "cached_tokens": 0, "usage_reported": False, "cost_usd": None}])
    o = parse_pinch(tmp_path, 0, "")
    assert o.cache_hits == 1 and o.cache_saved_tokens == 900 and o.usage_complete is False and o.cost_usd is None


@pytest.mark.parametrize("event,reason", [
    ({"kind": "rate_limit", "status": 429, "error": "slow down"}, "rate_limit"),
    ({"kind": "server", "status": 503, "error": "overloaded"}, "provider_unavailable"),
    ({"kind": "unreachable", "status": None, "error": "refused"}, "provider_unavailable"),
    ({"kind": "budget", "status": 402, "error": "budget_exceeded"}, "budget"),
    ({"kind": "auth", "status": 401, "error": "bad key"}, "config"),
    ({"kind": "rate_limit", "status": 429, "error": "You exceeded your current quota"}, "rate_limit"),   # unknown 429: wait, do not stop
    ({"kind": "rate_limit", "status": 429, "error": "Quota exceeded: requests per day limit: 1500"}, "quota"),
    ({"kind": "other", "status": 400, "error": "maximum context length is 8192 tokens"}, None),
])
def test_pinch_model_errors_are_infrastructure_unless_the_agent_overflowed(tmp_path, event, reason):
    pinch_session(tmp_path, [{"input_tokens": 10, "output_tokens": 1, "cached_tokens": 0}], events=[("model_error", event)])
    o = parse_pinch(tmp_path, 1, "")
    assert o.infra == reason
    assert o.stop == ("context_overflow" if reason is None else "error")


def test_pinch_step_limit_and_missing_session(tmp_path):
    pinch_session(tmp_path, [{"input_tokens": 10, "output_tokens": 1, "cached_tokens": 0}], events=[("step_limit", {"max_steps": 30})])
    assert parse_pinch(tmp_path, 1, "").stop == "step_limit"
    empty = tmp_path / "other"
    empty.mkdir()
    o = parse_pinch(empty, 2, "Traceback ...")
    assert o.infra == "harness" and "no session" in o.infra_detail


def write_traj(out, usages, status="Submitted", error=""):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for u in usages:
        msgs.append({"role": "assistant", "content": "x", "extra": {"response": {"model": "mock-small", **({"usage": u} if u is not None else {})}}})
        msgs.append({"role": "tool", "content": "{}"})
    (out / "traj.json").write_text(json.dumps({"messages": msgs, "info": {"submission": ""}}))
    (out / "mini_result.json").write_text(json.dumps({"exit_status": status, "error_type": "" if status == "Submitted" else status, "error": error}))


def test_mini_tokens_come_from_the_stored_responses(tmp_path):
    write_traj(tmp_path, [{"prompt_tokens": 500, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 100}},
                          {"prompt_tokens": 600, "completion_tokens": 30, "prompt_tokens_details": None}])
    o = parse_mini(tmp_path, 0, "")
    assert (o.input_tokens, o.output_tokens, o.cached_tokens, o.calls, o.stop) == (1100, 50, 100, 2, "done")
    agents.fill_cost(o, "mock-small")
    assert o.cost_usd == pytest.approx((1000 * 1.0 + 100 * 0.10 + 50 * 4.0) / 1e6)   # priced from prices.toml like pinch's calls


def test_mini_reply_without_usage_marks_the_run_incomplete(tmp_path):
    write_traj(tmp_path, [{"prompt_tokens": 500, "completion_tokens": 20}, None])
    o = parse_mini(tmp_path, 0, "")
    assert o.usage_complete is False and o.calls == 2
    agents.fill_cost(o, "mock-small")
    assert o.cost_usd is None


@pytest.mark.parametrize("status,error,stop,infra", [
    ("LimitsExceeded", "", "step_limit", None),
    ("TimeExceeded", "", "timeout", None),
    ("RateLimitError", "litellm.RateLimitError: 429 Too Many Requests", "error", "rate_limit"),
    ("InternalServerError", "status 503 upstream", "error", "provider_unavailable"),
    ("AuthenticationError", "invalid key 401", "error", "config"),
    ("BadRequestError", "maximum context length exceeded", "context_overflow", None),
])
def test_mini_exit_statuses(tmp_path, status, error, stop, infra):
    write_traj(tmp_path, [{"prompt_tokens": 5, "completion_tokens": 1}], status=status, error=error)
    o = parse_mini(tmp_path, 1, "")
    assert (o.stop, o.infra) == (stop, infra)


def test_classify_error_table():
    assert classify_error("", 429, "") == "rate_limit"
    assert classify_error("", 429, "Resource has been exhausted (e.g. check quota)") == "rate_limit"   # unknown 429s wait
    assert classify_error("", 403, "insufficient_quota") == "quota"
    assert classify_error("", 402, "") == "budget" and classify_error("", 400, "budget_exceeded for key") == "budget"
    assert classify_error("", 404, "model not found") == "config"
    assert classify_error("", 500, "") == "provider_unavailable"
    assert classify_error("", 400, "invalid tool schema") == "bad_request"
    assert classify_error("", 400, "This model's maximum context length is 128000 tokens") is None


GEMINI_PER_MINUTE = ("You exceeded your current quota, please check your plan and billing details. * Quota exceeded for metric: "
                     "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 15, model: gemini-3.5-flash-lite "
                     "Generate content requests per minute limit exceeded on Free Tier ... Please retry in 44.224906789s. [RESOURCE_EXHAUSTED]")
GEMINI_PER_DAY = GEMINI_PER_MINUTE.replace("requests per minute limit", "requests per day limit").replace("generate_content_free_tier_requests", "generate_content_free_tier_requests PerDay")


def test_geminis_per_minute_quota_message_is_a_rate_limit_to_wait_out_not_a_quota_stop():
    assert classify_error("rate_limit", 429, GEMINI_PER_MINUTE) == "rate_limit"
    assert classify_error("RateLimitError", None, GEMINI_PER_MINUTE) == "rate_limit"      # mini-swe-agent's error type, no status


def test_a_per_day_limit_is_the_quota_stop():
    for text in (GEMINI_PER_DAY, "limit exceeded: requests per day", "Quota exceeded for metric ... PerDay", "daily limit reached"):
        assert classify_error("rate_limit", 429, text) == "quota", text
    assert classify_error("RateLimitError", None, GEMINI_PER_DAY) == "quota"


def test_pinch_session_with_geminis_per_minute_error_is_not_fatal(tmp_path):
    pinch_session(tmp_path, [{"input_tokens": 10, "output_tokens": 1, "cached_tokens": 0}],
                  events=[("model_error", {"kind": "rate_limit", "status": 429, "error": GEMINI_PER_MINUTE})])
    assert parse_pinch(tmp_path, 1, "").infra == "rate_limit"


def test_mini_with_geminis_per_minute_error_is_not_fatal(tmp_path):
    write_traj(tmp_path, [{"prompt_tokens": 5, "completion_tokens": 1}], status="RateLimitError", error="litellm.RateLimitError: " + GEMINI_PER_MINUTE)
    assert parse_mini(tmp_path, 1, "").infra == "rate_limit"
