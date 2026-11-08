"""Token ledger, the `pinch ledger` command, and the command line end to end."""

import json

from helpers import call, fix_calc_script
from pinch.cli import main
from pinch.ledger import estimate_cost


def test_ledger_records_every_call_with_tokens_and_cost(make_rig):
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 400}
    rig = make_rig([{"tool_calls": [call("get_working_dir")], "usage": usage, "provider": "mock-a"},
                    {"content": "done", "usage": {"prompt_tokens": 1200, "completion_tokens": 10}}])
    rig.agent.run("go")
    rows = [r for r in rig.session.records() if r["type"] == "ledger"]
    assert [r["step"] for r in rows] == [1, 2]
    assert rows[0]["input_tokens"] == 1000 and rows[0]["output_tokens"] == 100 and rows[0]["cached_tokens"] == 400
    assert rows[0]["provider"] == "mock-a" and rows[0]["model"] == "mock-small"
    # mock-small: input 1.00, cached 0.10, output 4.00 per million tokens
    assert abs(rows[0]["cost_usd"] - (600 * 1.00 + 400 * 0.10 + 100 * 4.00) / 1e6) < 1e-12
    assert rows[1]["cached_tokens"] == 0


def test_unknown_model_costs_none_not_zero():
    assert estimate_cost("some-new-model", 100, 10, 0) is None
    assert estimate_cost("openai/gpt-4o-mini", 1_000_000, 0, 0) == 0.15      # provider prefix is ignored


def test_ledger_command_summarises_a_session(make_rig, capsys):
    rig = make_rig([{"tool_calls": [call("get_working_dir")], "usage": {"prompt_tokens": 1000, "completion_tokens": 100}},
                    {"content": "done", "usage": {"prompt_tokens": 1200, "completion_tokens": 10}}])
    rig.agent.run("go")
    assert main(["ledger", rig.session.id, "--session-dir", str(rig.cfg.session_dir)]) == 0
    out = capsys.readouterr().out
    assert rig.session.id in out and "2 calls" in out and " 2200 " in out and " 110 " in out


def test_cli_runs_a_task_end_to_end_against_the_mock(make_rig, repo, tmp_path, capsys):
    rig = make_rig(fix_calc_script())          # only used for its running mock server
    code = main(["Fix the failing test.", "--repo", str(repo), "--base-url", rig.mock.url, "--model", "mock-small",
                 "--session-dir", str(tmp_path / "cli-sessions"), "--config", str(tmp_path / "none.toml"), "--auto-approve", "--savings", "none"])
    captured = capsys.readouterr()
    assert code == 0 and "Fixed add()" in captured.out
    assert "--auto-approve is on" in captured.err and captured.err.count("Auto-approved") == 2
    assert "a + b" in (repo / "calc.py").read_text()
    (session_file,) = (tmp_path / "cli-sessions").glob("*.jsonl")
    assert sum(json.loads(line)["type"] == "ledger" for line in session_file.read_text().splitlines()) == 4


def test_cli_without_approval_flag_denies_when_nobody_answers(make_rig, repo, tmp_path, monkeypatch, capsys):
    rig = make_rig(fix_calc_script()[:2] + [{"content": "gave up"}])
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError))   # no keyboard
    main(["Fix it.", "--repo", str(repo), "--base-url", rig.mock.url, "--session-dir", str(tmp_path / "s"),
          "--config", str(tmp_path / "none.toml")])
    assert "a - b" in (repo / "calc.py").read_text()
    assert "you said no" in capsys.readouterr().err


def test_cli_resume_flag_continues_a_saved_session(make_rig, repo, tmp_path, capsys):
    first = make_rig([{"tool_calls": [call("get_working_dir")]}, {"status": 500}])
    first.agent.run("task")
    second = make_rig([{"content": "resumed fine"}])
    code = main(["--resume", first.session.id, "--base-url", second.mock.url,
                 "--session-dir", str(first.cfg.session_dir), "--config", str(tmp_path / "none.toml")])
    assert code == 0 and "resumed fine" in capsys.readouterr().out
