"""The confidential marker is detected, listed repos are detected, and the marker cannot be changed."""

import pytest

from helpers import call
from conftest import FakeGateway
from pinch.cli import build_agent
from pinch.config import Config
from pinch.policy import MARKER_NAME, load_policy
from pinch.session import Session


def test_marker_file_makes_a_repo_confidential(repo):
    assert not load_policy(repo).confidential
    (repo / MARKER_NAME).write_text("")
    policy = load_policy(repo)
    assert policy.confidential and "marker file" in policy.reasons[0]


def test_marker_in_a_parent_folder_counts(repo):
    (repo.parent / MARKER_NAME).write_text("")
    assert load_policy(repo).confidential


def test_repo_listed_in_user_config_is_confidential(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'confidential_repos = ["{repo}"]\n')
    assert load_policy(repo, config_path=config).confidential
    assert not load_policy(other, config_path=config).confidential
    (repo / "sub").mkdir()
    assert load_policy(repo / "sub", config_path=config).confidential  # inside a listed repo


def test_policy_is_read_once_so_removing_the_marker_later_changes_nothing(repo):
    marker = repo / MARKER_NAME
    marker.write_text("secret")
    policy = load_policy(repo)
    marker.unlink()
    assert policy.confidential
    assert policy.restore_markers() == [marker] and marker.read_text() == "secret"


@pytest.mark.parametrize("auto", [False, True], ids=["asked", "auto-approve"])
@pytest.mark.parametrize("tool,args", [
    ("delete_file", {"file_path": MARKER_NAME}),
    ("write_file", {"file_path": MARKER_NAME, "content": ""}),
    ("write_file", {"file_path": f"./sub/../{MARKER_NAME}", "content": "x", "mode": "append"}),
    ("edit_file", {"file_path": MARKER_NAME, "old_string": "a", "new_string": "b"}),
    ("move_file", {"source": MARKER_NAME, "destination": "gone.txt"}),
    ("move_file", {"source": "calc.py", "destination": MARKER_NAME}),
    ("copy_file", {"source": "calc.py", "destination": MARKER_NAME}),
    ("delete_directory", {"dir_path": ".", "recursive": True}),
    ("search_and_replace", {"pattern": "secret", "replacement": "x"}),
    ("run_command", {"command": f"rm {MARKER_NAME}"}),
    ("run_command", {"command": f"rm ./{MARKER_NAME}", "shell": True}),
    ("run_python", {"code": f"import os; os.remove('{MARKER_NAME}')"}),
])
def test_marker_cannot_be_deleted_or_edited_even_with_auto_approve(make_rig, repo, tool, args, auto):
    (repo / MARKER_NAME).write_text("secret")
    rig = make_rig([{"tool_calls": [call(tool, **args)]}, {"content": "done"}], auto_approve=auto)
    rig.agent.run("go")
    assert (repo / MARKER_NAME).read_text() == "secret"
    assert (repo / "calc.py").exists()
    assert rig.asked == []                                 # refused before the user is even asked
    assert rig.session_events("blocked")


def test_marker_can_still_be_read(make_rig, repo):
    (repo / MARKER_NAME).write_text("secret")
    rig = make_rig([{"tool_calls": [call("read_file", file_path=MARKER_NAME)]}, {"content": "done"}])
    rig.agent.run("go")
    assert not rig.session_events("blocked")


def test_a_shell_command_that_hides_the_name_still_cannot_remove_the_marker(make_rig, repo):
    """The name check is best effort for shell commands. The restore step is the backstop."""
    (repo / MARKER_NAME).write_text("secret")
    rig = make_rig([{"tool_calls": [call("run_command", command="rm -f .pinch-conf*", shell=True)]}, {"content": "done"}],
                   answers=("y",))
    rig.agent.run("go")
    assert (repo / MARKER_NAME).read_text() == "secret"
    assert rig.session_events("marker_restored")
