import subprocess
import sys

from helpers import call, fix_calc_script


def test_loop_completes_a_scripted_task_in_a_git_repo(make_rig, repo):
    rig = make_rig(fix_calc_script(), answers=("y", "y"))
    result = rig.agent.run("Fix the failing test in this repo.")

    assert result.stop == "done"
    assert "Fixed add()" in result.text
    assert "a + b" in (repo / "calc.py").read_text()
    # the fix really works, checked outside the agent
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=repo, capture_output=True, text=True)
    assert tests.returncode == 0, tests.stdout
    # git sees the change
    assert "calc.py" in subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout
    # one model call per scripted response, and the results were fed back
    assert len(rig.sent()) == 4
    assert any("a - b" in str(m.get("content")) for m in rig.sent()[1]["messages"] if m["role"] == "tool")


def test_reads_run_without_approval(make_rig):
    rig = make_rig([
        {"tool_calls": [call("read_file", file_path="calc.py"), call("list_directory", dir_path="."),
                        call("grep_search", pattern="def add"), call("get_working_dir")]},
        {"content": "ok"},
    ])
    result = rig.agent.run("look around")
    assert result.stop == "done"
    assert rig.asked == []
    tool_msgs = [m for m in rig.agent.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 4 and not any(m["content"].startswith("Error") for m in tool_msgs)


def test_step_limit_stops_the_loop(make_rig):
    rig = make_rig([{"tool_calls": [call("get_working_dir")]}] * 10, max_steps=3)
    result = rig.agent.run("loop forever")
    assert result.stop == "step_limit" and "--resume" in result.text
    assert len(rig.sent()) == 3


def test_rate_limit_stops_cleanly_and_keeps_the_session(make_rig):
    rig = make_rig([{"tool_calls": [call("get_working_dir")]}, {"status": 429, "message": "slow down"}])
    result = rig.agent.run("task")
    assert result.stop == "error"
    assert "429" in result.text and f"pinch --resume {rig.session.id}" in result.text
    assert rig.session_events("model_error")[0]["status"] == 429
    assert [m["role"] for m in rig.session.messages()] == ["user", "assistant", "tool"]


def test_notice_when_a_different_provider_answers(make_rig):
    rig = make_rig([
        {"tool_calls": [call("get_working_dir")], "provider": "mock-a", "model": "mock-small"},
        {"content": "done", "provider": "mock-b", "model": "mock-large"},
    ])
    rig.agent.run("task")
    assert any("changed from mock-a (model mock-small) to mock-b (model mock-large)" in line for line in rig.said)
    assert rig.session_events("provider_changed")
