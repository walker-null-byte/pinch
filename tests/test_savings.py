"""Token-saving techniques, one at a time. Each is switchable on its own (`--savings`), and each test turns on just that one."""

import json
import re
import subprocess

import pytest

from helpers import call, pytest_command
from pinch.cli import main
from pinch.ledger import summarize
from pinch.savings import TECHNIQUES, parse_savings
from pinch.savings.compress import HARD_CAP, LIMIT, compress_run
from pinch.savings.context import build_context
from pinch.savings.trim import trim_history

MARKER = "pinch-confidential-marker"


def tool_messages(rig):
    return [m for m in rig.agent.messages if m["role"] == "tool"]


# -- switches -------------------------------------------------------------------------------------

def test_savings_spec_all_none_list_and_typos():
    assert parse_savings("all") == frozenset(TECHNIQUES) and len(TECHNIQUES) == 11
    assert parse_savings("none") == frozenset() == parse_savings("")
    assert parse_savings("trim, cache") == {"trim", "cache"}
    with pytest.raises(ValueError, match="unknown technique.*trimm"):
        parse_savings("trimm")


def test_the_techniques_on_are_written_to_the_session_and_shown_by_ledger(make_rig, capsys):
    rig = make_rig([{"content": "ok"}], savings="trim,filememory")
    rig.agent.run("go")
    assert rig.session_events("savings")[-1]["techniques"] == ["filememory", "trim"]
    main(["ledger", rig.session.id, "--session-dir", str(rig.cfg.session_dir)])
    assert "techniques on: filememory, trim" in capsys.readouterr().out


def test_cli_flag_and_a_typo(tmp_path, capsys):
    with pytest.raises(SystemExit, match="unknown technique"):
        main(["x", "--savings", "bogus", "--config", str(tmp_path / "c.toml"), "--session-dir", str(tmp_path)])


# -- context up front -----------------------------------------------------------------------------

@pytest.fixture
def shop(repo):
    (repo / "shop").mkdir()
    (repo / "shop" / "cart.py").write_text("class Cart:\n    def add_item(self, item):\n        self.items.append(item)\n\n"
                                          "    def total(self):\n        return sum(i.price for i in self.items) / len(self.items)\n")
    (repo / "shop" / "pricing.py").write_text("def discount(price, pct):\n    return price * (1 - pct)\n\ndef tax(price):\n    return price * 1.2\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# Guide\nNothing about carts here.\n" * 300)
    (repo / ".env").write_text("API_KEY=super-secret-value\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.js").write_text("module.exports = 1\n")
    return repo


def test_context_has_a_repo_map_with_symbols_and_the_files_that_match_a_traceback(shop):
    task = 'Fix this:\nTraceback (most recent call last):\n  File "shop/cart.py", line 6, in total\nZeroDivisionError: division by zero'
    text, shown = build_context(shop, task)
    assert "Cart[add_item, total]" in text and "discount(price, pct)" in text and "calc.py: add(a, b)" in text
    assert list(shown)[0] == "shop/cart.py" and "--- shop/cart.py ---" in text
    assert "super-secret-value" not in text and ".env" not in text and "junk.js" not in text
    assert "Nothing about carts" not in text                       # the big unrelated doc is in the map only, not the prompt


def test_context_ranks_by_the_words_of_the_task(shop):
    _, shown = build_context(shop, "the tax calculation in pricing is wrong")
    assert "shop/pricing.py" in shown and "shop/cart.py" not in shown


def test_context_goes_in_the_first_prompt_only_when_on(make_rig, shop):
    for savings, expect in (("context", True), ("none", False)):
        rig = make_rig([{"content": "ok"}], savings=savings)
        rig.agent.run("add() in calc.py returns the wrong value")
        first_user = rig.sent()[0]["messages"][1]["content"]
        assert ("[pinch: repo map" in first_user and "--- calc.py ---" in first_user) == expect
        assert (len(rig.session_events("context")) == 1) == expect


def test_context_plus_file_memory_means_the_model_does_not_pay_to_read_the_file_again(make_rig):
    rig = make_rig([{"tool_calls": [call("read_file", file_path="calc.py")]}, {"content": "ok"}], savings="context,filememory")
    rig.agent.run("fix add() in calc.py")
    assert "unchanged since step 0" in tool_messages(rig)[0]["content"] and "return a - b" not in tool_messages(rig)[0]["content"]


# -- file memory ----------------------------------------------------------------------------------

