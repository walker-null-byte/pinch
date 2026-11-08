"""Technique `trim`: old tool output becomes a one-line stub.

Applied to the copy of the history that is sent to the model; the session file keeps everything. A tool result
is stubbed once it is more than `keep` model steps old, unless it carries the latest version of a file
(see filememory). The stub depends only on the message and its age, so the same message becomes the same stub
every time, and the part of the request before it stays byte-identical for provider prompt caching.
"""

from __future__ import annotations

import json

MIN_CHARS = 200  # shorter results are not worth a stub


def trim_history(messages: list[dict], keep: int, protected: set[str]) -> list[dict]:
    calls: dict[str, str] = {}
    for m in messages:
        for c in m.get("tool_calls") or []:
            calls[c["id"]] = _describe(c)
    assistant_after = 0
    out = list(messages)
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m["role"] == "assistant":
            assistant_after += 1
        elif m["role"] == "tool" and assistant_after > keep and m["tool_call_id"] not in protected and len(m["content"]) > MIN_CHARS:
            what = calls.get(m["tool_call_id"], "a tool call")
            out[i] = {**m, "content": f"[pinch: earlier output of {what} cut, {len(m['content'])} characters. Run it again if you need it.]"}
    return out


def _describe(call: dict) -> str:
    fn = call.get("function", {})
    name, raw = fn.get("name", "?"), fn.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except ValueError:
        args = {}
    if name == "call_tool":
        name, inner = args.get("name", "?"), args.get("arguments")
        try:
            args = json.loads(inner) if isinstance(inner, str) else dict(inner or {})
        except ValueError:
            args = {}
    target = args.get("file_path") or args.get("command") or args.get("pattern") or args.get("dir_path") or args.get("query") or ""
    return f"{name} {str(target)[:60]}".strip()
