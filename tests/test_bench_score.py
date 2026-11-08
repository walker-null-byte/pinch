"""Scoring: pass or fail is decided by the harness after the agent has stopped, not by the agent."""

from types import SimpleNamespace

from pinch.bench import score
from pinch.bench.sandbox import Run
from pinch.bench.score import purge_caches, run_check, summary_line
from pinch.bench.tasks import load_tasks, prepare_workdir


def fake_container(monkeypatch, returncode=0, stdout="9 passed in 0.01s", timed_out=False):
    seen = {}
    def run(command, **kw):
        seen.update(command=command, **kw)
        seen["files_at_check"] = {p.name: p.read_text() for p in kw["mounts"]["/work"][0].glob("*.py")}
        return Run(returncode, stdout, "", timed_out, 1.5)
    monkeypatch.setattr(score, "container_run", run)
    return seen


def task():
    return load_tasks(["A"], only=["two-bucket"])[0]


def test_the_check_restores_the_tests_before_running_and_trusts_only_the_exit_code(tmp_path, monkeypatch):
    t = task()
    work = prepare_workdir(t, tmp_path / "w")
    (work / "two_bucket_test.py").write_text("def test_cheat():\n    assert True\n")           # the agent rewrote the tests
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "two_bucket.cpython-312.pyc").write_bytes(b"stale")
    seen = fake_container(monkeypatch, returncode=0)
    result = run_check(t, work, "py-x")
    assert result.passed and result.summary == "9 passed in 0.01s"
    assert seen["files_at_check"]["two_bucket_test.py"] == (t.check_dir / "two_bucket_test.py").read_text()   # pristine test, not the cheat
    assert not (work / "__pycache__").exists()                                                   # bytecode purged
    assert seen["command"][:2] == ["sh", "-c"] and seen["command"][2] == t.check_command and seen["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_nonzero_exit_and_timeout_fail(tmp_path, monkeypatch):
    t = task()
    work = prepare_workdir(t, tmp_path / "w")
    fake_container(monkeypatch, returncode=1, stdout="2 failed, 7 passed in 0.1s")
    assert not run_check(t, work, "py-x").passed
    fake_container(monkeypatch, returncode=5, stdout="no tests ran in 0.01s")                    # pytest found nothing: not a pass
    assert not run_check(t, work, "py-x").passed
    fake_container(monkeypatch, returncode=124, stdout="", timed_out=True)
    r = run_check(t, work, "py-x")
    assert not r.passed and r.timed_out and r.summary == "timed out"


def test_summary_line_strips_colour_and_finds_the_pytest_summary():
    text = "...\n\x1b[31m\x1b[1m1 failed\x1b[0m, \x1b[32m240 passed\x1b[0m in 0.14s\n"
    assert summary_line(text) == "1 failed, 240 passed in 0.14s"
    assert summary_line("PASS") == "PASS" and summary_line("") == ""


def test_purge_caches(tmp_path):
    (tmp_path / "a" / "__pycache__").mkdir(parents=True)
    (tmp_path / "a" / "__pycache__" / "x.pyc").write_bytes(b"1")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / "y.pyc").write_bytes(b"1")
    (tmp_path / "keep.py").write_text("1")
    purge_caches(tmp_path)
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == ["keep.py"]
