"""Styles: the instruction is present, short, and never applied to tool arguments or code."""

import json
from pathlib import Path

import pytest

from helpers import call
from pinch.agent import SYSTEM_PROMPT
from pinch.config import STYLES
from pinch.styles import STYLES as INSTRUCTIONS, style_instruction

ROOT = Path(__file__).resolve().parents[1]


def test_every_style_has_an_instruction_under_five_lines():
    assert set(INSTRUCTIONS) == set(STYLES) == {"normal", "brief", "tiny", "caveman"}
    for name, text in INSTRUCTIONS.items():
        assert 1 <= len(text.splitlines()) < 5, name
        assert len(text) < 400, f"{name} is sent on every call, keep it short"


@pytest.mark.parametrize("style", STYLES)
def test_instruction_is_in_the_system_prompt_of_every_request(make_rig, style):
    rig = make_rig([{"tool_calls": [call("get_working_dir")]}, {"content": "done"}], style=style, savings="none")
    rig.agent.run("go")
    for body in rig.sent():
        system = body["messages"][0]
        assert system["role"] == "system" and system["content"] == SYSTEM_PROMPT + "\n" + style_instruction(style)


@pytest.mark.parametrize("style", [s for s in STYLES if s != "normal"])
def test_compressing_styles_say_they_are_for_prose_only(style):
    text = style_instruction(style)
    assert "prose only" in text and "tool arguments" in text and "commit messages" in text


def test_style_can_change_mid_session(make_rig):
    rig = make_rig([{"content": "one"}, {"content": "two"}], style="brief")
    rig.agent.run("a")
    rig.agent.set_style("caveman")
    rig.agent.run("b")
    first, second = (b["messages"][0]["content"] for b in rig.sent())
    assert style_instruction("brief") in first and style_instruction("caveman") in second


@pytest.mark.parametrize("style", STYLES)
def test_style_never_touches_tool_arguments_or_file_content(make_rig, repo, style):
    content = "def the_function():\n    # The quick brown fox, and a very long comment.\n    return 'I would be happy to help'\n"
    rig = make_rig([{"tool_calls": [call("write_file", file_path="out.py", content=content)]}, {"content": "done"}],
                   style=style, answers=("y",))
    rig.agent.run("write it")
    assert (repo / "out.py").read_text() == content                   # byte for byte
    echoed = json.loads(rig.sent()[1]["messages"][2]["tool_calls"][0]["function"]["arguments"])
    assert echoed["arguments"]["content"] == content                     # what the model sent is what we sent back
    assert style_instruction(style) not in str(rig.sent()[1]["messages"][1:])  # the instruction only lives in the system message


def test_approval_prompt_is_plain_whatever_the_style(make_rig):
    rig = make_rig([{"tool_calls": [call("run_command", command="ls")]}, {"content": "done"}], style="caveman")
    rig.agent.run("go")
    prompt = rig.asked[0]
    assert "pinch wants to run a command" in prompt and "Allow?" in prompt and "caveman" not in prompt.lower()


def test_caveman_is_credited_in_code_and_readme():
    assert "JuliusBrussee/caveman" in (ROOT / "src" / "pinch" / "styles.py").read_text()
    assert "JuliusBrussee/caveman" in (ROOT / "README.md").read_text()
