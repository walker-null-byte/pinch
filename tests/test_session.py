"""Session store and resume."""

import json

from helpers import call
from pinch.session import Session, find_session, missing_tool_results


def test_session_file_is_appended_after_every_step(make_rig):
    rig = make_rig([{"tool_calls": [call("get_working_dir")]}, {"content": "done"}])
    rig.agent.run("hello")
    lines = [json.loads(line) for line in rig.session.path.read_text().splitlines()]
    kinds = [(r["type"], r.get("message", {}).get("role") or r.get("event")) for r in lines]
    assert kinds[0][0] == "meta"
    assert [k for k in kinds if k[0] == "message"] == [("message", "user"), ("message", "assistant"), ("message", "tool"), ("message", "assistant")]
    assert sum(r["type"] == "ledger" for r in lines) == 2


def test_resume_continues_after_a_crash_with_full_history(make_rig, repo):
    first = make_rig([{"tool_calls": [call("read_file", file_path="calc.py")]}, {"status": 500, "message": "upstream down"}])
    result = first.agent.run("Fix the bug in calc.py")
    assert result.stop == "error"

    second = make_rig([{"content": "Picked up where I left off."}], resume=first.session.id)
    assert second.session.path == first.session.path
    result = second.agent.run(None)  # `pinch --resume <id>` with no new task
    assert result.stop == "done" and "Picked up" in result.text

    history = second.sent()[0]["messages"]
    assert history[1] == {"role": "user", "content": "Fix the bug in calc.py"}
    assert any(m["role"] == "tool" and "a - b" in m["content"] for m in history)   # earlier tool result is there
    steps = [r["step"] for r in second.session.records() if r["type"] == "ledger"]
    assert steps == [1, 2]                                                           # numbering continues


def test_resume_can_take_a_new_task(make_rig):
    first = make_rig([{"content": "first answer"}])
    first.agent.run("task one")
    second = make_rig([{"content": "second answer"}], resume=first.session.id)
    second.agent.run("task two")
    roles = [m["role"] for m in second.sent()[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_resume_repairs_a_session_cut_off_between_tool_request_and_result(make_rig):
    first = make_rig([{"tool_calls": [call("get_working_dir")]}])
    first.session.append("message", message={"role": "user", "content": "go"})
    first.session.append("message", message={"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_x", "type": "function", "function": {"name": "get_working_dir", "arguments": "{}"}}]})
    second = make_rig([{"content": "ok"}], resume=first.session.id)
    second.agent.run(None)
    sent = second.sent()[0]["messages"]
    assert sent[-1]["role"] == "tool" and sent[-1]["tool_call_id"] == "call_x" and "did not run" in sent[-1]["content"]
    assert missing_tool_results(second.session.messages()) == []   # the repair was saved, not just applied in memory


def test_resume_remembers_the_last_style_and_the_last_provider(make_rig):
    first = make_rig([{"content": "hi", "provider": "mock-a"}])
    first.agent.run("task")
    first.agent.set_style("tiny")
    assert first.session.last_style() == "tiny"
    second = make_rig([{"content": "hi", "provider": "mock-b"}], resume=first.session.id)
    second.agent.run("again")
    assert any("changed from mock-a" in line for line in second.said)


def test_find_session_by_prefix_and_path(tmp_path):
    session = Session.create(tmp_path, {"repo": "x"})
    assert find_session(tmp_path, session.id[:12]) == session.path
    assert find_session(tmp_path, str(session.path)) == session.path
