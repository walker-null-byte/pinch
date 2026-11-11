from pinch.client import outgoing


def test_assistant_tool_calls_are_sent_with_only_the_request_fields():
    """A reply's tool calls carry `index`; resent through Bifrost to Groq after a failover that history was refused (HTTP 502)."""
    history = [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x = 1"},
    ]
    sent = outgoing(history)
    assert sent[1]["tool_calls"] == [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]
    assert sent[0] is history[0] and sent[2] is history[2]  # other messages pass through untouched
    assert "index" in history[1]["tool_calls"][0]  # the session's own copy is not changed


def test_clean_history_is_passed_through_unchanged():
    history = [{"role": "assistant", "content": "done"},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}]
    sent = outgoing(history)
    assert sent[0] is history[0] and sent[1] is history[1]


def test_a_gateway_5xx_is_retried_with_growing_waits_and_each_wait_is_reported():
    """Bifrost reports a fully failed fallback chain as its first provider's error (e.g. 502), even when the provider that took
    over only said "try again in 6 s". Five tries with 2, 4, 8, 16 s waits ride that out, and every wait is told to the user."""
    from pinch.client import ModelClient
    from pinch.mock_provider import MockProvider
    events, sleeps = [], []
    script = [{"status": 502}] * 3 + [{"content": "done"}]
    with MockProvider(script) as mock:
        c = ModelClient(mock.url, "m", retries=5, backoff=2.0, sleep=sleeps.append, on_event=lambda n, **d: events.append((n, d)))
        assert c.chat([{"role": "user", "content": "hi"}]).message["content"] == "done"
    assert sleeps == [2.0, 4.0, 8.0]
    assert [d["attempt"] for n, d in events if n == "server_retry"] == [1, 2, 3]
    assert all(d["of"] == 4 and d["status"] == 502 for n, d in events if n == "server_retry")