def test_second_read_of_an_unchanged_file_is_a_one_line_stub(make_rig):
    rig = make_rig([{"tool_calls": [call("read_file", file_path="calc.py")]}, {"tool_calls": [call("read_file", file_path="calc.py")]},
                    {"content": "ok"}], savings="filememory")
    rig.agent.run("go")
    first, second = (m["content"] for m in tool_messages(rig))
    assert "return a - b" in first
    assert second == "[pinch: calc.py is unchanged since step 1. Not sent again.]"


def test_a_changed_file_comes_back_as_a_diff_against_the_version_the_model_saw(make_rig, repo):
    (repo / "big.py").write_text("".join(f"def f{i}():\n    return {i}\n\n" for i in range(40)))
    rig = make_rig([{"tool_calls": [call("read_file", file_path="big.py")]},
                    {"tool_calls": [call("edit_file", file_path="big.py", old_string="return 7\n", new_string="return 700\n")]},
                    {"tool_calls": [call("read_file", file_path="big.py")]},
                    {"tool_calls": [call("read_file", file_path="big.py")]}, {"content": "ok"}], savings="filememory", answers=("y",))
    rig.agent.run("go")
    third, fourth = tool_messages(rig)[2]["content"], tool_messages(rig)[3]["content"]
    assert third.startswith("[pinch: big.py changed since step 1") and "-    return 7" in third and "+    return 700" in third
    assert len(third) < 400 and "def f30" not in third
    assert "unchanged since step 3" in fourth


def test_partial_reads_are_never_stubbed(make_rig):
    rig = make_rig([{"tool_calls": [call("read_file", file_path="calc.py")]}, {"tool_calls": [call("read_file", file_path="calc.py", offset=1, limit=1)]},
                    {"content": "ok"}], savings="filememory")
    rig.agent.run("go")
    assert "def add" in tool_messages(rig)[1]["content"]


# -- small edits ----------------------------------------------------------------------------------

def test_tool_descriptions_and_system_prompt_steer_to_small_edits(make_rig, repo):
    from pinch.tools import ToolBox
    on, off = ToolBox(repo, "full", small_edits=True), ToolBox(repo, "full")
    described = {t["function"]["name"]: t["function"]["description"] for t in on.schemas()}
    assert "NEW file" in described["write_file"] and "Preferred" in described["edit_file"] and "unified diff" in described["apply_patch"]
    assert "NEW" not in {t["function"]["name"]: t["function"]["description"] for t in off.schemas()}["write_file"]
    rig = make_rig([{"content": "ok"}], savings="smalledits")
    rig.agent.run("go")
    assert "use edit_file or apply_patch" in rig.sent()[0]["messages"][0]["content"]


def test_how_often_write_file_still_gets_used_is_measured(make_rig, capsys):
    rig = make_rig([{"tool_calls": [call("write_file", file_path="calc.py", content="def add(a, b):\n    return a + b\n"),
                                    call("write_file", file_path="new.py", content="x = 1\n"),
                                    call("edit_file", file_path="new.py", old_string="1", new_string="2")]}, {"content": "ok"}],
                   savings="smalledits", answers=("y", "y", "y"))
    rig.agent.run("go")
    assert "edits: 1 edit_file, 1 write_file (new file), 1 write_file (existing file)" in summarize(rig.session)


# -- test output compression ---------------------------------------------------------------------

PYTEST_OUTPUT = "\n".join(
    ["============================= test session starts =============================", "collected 30 items", ""]
    + ["tests/test_x.py " + "." * 10 + "FFF" + "." * 17, "", "=================================== FAILURES ==================================="]
    + sum(([f"_______________________________ test_case_{i} _______________________________", "", "    def test_case():", "        x = compute()",
            f">       assert x == {i}", f"E       assert {i + 1} == {i}", "", f"tests/test_x.py:{10 + i}: AssertionError"] + ["    noise " * 12] * 6 for i in range(3)), [])
    + ["=========================== short test summary info ============================"]
    + [f"FAILED tests/test_x.py::test_case_{i} - assert {i + 1} == {i}" for i in range(3)]
    + ["======================== 3 failed, 27 passed in 0.31s ========================="])


def command_result(text: str, rc: int = 1) -> str:
    return json.dumps({"returncode": rc, "stdout": text, "stderr": ""})


