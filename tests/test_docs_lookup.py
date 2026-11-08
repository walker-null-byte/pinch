"""Tavily docs_lookup, against a mock Tavily server."""

import json

import pytest

from helpers import call
from pinch.approval import READ, classify
from pinch.ledger import summarize
from pinch.mock_provider import MockTavily
from pinch.tavily import DocsLookup, MAX_QUERY, MAX_SNIPPET, clean_query
from pinch.tools import ToolBox


@pytest.fixture
def tavily():
    with MockTavily() as server:
        yield server


def results(n=5):
    return [{"title": f"Result {i}", "url": f"https://docs.example.com/{i}", "content": "word " * 200, "score": 0.9} for i in range(n)]


def test_request_shape_is_basic_depth_three_results_bearer_auth(tavily):
    docs = DocsLookup("tvly-test-key", tavily.url)
    docs("TypeError: foo() got an unexpected keyword argument 'bar' in libx")
    request = tavily.requests[0]
    assert request["path"] == "/search" and request["authorization"] == "Bearer tvly-test-key"
    assert request["body"]["search_depth"] == "basic" and request["body"]["max_results"] == 3 and request["body"]["include_usage"] is True


def test_at_most_three_results_each_trimmed_with_a_source_url():
    with MockTavily(results()) as server:
        text = DocsLookup("k", server.url)("libx error")
    lines = text.splitlines()
    assert len(lines) == 3
    for i, line in enumerate(lines, 1):
        assert line.startswith(f"{i}. Result {i - 1}") and line.endswith(f"(https://docs.example.com/{i - 1})")
        assert len(line) < 100 + MAX_SNIPPET + 60


def test_queries_are_one_line_capped_and_free_of_code_fences_and_key_like_tokens():
    messy = "KeyError: 'x'\nin libfoo\n```python\nsecret_code()\n```\nkey sk-abcdefghijklmnopqrstuvwx and tvly-abcdefghijklmnop1234 " + "z" * 500
    cleaned = clean_query(messy)
    assert "\n" not in cleaned and len(cleaned) <= MAX_QUERY and "secret_code" not in cleaned
    assert "sk-abcdef" not in cleaned and "tvly-abcdef" not in cleaned and cleaned.startswith("KeyError: 'x' in libfoo")


def test_results_are_cached_per_session_and_credits_are_recorded(tavily):
    calls = []
    docs = DocsLookup("k", tavily.url, on_call=lambda **kw: calls.append(kw))
    first, second = docs("libx   error\nmessage"), docs("libx error message")
    assert first == second and len(tavily.requests) == 1
    assert calls == [dict(query="libx error message", credits=1, cached=False, results=1),
                     dict(query="libx error message", credits=0, cached=True, results=0)]


def test_service_errors_come_back_as_plain_text():
    with MockTavily(status=401) as server:
        assert DocsLookup("bad", server.url)("q").startswith("Error: the docs service answered HTTP 401")
    assert DocsLookup("k", "http://127.0.0.1:9")("q").startswith("Error: could not reach the docs service")
    assert DocsLookup("k", "http://x")("   ").startswith("Error: give a short query")


def test_docs_lookup_is_a_read_tool_that_runs_without_approval_and_credits_reach_the_ledger(make_rig, tavily):
    rig = make_rig([{"tool_calls": [call("docs_lookup", query="libx raises ValueError on connect")]},
                    {"tool_calls": [call("docs_lookup", query="libx raises ValueError on connect")]}, {"content": "ok"}],
                   savings="docs", tavily_key="tvly-x", tavily_url=tavily.url)
    rig.agent.run("go")
    assert classify("docs_lookup") == READ and rig.asked == []
    assert "https://docs.example.com/a" in [m for m in rig.agent.messages if m["role"] == "tool"][0]["content"]
    rows = [r for r in rig.session.records() if r["type"] == "tavily"]
    assert [(r["credits"], r["cached"]) for r in rows] == [(1, False), (0, True)]
    assert "docs_lookup: 2 calls, 1 Tavily credits (1 answered from the session cache)" in summarize(rig.session)


def test_the_tool_is_offered_in_full_mode_and_found_by_search_in_lazy_mode(make_rig, tavily, repo):
    docs = DocsLookup("k", tavily.url)
    full, lazy = ToolBox(repo, "full", docs=docs), ToolBox(repo, "lazy", docs=docs)
    assert "docs_lookup" in [t["function"]["name"] for t in full.schemas()]
    assert "docs_lookup" not in [t["function"]["name"] for t in lazy.schemas()]            # lazy shows only the 3 meta-tools
    assert any(t["name"] == "docs_lookup" for t in lazy.registry.search_tools("look up documentation error message")["tools"])
    assert "docs_lookup" not in ToolBox(repo, "full").registry.tools                       # off without a key


def test_switched_off_when_the_technique_is_off_or_there_is_no_key(make_rig, tavily):
    for kwargs in ({"savings": "none", "tavily_key": "k"}, {"savings": "docs", "tavily_key": ""}):
        rig = make_rig([{"tool_calls": [call("docs_lookup", query="x")]}, {"content": "ok"}], tavily_url=tavily.url, mode="full", **kwargs)
        rig.agent.run("go")
        assert "docs_lookup" not in json.dumps(rig.sent()[0]["tools"])
        assert "switched off" in [m for m in rig.agent.messages if m["role"] == "tool"][0]["content"]
    assert tavily.requests == []


def test_confidential_repos_never_get_docs_lookup_and_a_call_is_refused(make_rig, tavily, repo):
    (repo / ".pinch-confidential").write_text("")
    rig = make_rig([{"tool_calls": [call("docs_lookup", lazy=False, query="libx error")]}, {"content": "ok"}],
                   savings="all", mode="full", tavily_key="tvly-x", tavily_url=tavily.url)
    rig.agent.run("go")
    assert "docs_lookup" not in json.dumps(rig.sent()[0]["tools"])                         # not offered
    refusal = [m for m in rig.agent.messages if m["role"] == "tool"][0]["content"]
    assert "switched off for this repo" in refusal and tavily.requests == []               # refused; nothing left the machine
    via_lazy = make_rig([{"tool_calls": [call("docs_lookup", query="libx error")]}, {"content": "ok"}], savings="all",
                        tavily_key="tvly-x", tavily_url=tavily.url, repo_path=repo)
    via_lazy.agent.run("go")
    assert "switched off" in [m for m in via_lazy.agent.messages if m["role"] == "tool"][0]["content"] and tavily.requests == []


def test_even_if_someone_builds_it_enabled_the_function_refuses_when_disabled(tavily):
    docs = DocsLookup("k", tavily.url, enabled=False)
    assert docs("query").startswith("Error: docs_lookup is switched off") and tavily.requests == []


def test_key_name_is_documented():
    from pathlib import Path
    assert "\nTAVILY_API_KEY=" in (Path(__file__).resolve().parents[1] / ".env.example").read_text()
