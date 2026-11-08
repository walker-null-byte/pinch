"""Full mode sends every tool schema. Lazy mode sends only TurboMCP's 3 meta-tools."""

import json

from helpers import call
from pinch.tools import ToolBox


def measure(rig) -> dict:
    body = rig.sent()[0]
    tools_json = json.dumps(body["tools"], separators=(",", ":"))
    return {
        "tools": len(body["tools"]),
        "schema_bytes": len(tools_json),
        "schema_tokens_est": len(tools_json) // 4,            # about 4 characters per token
        "request_bytes": len(json.dumps(body, separators=(",", ":"))),
        "prompt_tokens_reported": next(r["input_tokens"] for r in rig.session.records() if r["type"] == "ledger"),
    }


def test_full_mode_sends_far_more_schema_than_lazy_mode(make_rig, capsys):
    results = {}
    for mode in ("full", "lazy"):
        rig = make_rig([{"content": "ok"}], mode=mode)
        rig.agent.run("say ok")
        results[mode] = measure(rig)

    full, lazy = results["full"], results["lazy"]
    with capsys.disabled():
        print("\n  tool-schema size per request (about 4 chars per token)")
        print(f"  {'mode':<6}{'tools':>6}{'schema bytes':>14}{'~schema tokens':>16}{'request bytes':>15}{'prompt tokens (mock est.)':>27}")
        for mode, r in results.items():
            print(f"  {mode:<6}{r['tools']:>6}{r['schema_bytes']:>14}{r['schema_tokens_est']:>16}{r['request_bytes']:>15}{r['prompt_tokens_reported']:>27}")
        print(f"  full / lazy schema size: {full['schema_bytes'] / lazy['schema_bytes']:.1f}x\n")

    assert full["tools"] == 34 and lazy["tools"] == 3
    assert full["schema_bytes"] > 8 * lazy["schema_bytes"]
    assert full["prompt_tokens_reported"] > lazy["prompt_tokens_reported"]


def test_lazy_mode_exposes_exactly_the_three_meta_tools(repo):
    names = [t["function"]["name"] for t in ToolBox(repo, "lazy").schemas()]
    assert names == ["search_tools", "get_tool", "call_tool"]


def test_lazy_discovery_flow_works_end_to_end(make_rig):
    rig = make_rig([
        {"tool_calls": [{"name": "search_tools", "arguments": {"query": "read a file", "top_n": 2}}]},
        {"tool_calls": [{"name": "get_tool", "arguments": {"name": "read_file"}}]},
        {"tool_calls": [call("read_file", file_path="calc.py")]},
        {"content": "done"},
    ])
    rig.agent.run("go")
    tool_msgs = [m["content"] for m in rig.agent.messages if m["role"] == "tool"]
    assert "read_file" in tool_msgs[0] and "input_schema" in tool_msgs[1] and "a - b" in tool_msgs[2]
    assert rig.asked == []


def test_tools_are_jailed_to_the_repo_by_coding_tools_itself(make_rig, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    rig = make_rig([{"tool_calls": [call("read_file", file_path=str(outside))]}, {"content": "done"}])
    rig.agent.run("go")
    result = [m for m in rig.agent.messages if m["role"] == "tool"][0]["content"]
    assert "outside CODING_MCP_ROOT" in result and "private" not in result


def test_a_second_toolbox_gets_its_own_jail(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("x")
    a, b = ToolBox(repo, "full"), ToolBox(other, "full")
    assert "outside" in a.execute(a.parse_call("read_file", {"file_path": str(other / "x.txt")}))
    assert '"content":"x"' in b.execute(b.parse_call("read_file", {"file_path": str(other / "x.txt")}))
