"""The mock provider and the model client, including error codes and recorded requests."""

import json

import pytest

from pinch.client import ModelClient, ProviderError
from pinch.mock_provider import MockProvider, load_script

MSG = [{"role": "user", "content": "hi"}]


def test_mock_replays_content_and_tool_calls_with_usage():
    script = [{"tool_calls": [{"name": "read_file", "arguments": {"file_path": "a.py"}}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 7, "cached_tokens": 60}, "provider": "mock-a", "model": "m1"},
              {"content": "bye"}]
    with MockProvider(script) as mock:
        client = ModelClient(mock.url, "mock-small")
        first = client.chat(MSG)
        assert first.message["tool_calls"][0]["function"]["name"] == "read_file"
        assert json.loads(first.message["tool_calls"][0]["function"]["arguments"]) == {"file_path": "a.py"}
        assert (first.usage.prompt_tokens, first.usage.completion_tokens, first.usage.cached_tokens) == (100, 7, 60)
        assert first.provider == "mock-a" and first.served_model == "m1" and first.finish_reason == "tool_calls"
        second = client.chat(MSG)
        assert second.message["content"] == "bye" and second.usage.reported and second.usage.prompt_tokens > 0


@pytest.mark.parametrize("status", [429, 500])
def test_mock_returns_scripted_errors_on_chosen_calls(status):
    script = [{"content": "ok"}, {"status": status, "message": "boom", "headers": {"Retry-After": "7"}}, {"content": "ok again"}]
    with MockProvider(script) as mock:
        client = ModelClient(mock.url, "mock-small")
        assert client.chat(MSG).message["content"] == "ok"
        with pytest.raises(ProviderError) as err:
            client.chat(MSG)
        assert err.value.status == status and "boom" in str(err.value) and err.value.retry_after == "7"
        assert client.chat(MSG).message["content"] == "ok again"


def test_times_repeats_an_entry_and_an_exhausted_script_is_a_500():
    with MockProvider([{"status": 429, "times": 2}, {"content": "ok"}]) as mock:
        client = ModelClient(mock.url, "mock-small")
        for _ in range(2):
            with pytest.raises(ProviderError):
                client.chat(MSG)
        client.chat(MSG)
        with pytest.raises(ProviderError, match="exhausted"):
            client.chat(MSG)


def test_every_request_is_recorded_and_secrets_are_not(tmp_path):
    record = tmp_path / "req.jsonl"
    tools = [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}]
    with MockProvider([{"content": "ok"}, {"status": 500}], record) as mock:
        client = ModelClient(mock.url, "mock-small", gateway_key="sk-very-secret-value")
        client.chat(MSG, tools)
        with pytest.raises(ProviderError):
            client.chat(MSG)
    lines = [json.loads(line) for line in record.read_text().splitlines()]
    assert [r["status"] for r in lines] == [200, 500]
    assert lines[0]["body"]["tools"] == tools and lines[0]["body"]["model"] == "mock-small"
    assert "tools" not in lines[1]["body"]                       # the file proves what was NOT sent, too
    assert "sk-very-secret-value" not in record.read_text()
    assert lines[0]["headers"]["authorization"].startswith("redacted:")


def test_gateway_key_header_name_is_configurable():
    with MockProvider([{"content": "a"}, {"content": "b"}]) as mock:
        ModelClient(mock.url, "m", gateway_key="vk-1", key_header="x-virtual-key").chat(MSG)
        ModelClient(mock.url, "m", gateway_key="vk-1").chat(MSG)
        custom, default = (r["headers"] for r in mock.recorded)
    assert "x-virtual-key" in custom and "authorization" not in custom
    assert "authorization" in default and "x-virtual-key" not in default
    assert custom["x-virtual-key"].removeprefix("redacted:") != "vk-1"


def test_client_reads_provider_fields_from_a_gateway_style_response():
    from pinch.client import _reply_from
    data = {"model": "gpt-4o-mini", "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}},
            "extra_fields": {"provider": "openai", "latency": 120}}
    reply = _reply_from(data, data["choices"][0], data["choices"][0]["message"], "gpt-4o-mini", 5)
    assert reply.provider == "openai" and reply.usage.cached_tokens == 4 and reply.extra["extra_fields"]["latency"] == 120


def test_unreachable_endpoint_is_a_plain_error():
    with pytest.raises(ProviderError, match="Could not reach"):
        ModelClient("http://127.0.0.1:9/v1", "m").chat(MSG)


def test_yaml_scripts_work(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "script.yaml"
    path.write_text("responses:\n  - content: hello from yaml\n  - status: 429\n")
    assert [e.get("content") for e in load_script(path)] == ["hello from yaml", None]