def test_long_pytest_output_is_cut_to_names_first_errors_and_the_summary(repo):
    raw = command_result(PYTEST_OUTPUT)
    out = compress_run(raw, repo / ".pinch" / "outputs", "t1", repo)
    assert len(PYTEST_OUTPUT) > 2 * HARD_CAP and len(out) <= HARD_CAP + 400
    for i in range(3):
        assert f"FAILED tests/test_x.py::test_case_{i}" in out and f"first error: assert {i + 1} == {i}" in out
    assert "3 failed, 27 passed in 0.31s" in out and "noise" not in out and out.startswith("returncode 1.")
    assert (repo / ".pinch" / "outputs" / "t1.txt").read_text() == PYTEST_OUTPUT                      # the full output is kept


def test_short_output_and_non_command_results_are_untouched(repo):
    short = command_result("1 passed in 0.01s", rc=0)
    assert compress_run(short, repo / ".pinch" / "o", "t", repo) == short
    assert compress_run("Error: nope", repo / ".pinch" / "o", "t", repo) == "Error: nope"
    assert len("x" * LIMIT) <= LIMIT


def test_unrecognised_long_output_keeps_the_start_and_the_end(repo):
    lines = [f"line {i}" for i in range(400)]
    out = compress_run(command_result("\n".join(lines)), repo / ".pinch" / "o", "t", repo)
    assert "line 0" in out and "line 399" in out and "line 200" not in out and len(out) < 1200


def test_the_agent_compresses_test_runs_and_the_full_output_is_readable_from_the_repo(make_rig, repo):
    tests = "".join(f"def test_n{i}():\n    assert {i} + 1 == {i}, 'case {i}'\n\n" for i in range(25))
    (repo / "test_many.py").write_text(tests)
    rig = make_rig([{"tool_calls": [call("run_command", command=pytest_command())]}, {"content": "ok"}], savings="testcompress", answers=("y",))
    rig.agent.run("run the tests")
    text = tool_messages(rig)[0]["content"]
    assert text.startswith("returncode 1.") and "26 failed" in text and len(text) < 2200
    saved = re.search(r"in (\.pinch/outputs/\S+\.txt)", text).group(1)
    assert "test_n24" in (repo / saved).read_text() and len((repo / saved).read_text()) > 3000
    assert ".pinch" not in subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout
    off = make_rig([{"tool_calls": [call("run_command", command=pytest_command())]}, {"content": "ok"}], savings="none", answers=("y",))
    off.agent.run("run the tests")
    assert len(tool_messages(off)[0]["content"]) > 3000


def test_huge_test_output_is_compressed_before_the_12000_character_cap_can_cut_it(make_rig, repo):
    """Regression: cutting the JSON at 12000 characters used to make it unreadable, so nothing got compressed."""
    (repo / "test_huge.py").write_text("".join(f"def test_h{i}():\n    assert {i} + 1 == {i}, 'a longish failure message for case {i}'\n\n" for i in range(120)))
    rig = make_rig([{"tool_calls": [call("run_command", command=pytest_command())]}, {"content": "ok"}], savings="testcompress", answers=("y",))
    rig.agent.run("run the tests")
    text = tool_messages(rig)[0]["content"]
    assert text.startswith("returncode 1.") and len(text) < 2200 and "121 failed" in text
    assert len((repo / re.search(r"in (\.pinch/outputs/\S+\.txt)", text).group(1)).read_text()) > 12000        # the saved file is complete


# -- history trimming -----------------------------------------------------------------------------

def test_old_tool_output_becomes_a_stub_but_the_latest_file_version_stays(make_rig, repo):
    cmd = lambda i: call("run_command", command=f"{__import__('shlex').quote(__import__('sys').executable)} -c \"print('{i}' * 400)\"")
    script = [{"tool_calls": [call("read_file", file_path="calc.py")]}] + [{"tool_calls": [cmd(i)]} for i in range(1, 6)] + [{"content": "ok"}]
    rig = make_rig(script, savings="trim", auto_approve=True, trim_after=2)
    rig.agent.run("go")
    last = rig.sent()[-1]["messages"]
    tools = [m["content"] for m in last if m["role"] == "tool"]
    assert "return a - b" in tools[0]                                       # the file read is the latest version of calc.py: kept
    assert [t.startswith("[pinch: earlier output of run_command") for t in tools[1:]] == [True, True, False, False, False]
    assert "1111" not in tools[1] and "5555" in tools[-1] and "4444" in tools[-2]       # the newest steps are intact
    assert all("run_command" in t and "characters" in t for t in tools[1:3])
    assert "1" * 400 in json.dumps(rig.session.messages())                  # the session file keeps everything


