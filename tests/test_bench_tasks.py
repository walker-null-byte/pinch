"""The task loader and the task data: counts, provenance, and that no answer is visible to an agent."""

import json

import pytest

from pinch.bench.tasks import TaskError, load_task, load_tasks, overlay, prepare_workdir


def test_set_sizes_and_provenance():
    a, b, c = (load_tasks([s]) for s in "ABC")
    assert (len(a), len(b), len(c)) == (34, 8, 5)
    assert {t.source["commit"] for t in a} == {"7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f"}   # pinned polyglot commit
    assert all(t.source["license"] == "MIT" for t in a)
    for t in b:  # repo, fix commit, parent commit and licence are recorded per task
        assert t.source["repo"].startswith("https://github.com/") and len(t.source["fix_commit"]) == 40
        assert len(t.source["base_commit"]) == 40 and t.source["license"]
        assert t.source["license"] in {"MIT", "BSD-3-Clause"}                                  # permissive only
    assert len({t.source["repo"] for t in b}) == 8                                              # eight different projects
    assert all(t.authored_by == "pinch-team" for t in c)                                        # marked as written by us


def test_ids_match_folders_and_every_task_has_a_prompt_and_check():
    for t in load_tasks():
        assert t.id == f"{t.set}/{t.root.name}" and t.prompt.strip() and t.check_command
        assert any(t.check_dir.rglob("*")) and any(t.gold_dir.rglob("*"))


def test_the_agent_never_starts_with_the_answer():
    for t in load_tasks():
        files = {str(p.relative_to(t.repo_dir)) for p in t.repo_dir.rglob("*") if p.is_file()}
        assert not any(".meta" in f.split("/") or "example.py" in f for f in files), t.id             # A: reference solution
        assert not any(f.startswith("gold") for f in files)
        if t.set == "B":  # the fix's new test is hidden: the starting repo has the OLD version of each test file
            for rel in t.source["hidden_test_files"]:
                assert (t.check_dir / rel).read_text() != (t.repo_dir / rel).read_text(), t.id
        if t.set == "C":
            assert "_bench_check.py" not in files


def test_gold_differs_from_the_starting_repo():
    for t in load_tasks():
        for gold in t.gold_dir.rglob("*"):
            if gold.is_file():
                start = t.repo_dir / gold.relative_to(t.gold_dir)
                assert not start.exists() or start.read_text() != gold.read_text(), (t.id, gold)


def test_workdir_is_a_fresh_copy_and_overlay_restores_tests(tmp_path):
    t = load_tasks(["A"], only=["two-bucket"])[0]
    work = prepare_workdir(t, tmp_path / "w")
    (work / "two_bucket_test.py").write_text("# the agent edited the tests\n")
    assert not (work / "gold").exists() and not (work / ".meta").exists()
    overlay(t.check_dir, work)                                                                      # what the checker does
    assert (work / "two_bucket_test.py").read_text() == (t.check_dir / "two_bucket_test.py").read_text()
    assert prepare_workdir(t, tmp_path / "w").joinpath("two_bucket_test.py").read_text() == (t.repo_dir / "two_bucket_test.py").read_text()


def test_filters_and_limit():
    assert [t.slug for t in load_tasks(["A"], only=["two-bucket", "A/zipper"])] == ["two-bucket", "zipper"]
    assert len(load_tasks(["A", "B"], limit=2)) == 4


def test_loader_rejects_bad_tasks(tmp_path):
    def make(data, name="x"):
        d = tmp_path / "A" / name
        for sub in ("repo", "check", "gold"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        (d / "task.json").write_text(json.dumps(data))
        return d

    good = {"id": "A/x", "set": "A", "title": "t", "prompt": "p", "env": {}, "check": {"command": "true"}, "source": {}, "authored_by": "me"}
    assert load_task(make(good)).slug == "x"
    with pytest.raises(TaskError, match="missing"):
        load_task(make({k: v for k, v in good.items() if k != "prompt"}))
    with pytest.raises(TaskError, match="does not match its folder"):
        load_task(make({**good, "id": "A/other"}))
    with pytest.raises(TaskError, match="written by us"):
        load_task(make({**good, "id": "C/y", "set": "C"}, name="y"))
    with pytest.raises(TaskError, match="set B"):
        load_task(make({**good, "id": "B/z", "set": "B"}, name="z"))
    with pytest.raises(TaskError, match="unknown set"):
        load_tasks(["Z"])
