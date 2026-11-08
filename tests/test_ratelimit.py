"""Rate-limit policy shared by pinch's client and mini-swe-agent's wrapper, and pinch's client behaviour."""

import pytest

from pinch import ratelimit as rl
from pinch.client import ModelClient, ProviderError
from pinch.mock_provider import MockProvider

GEMINI = ("You exceeded your current quota, please check your plan and billing details. * Quota exceeded for metric: "
          "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 15, model: gemini-3.5-flash-lite "
          "Generate content requests per minute limit exceeded on Free Tier ... Please retry in 44.224906789s.")
MSG = [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("text,seconds", [
    (GEMINI, 44.224906789),
    ("Rate limit reached ... Please try again in 7.7925s. Need more tokens?", 7.7925),
    ("try again in 1m30.5s", 90.5),                  # Groq writes minutes and seconds together
    ('{"retryDelay": "44s"}', 44.0),
    ("retry after 250ms", 0.25),
    ("nothing useful here", None),
])
def test_retry_delay_is_read_from_the_message(text, seconds):
    got = rl.retry_delay(text)
    assert (got is None) if seconds is None else abs(got - seconds) < 0.01


def test_retry_after_header_wins_over_the_message():
    assert rl.retry_delay(GEMINI, "12") == 12.0 and rl.retry_delay("", "bad") is None


def test_daily_markers_mean_quota_everything_else_waits():
    for text in ("requests per day", "PerDay", "Requests Per Day limit", "daily limit", "tokens per day (TPD)"):
        assert rl.classify_429(text) == "quota" and rl.is_daily_limit(text)
    for text in (GEMINI, "requests per minute", "PerMinute", "tokens per minute (TPM)", "", "something unknown"):
        assert rl.classify_429(text) == "rate_limit"


def test_wait_is_the_hint_with_jitter_and_a_cushion_and_never_more_than_the_cap():
    low = rl.wait_seconds(GEMINI, rng=lambda: 0.0)
    high = rl.wait_seconds(GEMINI, rng=lambda: 1.0)
    assert 45.2 < low < high < 50.8
    assert rl.wait_seconds("retry in 900s") == rl.MAX_WAIT == 120.0
    assert rl.wait_seconds("", None, 3, 1.0, rng=lambda: 0.0) == 4.0                      # no hint: exponential backoff
    assert rl.wait_seconds("", None, 20, 1.0) == rl.MAX_WAIT


# -- pinch's client ---------------------------------------------------------------------------------

@pytest.fixture
def mock(tmp_path):
    made = []

    def make(script):
        m = MockProvider(script, tmp_path / "m.jsonl").start()
        made.append(m)
        return m
    yield make
    for m in made:
        m.stop()


def client(m, sleeps, events, **kw):
    return ModelClient(m.url, "gemini/small", "k", "x-bf-vk", 50, retries=2, backoff=0.0, sleep=sleeps.append,
                       on_event=lambda name, **d: events.append((name, d)), **kw)


def test_the_client_waits_out_the_providers_delay_and_records_it(mock):
    m = mock([{"status": 429, "message": GEMINI}, {"status": 429, "message": GEMINI}, {"content": "ok"}])
    sleeps, events = [], []
    reply = client(m, sleeps, events).chat(MSG)
    assert reply.message["content"] == "ok" and len(m.recorded) == 3 and reply.rate_limit_waits == 2
    assert len(sleeps) == 2 and all(45 < s < 51 for s in sleeps) and abs(reply.waited - sum(sleeps)) < 1e-6
    assert [e[0] for e in events] == ["rate_limit_wait"] * 2 and events[0][1]["provider"] == "gemini" and events[0][1]["of"] == 8


def test_rate_limit_waits_do_not_use_up_the_other_error_budget(mock):
    m = mock([{"status": 429, "message": "slow"}] * 3 + [{"status": 500, "message": "boom"}, {"content": "ok"}])
    c = client(m, [], [])
    assert c.chat(MSG).message["content"] == "ok" and len(m.recorded) == 5            # retries=2: one 500 retry still allowed after three 429s


def test_after_eight_waits_it_gives_up_with_the_rate_limit_error(mock):
    m = mock([{"status": 429, "message": "slow down", "times": 20}])
    sleeps = []
    with pytest.raises(ProviderError) as info:
        client(m, sleeps, []).chat(MSG)
    assert info.value.kind == "rate_limit" and len(m.recorded) == 9 and len(sleeps) == 8


def test_a_per_day_429_is_not_waited_for(mock):
    m = mock([{"status": 429, "message": "Quota exceeded: requests per day limit: 1500"}, {"content": "never"}])
    sleeps = []
    with pytest.raises(ProviderError) as info:
        client(m, sleeps, []).chat(MSG)
    assert info.value.kind == "quota" and len(m.recorded) == 1 and sleeps == []


def test_the_agent_says_it_out_loud_and_writes_the_event(make_rig):
    rig = make_rig([{"status": 429, "message": "Please retry in 0.01s"}, {"content": "done"}], model="gemini/small", retry_backoff=0.0)
    rig.agent.client._sleep = lambda s: None
    assert rig.agent.run("go").text == "done"
    assert rig.session_events("rate_limit_wait")
    line = next(x for x in rig.said if x.startswith("Rate limited by gemini"))
    assert "waiting" in line and "Your session is saved" in line
    ledger = [r for r in rig.agent.session.records() if r["type"] == "ledger"][0]
    assert ledger["rate_limit_waits"] == 1 and ledger["wait_seconds"] > 0


def test_pacing_spaces_requests_and_counts_as_waiting(mock):
    m = mock([{"content": "a"}, {"content": "b"}])
    sleeps = []
    c = ModelClient(m.url, "mock-small", "k", "x-bf-vk", 50, sleep=sleeps.append, min_call_interval=3.0)
    c.chat(MSG)
    second = c.chat(MSG)
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 3.0 and abs(second.waited - sleeps[0]) < 1e-6


def test_no_pacing_by_default(mock):
    m = mock([{"content": "a"}, {"content": "b"}])
    sleeps = []
    c = ModelClient(m.url, "mock-small", "k", "x-bf-vk", 50, sleep=sleeps.append)
    c.chat(MSG)
    assert c.chat(MSG).waited == 0 and sleeps == []


def test_config_reads_the_interval_from_the_environment(monkeypatch, tmp_path):
    from pinch.config import load_config
    monkeypatch.setenv("PINCH_MIN_CALL_INTERVAL", "4.5")
    monkeypatch.setenv("PINCH_ENV_FILE", str(tmp_path / "none"))
    assert load_config(tmp_path / "c.toml").min_call_interval == 4.5


def test_mini_wrapper_uses_the_same_policy_and_pace():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src/pinch/bench/container/mini_run.py").read_text()
    assert "from pinch import ratelimit" in src and "ratelimit.is_daily_limit" in src and "ratelimit.wait_seconds" in src
    assert "ratelimit.RATE_LIMIT_RETRIES" in src and 'spec.get("min_call_interval")' in src
    assert "wait_seconds" in src.split("finally:")[1]          # the result file reports the waits


def test_summary_times_exclude_waits_and_say_so():
    from pinch.bench.summary import active_s, methodology
    assert active_s({"agent_s": 100.0, "wait_seconds": 40.0}) == 60.0 and active_s({"agent_s": 5.0}) == 5.0
    row = {"agent": "pinch", "model": "m", "wait_seconds": 12.0, "rate_limit_waits": 2, "auto_approvals": 0}
    text = methodology([{"min_call_interval": 4.5}], [row], [], False)
    assert "at least 4.5 s between model requests" in text and "2 rate-limit waits" in text and "exclude those waits" in text and "`--jobs` above 1" in text