def test_trimming_is_deterministic_so_the_prefix_stays_byte_identical():
    msgs = [{"role": "user", "content": "t"}]
    for i in range(6):
        msgs += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "grep_search", "arguments": "{\"pattern\": \"x\"}"}}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 500}]
    a, b = trim_history(msgs, 2, set()), trim_history(msgs + [{"role": "user", "content": "more"}], 2, set())
    assert a == b[:-1] and a[2]["content"] == trim_history(msgs, 2, set())[2]["content"]


# -- stop on green ----------------------------------------------------------------------------------

def edit_add(lazy=True):
    return call("edit_file", file_path="calc.py", old_string="a - b", new_string="a + b")


def run_tests_call():
    return call("run_test", command=pytest_command())


def test_red_then_green_stops_the_run_without_another_model_call(make_rig, repo):
    """The tests fail first (the model ran them), the edit fixes them, pinch's own run is green: stop, no further model call."""
    rig = make_rig([{"tool_calls": [run_tests_call()]}, {"tool_calls": [edit_add()]}, {"content": "never asked for"}],
                   savings="stopongreen", test_command=pytest_command(), answers=("y", "y", "y"))
    result = rig.agent.run("fix add")
    assert result.stop == "done" and "tests pass" in result.text
    assert len(rig.sent()) == 2                                           # the test run, the edit; nothing after the green
    assert len(rig.asked) == 3 and "tool: run_test (shell)" in rig.asked[0] and "edit_file" in rig.asked[1] and "tool: run_test (shell)" in rig.asked[2]
    assert any("pinch proposes running the tests" in line for line in rig.said)
    assert rig.session_events("stop_on_green")[0]["rule"] == "red_to_green" and not rig.session_events("green_not_stopped")
    assert [e["red"] for e in rig.session_events("test_result")] == [True, False]
    assert rig.agent.messages[-1] == {"role": "assistant", "content": result.text}
    out = summarize(rig.session)
    assert "stopped on green" in out and "red_to_green" in out


def test_green_with_no_earlier_red_does_not_stop_the_run(make_rig, repo):
    """A bug fix whose existing tests already pass: green says nothing, so the model decides when it is done."""
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")                    # the visible tests are green from the start
    rig = make_rig([{"tool_calls": [call("edit_file", file_path="calc.py", old_string="def add(a, b):", new_string="def add(a, b):  # sum")]},
                    {"content": "I am done."}], savings="stopongreen", test_command=pytest_command(), answers=("y", "y"))
    result = rig.agent.run("handle the edge case")
    assert result.text == "I am done." and len(rig.sent()) == 2           # pinch ran the tests (green) but did not end the run
    assert len(rig.asked) == 2 and "tool: run_test (shell)" in rig.asked[1]
    assert not rig.session_events("stop_on_green")
    held = rig.session_events("green_not_stopped")
    assert len(held) == 1 and held[0]["rule"] == "never_red"
    assert "never failed" in summarize(rig.session)


def test_green_before_any_red_then_red_then_green_stops_on_the_last_one(make_rig, repo):
    """Never-red green does not stop; once the tests have failed, the next green does."""
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    break_it = call("edit_file", file_path="calc.py", old_string="a + b", new_string="a - b")
    rig = make_rig([{"tool_calls": [call("edit_file", file_path="calc.py", old_string="def add(a, b):", new_string="def add(a, b):  # sum")]},
                    {"tool_calls": [break_it]}, {"tool_calls": [call("edit_file", file_path="calc.py", old_string="a - b", new_string="a + b  # ok")]},
                    {"content": "never asked for"}], savings="stopongreen", test_command=pytest_command(), answers=("y",) * 6)
    result = rig.agent.run("go")
    assert result.stop == "done" and "tests pass" in result.text and len(rig.sent()) == 3
    assert [e["rule"] for e in rig.session_events("green_not_stopped")] == ["never_red"] and rig.session_events("stop_on_green")[0]["rule"] == "red_to_green"


def test_a_run_that_never_ran_the_tests_is_neither_red_nor_green():
    from pinch.agent import is_red
    assert [is_red(rc) for rc in (1, 2, 5, 124, 126, 127, -1, 0)] == [True, True, False, False, False, False, False, False]


def test_the_bug_fix_line_is_in_the_byte_stable_system_prompt():
    from pinch.agent import SYSTEM_PROMPT
    assert "no failing test, first add a small test that reproduces the bug" in SYSTEM_PROMPT
    assert "\n" not in SYSTEM_PROMPT and len(SYSTEM_PROMPT) < 400            # short, one line, no per-call text (the prefix stays cacheable)


