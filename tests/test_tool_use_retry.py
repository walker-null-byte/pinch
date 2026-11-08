"""A provider 400 `tool_use_failed` (the model wrote a malformed tool call) is retried, up to 2 more times, and recorded."""

import pytest

from pinch.client import ModelClient, ProviderError
from pinch.mock_provider import MockProvider

MSG = [{"role": "user", "content": "hi"}]
BAD = {"status": 400, "message": "Tool call validation failed: code tool_use_failed, failed_generation {...}"}


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


def client(m, events):
    return ModelClient(m.url, "mock-small", "k", "x-bf-vk", 50, retries=1, backoff=0.0, sleep=lambda s: None,
                       on_event=lambda name, **data: events.append((name, data)))


def test_a_malformed_tool_call_is_retried_and_recorded(mock):
    events = []
    m = mock([BAD, BAD, {"content": "fine now"}])
    reply = client(m, events).chat(MSG)
    assert reply.message["content"] == "fine now" and len(m.recorded) == 3 and reply.attempts == 3
    assert [(n, d["attempt"], d["of"]) for n, d in events] == [("tool_use_failed_retry", 1, 2), ("tool_use_failed_retry", 2, 2)]


def test_after_two_more_tries_it_gives_up_with_a_clear_kind(mock):
    events = []
    m = mock([BAD, BAD, BAD, {"content": "never reached"}])
    with pytest.raises(ProviderError) as info:
        client(m, events).chat(MSG)
    assert info.value.kind == "tool_use_failed" and info.value.status == 400 and len(m.recorded) == 3 and len(events) == 2


def test_other_400s_are_not_retried(mock):
    m = mock([{"status": 400, "message": "context length exceeded"}, {"content": "never reached"}])
    with pytest.raises(ProviderError) as info:
        client(m, []).chat(MSG)
    assert info.value.kind == "other" and len(m.recorded) == 1


def test_the_gateway_retry_budget_still_works_beside_it(mock):
    m = mock([{"status": 500, "message": "boom"}, BAD, {"content": "ok"}])
    c = ModelClient(m.url, "mock-small", "k", "x-bf-vk", 50, retries=2, backoff=0.0, sleep=lambda s: None)
    assert c.chat(MSG).message["content"] == "ok" and len(m.recorded) == 3


def test_the_agent_writes_the_retry_to_the_session(make_rig):
    rig = make_rig([BAD, {"content": "done"}])
    rig.agent.run("go")
    assert rig.session_events("tool_use_failed_retry")
