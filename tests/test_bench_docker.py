"""Against real containers: task validation, secrets staying out of agent commands, and the whole mock smoke through Bifrost.
Skipped when Docker, the pinned images or `python -m pinch.bench setup` are missing."""

import json
import shutil
from types import SimpleNamespace

import pytest

from pinch import prove
from pinch.bench import sandbox
from pinch.bench.agents import mini_command, mini_spec, pinch_command, parse_mini, parse_pinch
from pinch.bench.mock import Oracle, cmd_smoke
from pinch.bench.score import validate_task
from pinch.bench.tasks import load_tasks, prepare_workdir
from pinch.mock_provider import MockProvider, redact_headers

READY = sandbox.docker_ok() and sandbox.image_present() and bool(sandbox.tool_versions())
needs_sandbox = pytest.mark.skipif(not READY, reason="needs Docker, the pinned python image and `python -m pinch.bench setup`")
needs_gateway = pytest.mark.skipif(not (READY and prove.docker_ok() and sandbox.subprocess.run(
    ["docker", "image", "inspect", prove.IMAGE], capture_output=True).returncode == 0), reason=f"needs Docker and {prove.IMAGE}")

SECRET = "sk-bf-test-secret-value-1234"


@needs_sandbox
@pytest.mark.parametrize("task_id", ["A/two-bucket", "B/toolz-interpose-empty", "C/pytest-max-warnings"])
def test_tasks_validate_in_the_container(task_id, tmp_path):
    """The unsolved task must fail its check and the reference solution must pass it, in a real fresh container."""
    t = load_tasks([task_id[0]], only=[task_id])[0]
    result = validate_task(t, sandbox.ensure_env(t.pip), tmp_path)
    assert result["ok"], result


@needs_sandbox
def test_pinch_wrapper_sends_the_key_header_and_keeps_the_key_out_of_commands(tmp_path, monkeypatch):
    mock = MockProvider([
        {"tool_calls": [{"name": "call_tool", "arguments": {"name": "run_command", "arguments": {"command": "env", "shell": True}}}]},
        {"content": "done"}], tmp_path / "rec.jsonl").start()
    try:
        monkeypatch.setenv("PINCH_GATEWAY_KEY", SECRET)
        work, out = tmp_path / "work", tmp_path / "out"
        work.mkdir(); out.mkdir()
        run = sandbox.container_run(
            pinch_command("run env", model="m", base_url=mock.url, savings="none", style="brief", max_steps=5, tool_mode="lazy"),
            mounts=sandbox.task_mounts(work, out), env=sandbox.task_env(sandbox.ensure_env(("pytest==8.4.2",)), ()),
            secrets=sandbox.SECRET_ENV, timeout=120)
        assert run.returncode == 0, run.stdout + run.stderr
        sent = json.dumps(mock.recorded[-1]["body"])                         # what the model was shown, including the output of `env`
        assert "PYTHONDONTWRITEBYTECODE=1" in sent and SECRET not in sent and "PINCH_GATEWAY_KEY" not in sent
        assert mock.recorded[0]["headers"]["x-bf-vk"] == redact_headers({"x-bf-vk": SECRET})["x-bf-vk"]       # the key did reach the gateway
        assert SECRET not in (run.stdout + run.stderr) and parse_pinch(out, 0, "").auto_approvals == 1
    finally:
        mock.stop()


@needs_sandbox
def test_mini_wrapper_sends_the_key_header_keeps_it_out_of_commands_and_trajectory(tmp_path, monkeypatch):
    mock = MockProvider([
        {"tool_calls": [{"name": "bash", "arguments": {"command": "env"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 7}},
        {"tool_calls": [{"name": "bash", "arguments": {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}}],
         "usage": {"prompt_tokens": 400, "completion_tokens": 9, "cached_tokens": 50}}], tmp_path / "rec.jsonl").start()
    try:
        monkeypatch.setenv("PINCH_GATEWAY_KEY", SECRET)
        work, out = tmp_path / "work", tmp_path / "out"
        work.mkdir(); out.mkdir()
        (out / "mini_spec.json").write_text(json.dumps(mini_spec("do it", model="m", base_url=mock.url, max_steps=5, max_tokens=256, wall_time=100)))
        run = sandbox.container_run(mini_command(), mounts=sandbox.task_mounts(work, out),
                                    env=sandbox.task_env(sandbox.ensure_env(("pytest==8.4.2",)), ()), secrets=sandbox.SECRET_ENV, timeout=180)
        assert run.returncode == 0, run.stdout + run.stderr
        sent = json.dumps(mock.recorded[-1]["body"])
        assert "PYTHONDONTWRITEBYTECODE=1" in sent and SECRET not in sent
        assert mock.recorded[0]["headers"]["x-bf-vk"] == redact_headers({"x-bf-vk": SECRET})["x-bf-vk"]
        assert SECRET not in (out / "traj.json").read_text() and SECRET not in (run.stdout + run.stderr)
        o = parse_mini(out, 0, "")
        assert (o.input_tokens, o.output_tokens, o.cached_tokens, o.calls, o.stop) == (500, 16, 50, 2, "done")   # tokens from reported usage
    finally:
        mock.stop()


@needs_gateway
def test_mock_smoke_end_to_end_through_bifrost(tmp_path):
    """pinch and mini-swe-agent, in containers, through the real Bifrost, scored outside the agent, summarised, labelled MOCK."""
    out = tmp_path / "smoke.jsonl"
    args = SimpleNamespace(tasks=["A/two-bucket", "A/pig-latin"], modes=["baseline", "all", "cache-twice"], repeat=1, jobs=2, out=out, with_docs=False)
    assert cmd_smoke(args) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    runs = [r for r in rows if r["type"] == "run"]
    assert rows[0]["type"] == "meta" and rows[0]["mock"] is True and all(r["mock"] for r in runs)
    got = {(r["task"], r["mode"], r["phase"]): r for r in runs}
    for mode in ("baseline", "all"):
        assert got[("A/two-bucket", mode, 1)]["passed"] is True and got[("A/pig-latin", mode, 1)]["passed"] is False
    assert got[("A/two-bucket", "baseline", 1)]["agent"] == "mini" and got[("A/two-bucket", "baseline", 1)]["calls"] == 5
    first, again = got[("A/two-bucket", "cache-twice", 1)], got[("A/two-bucket", "cache-twice", 2)]
    # same task twice: the early calls are answered from the cache; once a test run's output differs (it prints its own timing) the
    # requests differ too and are billed again, so the second run is cheaper but not free
    assert first["total_tokens"] > 0 and again["cache_hits"] >= 1 and again["total_tokens"] < first["total_tokens"]
    assert again["cache_saved_tokens"] > 0
    assert all(r["total_tokens"] > 0 for r in runs if r["mode"] != "cache-twice" or r["phase"] == 1)
    text = out.with_suffix(".md").read_text()
    assert "MOCK DATA" in text and out.with_suffix(".csv").exists()
