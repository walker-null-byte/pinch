"""Response styles. Each instruction is under 5 lines because it is sent again on every call.

A style shapes the model's prose only. It never touches code, diffs, tool arguments or
commit messages, and pinch never rewrites any model output: the style is just the system
prompt text below. Approval prompts and errors are written by pinch's own code in plain
language, whatever the style.

The `caveman` rules are adapted from JuliusBrussee/caveman (https://github.com/JuliusBrussee/caveman),
originally MIT licensed. Credit to Julius Brussee. See README.md.
"""

_SCOPE = "This applies to your prose only. Code, diffs, tool arguments and commit messages stay normal and complete."

STYLES: dict[str, str] = {
    "normal": "Reply in ordinary, complete sentences.",
    "brief": "Reply in short, plain sentences. No greetings, filler or recap of what you did.\n" + _SCOPE,
    "tiny": "Reply with one line stating the result, then the diff if there is one. No narration between tool calls.\n" + _SCOPE,
    # Adapted from caveman: answer first, kill ceremony, short words, articles optional,
    # technical payload verbatim. Never drop not/no/never/only.
    "caveman": (
        "Talk like smart caveman. Answer first. Drop greetings, hedging, filler and articles. Fragments fine. Short words.\n"
        "Never drop not, no, never or only. Keep numbers, paths, identifiers and error text exact.\n" + _SCOPE
    ),
}


def style_instruction(name: str) -> str:
    return STYLES[name]