def test_the_test_proposal_needs_approval_and_declining_is_remembered(make_rig, repo):
    rig = make_rig([{"tool_calls": [edit_add()]}, {"tool_calls": [call("edit_file", file_path="calc.py", old_string="a + b", new_string="b + a")]},
                    {"content": "done"}], savings="stopongreen", test_command=pytest_command(), answers=("y", "n", "y"))
    result = rig.agent.run("fix add")
    assert result.stop == "done" and "tests pass" not in result.text    # the user said no, so the model finished normally
    assert len(rig.asked) == 3 and sum("run_test" in p for p in rig.asked) == 1       # asked once, not after every edit
    assert len(rig.sent()) == 3


def test_always_for_the_exact_test_command_works_and_failing_tests_go_back_to_the_model(make_rig, repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text("from calc import add, sub\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n")
    # (the second edit makes the file longer: a same-size edit within one second can leave a stale .pyc)
    rig = make_rig([{"tool_calls": [edit_add()]},
                    {"tool_calls": [call("edit_file", file_path="calc.py", old_string="def sub(a, b):\n    return a + b", new_string="def sub(a, b):\n    return a - b  # fixed")]},
                    {"content": "never reached"}], savings="stopongreen,testcompress", test_command=pytest_command(), answers=("y", "a", "y"))
    result = rig.agent.run("fix both")
    assert result.stop == "done" and len(rig.sent()) == 2
    assert len(rig.asked) == 3 and sum("run_test" in p for p in rig.asked) == 1       # edit, tests ("a"), edit; second test run not asked
    assert "only this exact run_test call" in [p for p in rig.asked if "run_test" in p][0]
    second_request_tool = [m for m in rig.sent()[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "they still fail" in second_request_tool and "test_sub" in second_request_tool
    resumed = rig.session.messages()                                                   # the appended failure survives a resume
    assert "they still fail" in [m for m in resumed if m["role"] == "tool"][0]["content"]


def test_no_proposal_when_the_repo_has_no_tests(make_rig, tmp_path):
    import subprocess as sp
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "a.py").write_text("x = 1\n")
    sp.run(["git", "init", "-q"], cwd=bare, check=True)
    rig = make_rig([{"tool_calls": [call("edit_file", file_path="a.py", old_string="1", new_string="2")]}, {"content": "done"}],
                   savings="stopongreen", repo_path=bare, answers=("y",))
    assert rig.agent.run("go").text == "done" and len(rig.asked) == 1


# -- pretest: run the tests once before the first model call ----------------------------------------------

def first_user_message(rig):
    return [m for m in rig.sent()[0]["messages"] if m["role"] == "user"][0]["content"]


def test_red_pretest_then_a_correct_first_fix_stops_without_an_extra_model_call(make_rig, repo):
    rig = make_rig([{"tool_calls": [edit_add()]}, {"content": "never asked for"}], savings="pretest,stopongreen",
                   test_command=pytest_command(), answers=("y", "y", "y"))
    result = rig.agent.run("fix add")
    assert result.stop == "done" and "tests pass" in result.text
    assert len(rig.sent()) == 1                                            # the single model call is the edit; nothing after the green
    assert len(rig.asked) == 3 and "tool: run_test (shell)" in rig.asked[0] and "edit_file" in rig.asked[1]        # the pretest asked first
    first = first_user_message(rig)
    assert first.startswith("fix add") and "they currently fail" in first and "test_add" in first and "in 0." not in first.split("fail:")[1]
    assert [e["status"] for e in rig.session_events("pretest")] == ["red"] and rig.session_events("pretest")[0]["returncode"] == 1
    assert [e["red"] for e in rig.session_events("test_result")] == [True, False]
    assert rig.session_events("stop_on_green")[0]["rule"] == "red_to_green" and rig.agent.seen_red
    assert "pretest" in summarize(rig.session) and "the tests failed" in summarize(rig.session)


def test_green_pretest_adds_a_one_line_note_and_does_not_stop(make_rig, repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    rig = make_rig([{"tool_calls": [call("edit_file", file_path="calc.py", old_string="def add(a, b):", new_string="def add(a, b):  # sum")]},
                    {"content": "I am done."}], savings="pretest,stopongreen", test_command=pytest_command(), answers=("y", "y", "y"))
    result = rig.agent.run("handle the edge case")
    assert result.text == "I am done." and len(rig.sent()) == 2           # the green run after the edit did not end it
    first = first_user_message(rig)
    assert first.endswith("[pinch ran the tests before you started: they currently pass.]") and first.count("\n") <= 3
    assert [e["status"] for e in rig.session_events("pretest")] == ["green"] and not rig.agent.seen_red
    assert rig.session_events("green_not_stopped")[0]["rule"] == "never_red" and not rig.session_events("stop_on_green")


def test_with_pretest_off_behaviour_is_unchanged(make_rig, repo):
    rig = make_rig([{"tool_calls": [edit_add()]}, {"content": "after the edit"}], savings="stopongreen",
                   test_command=pytest_command(), answers=("y", "y"))
    result = rig.agent.run("fix add")
    assert first_user_message(rig) == "fix add" and not rig.session_events("pretest")
    assert "edit_file" in rig.asked[0] and sum("run_test" in p for p in rig.asked) == 1       # only the post-edit proposal asks
    assert result.text == "after the edit" and len(rig.sent()) == 2                           # never red, so the green did not stop it
    assert "pretest" not in rig.session_events("savings")[0]["techniques"]
    none = make_rig([{"content": "ok"}], savings="none", test_command=pytest_command())
    none.agent.run("hi")
    assert first_user_message(none) == "hi" and none.asked == []


def test_the_pretest_is_in_all_and_each_technique_can_be_turned_off_alone(make_rig, repo):
    from pinch.savings import parse_savings
    assert "pretest" in parse_savings("all") and "pretest" not in parse_savings("none")
    rig = make_rig([{"content": "ok"}], savings="all", test_command=pytest_command(), answers=("y",))
    rig.agent.run("hi")
    assert rig.session_events("pretest") and "pretest" in rig.session_events("savings")[0]["techniques"]


def test_the_pretest_needs_approval_and_a_no_is_remembered(make_rig, repo):
    rig = make_rig([{"tool_calls": [edit_add()]}, {"content": "done"}], savings="pretest,stopongreen",
                   test_command=pytest_command(), answers=("n", "y"))
    result = rig.agent.run("fix add")
    assert "tool: run_test (shell)" in rig.asked[0] and "pytest" in rig.asked[0]
    assert first_user_message(rig) == "fix add"                                           # nothing added
    event = rig.session_events("pretest")[0]
    assert event["status"] == "skipped" and event["reason"] == "declined"
    assert sum("run_test" in p for p in rig.asked) == 1 and result.text == "done"           # not asked again after the edit


def test_under_auto_approve_the_pretest_is_not_asked_but_logged(make_rig, repo):
    rig = make_rig([{"tool_calls": [edit_add()]}, {"content": "never asked for"}], savings="pretest,stopongreen",
                   test_command=pytest_command(), auto_approve=True)
    result = rig.agent.run("fix add")
    assert rig.asked == [] and result.stop == "done" and len(rig.sent()) == 1
    logged = rig.session_events("auto_approved")
    assert [e["tool"] for e in logged] == ["run_test", "edit_file", "run_test"] and "currently fail" in first_user_message(rig)
    assert any(e.get("pretest") for e in rig.session_events("tool_run"))


def test_a_pretest_that_cannot_run_is_left_out_and_the_reason_recorded(make_rig, repo, tmp_path):
    rig = make_rig([{"content": "ok"}], savings="pretest", test_command="definitely-not-a-real-command-xyz", answers=("y",))
    rig.agent.run("hi")
    event = rig.session_events("pretest")[0]
    assert (event["status"], event["reason"]) == ("skipped", "command_not_found") and first_user_message(rig) == "hi"
    assert "skipped (command_not_found)" in summarize(rig.session)
    timeout = make_rig([{"content": "ok"}], savings="pretest", test_command=pytest_command(), answers=("y",))
    timeout.agent.tools.execute = lambda *a, **k: json.dumps({"returncode": -1, "stdout": "", "stderr": "Command timed out after 300s"})
    timeout.agent.run("hi")
    assert timeout.session_events("pretest")[0]["reason"] == "timeout" and first_user_message(timeout) == "hi"
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True)
    none_found = make_rig([{"content": "ok"}], savings="pretest", repo_path=bare)
    none_found.agent.run("hi")
    assert none_found.session_events("pretest")[0]["reason"] == "no_tests_found" and none_found.asked == []


def test_the_red_state_survives_a_resume(make_rig, repo):
    rig = make_rig([{"content": "stop here"}], savings="pretest", test_command=pytest_command(), answers=("y",))
    rig.agent.run("fix add")
    assert rig.agent.seen_red
    again = make_rig([{"content": "ok"}], savings="stopongreen", resume=rig.session.id)
    assert again.agent.seen_red


# -- cascade ----------------------------------------------------------------------------------------

def models_requested(rig):
    return [b["model"] for b in rig.sent()]


def test_cascade_starts_small_and_escalates_once_when_tests_keep_failing(make_rig):
    wrong = call("edit_file", file_path="calc.py", old_string="a - b", new_string="a * b")
    wrong2 = call("edit_file", file_path="calc.py", old_string="a * b", new_string="a / b")
    run = call("run_command", command=pytest_command())
    rig = make_rig([{"tool_calls": [wrong]}, {"tool_calls": [run]}, {"tool_calls": [wrong2]}, {"tool_calls": [run]},
                    {"tool_calls": [call("get_working_dir")]}, {"content": "ok"}], savings="cascade",
                   small_model="mock-small", big_model="mock-big", answers=("y",) * 8)
    rig.agent.run("fix add")
    assert models_requested(rig) == ["mock-small"] * 4 + ["mock-big"] * 2
    rows = [r for r in rig.session.records() if r["type"] == "ledger"]
    assert [r["model_tier"] for r in rows] == ["small"] * 4 + ["big"] * 2
    assert [bool(r["escalated"]) for r in rows] == [False] * 4 + [True, False]       # recorded once, on the first big call
    assert "tests still failing after 2 edit-and-test rounds" in rows[4]["escalated"]
    assert len(rig.session_events("escalated")) == 1 and "escalated to the big tier at step 5" in summarize(rig.session)


def test_cascade_escalates_on_repeated_tool_errors(make_rig):
    rig = make_rig([{"tool_calls": [call("read_file", file_path=f"missing{i}.py")]} for i in range(3)] + [{"content": "ok"}],
                   savings="cascade", small_model="mock-small", big_model="mock-big")
    rig.agent.run("go")
    assert models_requested(rig) == ["mock-small"] * 3 + ["mock-big"]
    assert "3 tool errors in a row" in rig.session_events("escalated")[0]["reason"]


def test_cascade_stays_put_without_signals_and_when_not_configured_or_switched_off(make_rig):
    rig = make_rig([{"tool_calls": [call("get_working_dir")]}] * 3 + [{"content": "ok"}], savings="cascade", small_model="mock-small", big_model="mock-big")
    rig.agent.run("go")
    assert set(models_requested(rig)) == {"mock-small"}
    for kwargs in ({"savings": "cascade"}, {"savings": "none", "small_model": "mock-small", "big_model": "mock-big"}):
        off = make_rig([{"tool_calls": [call("read_file", file_path=f"m{i}.py")]} for i in range(4)] + [{"content": "ok"}], **kwargs)
        off.agent.run("go")
        assert set(models_requested(off)) == {"mock-small"} and not off.session_events("escalated")


# -- stable prefix ----------------------------------------------------------------------------------

def test_system_prompt_and_tool_schemas_are_byte_identical_from_call_to_call(make_rig):
    rig = make_rig([{"tool_calls": [call("get_working_dir")]}, {"tool_calls": [call("get_working_dir")]}, {"content": "ok"}], savings="prefix")
    rig.agent.run("go")
    first, *rest = rig.mock.recorded
    for other in rest:
        assert json.dumps(other["body"]["tools"]) == json.dumps(first["body"]["tools"])
        assert other["body"]["messages"][0] == first["body"]["messages"][0]
        assert other["body"]["messages"][:2] == first["body"]["messages"][:2]
    assert list(first["body"])[:2] == ["model", "max_tokens"] and list(first["body"])[2] == "tools"      # tools before messages


def test_without_the_prefix_technique_the_system_prompt_changes_every_call(make_rig):
    rig = make_rig([{"tool_calls": [call("get_working_dir")]}, {"content": "ok"}], savings="none")
    rig.agent.run("go")
    a, b = (r["body"]["messages"][0]["content"] for r in rig.mock.recorded)
    assert a != b and a.startswith("[pinch step 1]") and b.startswith("[pinch step 2]")


def test_cached_tokens_are_reported_separately_in_the_ledger(make_rig, capsys):
    rig = make_rig([{"tool_calls": [call("get_working_dir")], "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "cached_tokens": 0}},
                    {"content": "ok", "usage": {"prompt_tokens": 1100, "completion_tokens": 10, "cached_tokens": 900}}], savings="prefix")
    rig.agent.run("go")
    rows = [r for r in rig.session.records() if r["type"] == "ledger"]
    assert [r["cached_tokens"] for r in rows] == [0, 900]
    assert "cached input tokens (provider prompt cache): 900 of 2100 = 43%" in summarize(rig.session)


# -- cache ------------------------------------------------------------------------------------------

def fingerprint(value):
    import hashlib
    return "redacted:" + hashlib.sha256(value.encode()).hexdigest()[:8]


def test_cache_uses_a_fresh_namespace_per_run_and_exact_match_only(make_rig):
    one, two = (make_rig([{"content": "ok"}], savings="cache") for _ in range(2))
    for rig in (one, two):
        rig.agent.run("go")
    h1, h2 = (r.mock.recorded[0]["headers"] for r in (one, two))
    assert h1["x-bf-cache-key"] == fingerprint(one.session.id) != h2["x-bf-cache-key"] and h1["x-bf-cache-type"] == "direct"
    pinned = make_rig([{"content": "ok"}], savings="cache", cache_key="shared-on-purpose")
    pinned.agent.run("go")
    assert pinned.mock.recorded[0]["headers"]["x-bf-cache-key"] == fingerprint("shared-on-purpose")
    off = make_rig([{"content": "ok"}], savings="none")
    off.agent.run("go")
    assert not any(h.startswith("x-bf-cache") for h in off.mock.recorded[0]["headers"])


def test_confidential_repos_never_use_the_cache_or_the_web_lookup(make_rig, repo):
    (repo / ".pinch-confidential").write_text("")
    rig = make_rig([{"content": "ok"}], savings="all", cache_key="shared", tavily_key="tvly-x")
    rig.agent.run("go")
    assert not any(h.startswith("x-bf-cache") for h in rig.mock.recorded[0]["headers"])
    assert {"cache", "docs"}.isdisjoint(rig.session_events("savings")[-1]["techniques"])
    assert "docs_lookup" not in json.dumps(rig.sent()[0]["tools"])


def test_a_cache_hit_costs_nothing_and_the_saving_is_recorded(make_rig):
    rig = make_rig([{"content": "ok", "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
                     "response_headers": {"x-bifrost-cache-hit": "true", "x-bifrost-cache-hit-type": "direct", "x-bifrost-cache-input-tokens": "7"}}],
                   savings="cache")
    rig.agent.run("go")
    row = [r for r in rig.session.records() if r["type"] == "ledger"][0]
    assert row["cache_hit"] and row["input_tokens"] == row["output_tokens"] == 0 and row["cost_usd"] == 0.0
    assert row["cache_saved_tokens"] == 1050 and row["embedding_tokens"] == 7
    assert "1 calls, 1050 tokens not billed; embedding tokens spent on the cache: 7" in summarize(rig.session)
    assert rig.agent.spent == 0.0


# -- per-user spend file ------------------------------------------------------------------------------

def test_spend_file_adds_up_across_sessions_and_resets_each_month(tmp_path):
    from pinch.spend import SpendFile
    spend = SpendFile(tmp_path, "alice")
    spend.add(0.004)
    SpendFile(tmp_path, "alice").add(0.003)
    assert SpendFile(tmp_path, "alice").total() == pytest.approx(0.007) and SpendFile(tmp_path, "bob").total() == 0.0
    data = json.loads(spend.path.read_text())
    spend.path.write_text(json.dumps({**data, "period": "1999-01"}))
    assert spend.total() == 0.0


def test_the_ledger_fallback_budget_counts_spend_across_sessions(make_rig):
    from pinch.spend import SpendFile
    usage = {"prompt_tokens": 3000, "completion_tokens": 100}                        # $0.0034 at the demo price
    first = make_rig([{"content": "ok", "usage": usage}], budget_usd=0.0036, max_tokens=100)
    first.agent.run("one")
    assert SpendFile(first.cfg.spend_dir, "vk-test").total() == pytest.approx(0.0034)
    second = make_rig([{"content": "must not be sent"}], budget_usd=0.0036, max_tokens=100)      # a new session, same user
    result = second.agent.run("two")
    assert result.stop == "budget" and second.sent() == [] and "pinch ledger" in second.budget_prompts[0]


def test_the_recorded_technique_list_is_what_is_really_on(make_rig):
    bare = make_rig([{"content": "ok"}], savings="all")
    bare.agent.run("go")
    assert set(bare.session_events("savings")[-1]["techniques"]) == set(TECHNIQUES) - {"docs", "cascade"}     # no key, no tier models
    full = make_rig([{"content": "ok"}], savings="all", tavily_key="k", small_model="mock-small", big_model="mock-big")
    full.agent.run("go")
    assert set(full.session_events("savings")[-1]["techniques"]) == set(TECHNIQUES)
